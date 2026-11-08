from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionFactory
from app.models import Ticket, TicketStatus as S


def _after(seconds: float):
    return func.now() + timedelta(seconds=seconds)


def _release() -> dict:
    return {"lease_token": None, "locked_until": None, "updated_at": func.now()}


async def _execute(stmt) -> bool:
    async with SessionFactory() as session, session.begin():
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        return result.rowcount > 0


async def create_from_event(session: AsyncSession, payload: dict) -> bool:
    stmt = (
        pg_insert(Ticket)
        .values(**payload)
        .on_conflict_do_nothing(index_elements=[Ticket.event_id])
        .returning(Ticket.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()
    return inserted is not None


async def _claim(claimable, values: dict, limit: int) -> list[Ticket]:
    if limit <= 0:
        return []
    token = uuid4().hex
    async with SessionFactory() as session, session.begin():
        ids = (
            select(Ticket.id)
            .where(claimable)
            .order_by(Ticket.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(Ticket)
            .where(Ticket.id.in_(ids))
            .values(lease_token=token, updated_at=func.now(), **values)
            .returning(Ticket)
            .execution_options(synchronize_session=False)
        )
        return list((await session.scalars(stmt)).all())


def _owned(ticket: Ticket, status: str):
    return and_(Ticket.id == ticket.id, Ticket.lease_token == ticket.lease_token, Ticket.status == status)


async def claim_for_generation(limit: int) -> list[Ticket]:
    claimable = or_(
        and_(Ticket.status == S.NEW, Ticket.next_attempt_at <= func.now()),
        and_(Ticket.status == S.GENERATING, Ticket.locked_until < func.now()),
    )
    values = {
        "status": S.GENERATING,
        "locked_until": _after(settings.generation_lease_seconds),
        "generation_attempts": Ticket.generation_attempts + 1,
    }
    return await _claim(claimable, values, limit)


async def complete_generation(ticket: Ticket, draft: str, articles: list[dict], mode: str) -> bool:
    stmt = (
        update(Ticket)
        .where(_owned(ticket, S.GENERATING))
        .values(status=S.DRAFT_READY, draft=draft, articles=articles, llm_mode=mode, last_error=None, **_release())
    )
    return await _execute(stmt)


async def reschedule_generation(ticket: Ticket, error: str, delay: float) -> bool:
    stmt = (
        update(Ticket)
        .where(_owned(ticket, S.GENERATING))
        .values(status=S.NEW, next_attempt_at=_after(delay), last_error=error, **_release())
    )
    return await _execute(stmt)


async def fail_generation(ticket: Ticket, error: str) -> bool:
    stmt = (
        update(Ticket)
        .where(_owned(ticket, S.GENERATING))
        .values(status=S.GENERATION_FAILED, last_error=error, **_release())
    )
    return await _execute(stmt)


async def claim_for_sending(limit: int) -> list[Ticket]:
    claimable = or_(
        and_(Ticket.status == S.APPROVED, Ticket.next_attempt_at <= func.now()),
        and_(Ticket.status == S.SENDING, Ticket.locked_until < func.now()),
    )
    values = {
        "status": S.SENDING,
        "locked_until": _after(settings.send_lease_seconds),
        "send_attempts": Ticket.send_attempts + 1,
    }
    return await _claim(claimable, values, limit)


async def mark_sent(ticket: Ticket) -> bool:
    stmt = (
        update(Ticket)
        .where(_owned(ticket, S.SENDING))
        .values(status=S.SENT, sent_at=func.now(), last_error=None, **_release())
    )
    return await _execute(stmt)


async def reschedule_send(ticket: Ticket, error: str, delay: float) -> bool:
    stmt = (
        update(Ticket)
        .where(_owned(ticket, S.SENDING))
        .values(status=S.APPROVED, next_attempt_at=_after(delay), last_error=error, **_release())
    )
    return await _execute(stmt)


async def fail_send(ticket: Ticket, error: str) -> bool:
    stmt = (
        update(Ticket)
        .where(_owned(ticket, S.SENDING))
        .values(status=S.SEND_FAILED, last_error=error, **_release())
    )
    return await _execute(stmt)


async def approve(ticket_id: int, message: str | None) -> bool:
    final_message = message.strip() if message and message.strip() else None
    stmt = (
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status == S.DRAFT_READY)
        .values(
            status=S.APPROVED,
            final_message=func.coalesce(final_message, Ticket.draft),
            idempotency_key=func.coalesce(Ticket.idempotency_key, uuid4().hex),
            approved_at=func.now(),
            next_attempt_at=func.now(),
            send_attempts=0,
            last_error=None,
            updated_at=func.now(),
        )
    )
    return await _execute(stmt)


async def reject(ticket_id: int) -> bool:
    stmt = (
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status == S.DRAFT_READY)
        .values(status=S.REJECTED, updated_at=func.now())
    )
    return await _execute(stmt)


async def regenerate(ticket_id: int) -> bool:
    stmt = (
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status.in_([S.DRAFT_READY, S.GENERATION_FAILED, S.REJECTED]))
        .values(
            status=S.NEW,
            draft=None,
            articles=[],
            llm_mode=None,
            generation_attempts=0,
            next_attempt_at=func.now(),
            last_error=None,
            updated_at=func.now(),
        )
    )
    return await _execute(stmt)


async def retry_send(ticket_id: int) -> bool:
    stmt = (
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.status == S.SEND_FAILED)
        .values(status=S.APPROVED, send_attempts=0, next_attempt_at=func.now(), updated_at=func.now())
    )
    return await _execute(stmt)


async def get(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def list_tickets(session: AsyncSession, status: str | None, limit: int = 200) -> list[Ticket]:
    stmt = select(Ticket).order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(Ticket.status == status)
    return list((await session.scalars(stmt)).all())


async def status_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(select(Ticket.status, func.count()).group_by(Ticket.status))
    counts = {status: 0 for status in S.ALL}
    counts.update({status: count for status, count in rows.all()})
    return counts
