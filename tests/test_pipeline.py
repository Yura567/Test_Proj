import asyncio
import time

import httpx
from sqlalchemy import update

from app import kb, tickets
from app.db import SessionFactory
from app.generation import DraftGenerator, ensure_article_links
from app.llm import STATIC_RESPONSE, LlmClient
from app.models import Ticket, TicketStatus
from app.rate_limiter import RateLimiter
from app.sending import ReplySender


class NoProbeLlmClient(LlmClient):
    async def _probe_provider(self, message, articles):
        self.probed = True


async def _make_draft(client, event_payload) -> Ticket:
    await client.post("/webhooks/helpdesk", json=event_payload)
    llm = NoProbeLlmClient(RateLimiter(60))
    [claimed] = await tickets.claim_for_generation(10)
    await DraftGenerator(llm).process(claimed)
    await llm.close()
    async with SessionFactory() as session:
        return await session.get(Ticket, claimed.id)


async def _force_due(ticket_id: int) -> None:
    async with SessionFactory() as session, session.begin():
        await session.execute(
            update(Ticket).where(Ticket.id == ticket_id).values(next_attempt_at=Ticket.created_at)
        )


async def test_search_finds_export_article():
    async with SessionFactory() as session:
        found = await kb.search_articles(session, "Не можу експортувати звіт у PDF", 3)
    assert found
    assert found[0].slug == "export-reports"


async def test_search_finds_password_article():
    async with SessionFactory() as session:
        found = await kb.search_articles(session, "Забув пароля, не можу увійти", 3)
    assert found[0].slug == "password-reset"


def test_ensure_article_links_appends_missing_links():
    refs = [{"slug": "a", "title": "Стаття", "url": "http://x/kb/a"}]
    assert "http://x/kb/a" in ensure_article_links("Текст", refs)
    assert ensure_article_links("Див. http://x/kb/a", refs) == "Див. http://x/kb/a"


async def test_generation_produces_draft_with_links(client, event_payload):
    ticket = await _make_draft(client, event_payload)
    assert ticket.status == TicketStatus.DRAFT_READY
    assert ticket.llm_mode == "stub"
    assert ticket.draft.startswith(STATIC_RESPONSE)
    assert ticket.articles[0]["slug"] == "export-reports"
    assert "http://testserver/kb/export-reports" in ticket.draft
    assert ticket.lease_token is None


async def test_claim_is_exclusive(client, event_payload):
    await client.post("/webhooks/helpdesk", json=event_payload)
    results = await asyncio.gather(*(tickets.claim_for_generation(5) for _ in range(5)))
    assert sum(len(r) for r in results) == 1


async def test_approve_only_once(client, event_payload):
    ticket = await _make_draft(client, event_payload)
    assert await tickets.approve(ticket.id, "Готова відповідь")
    assert not await tickets.approve(ticket.id, "Інша відповідь")
    assert not await tickets.reject(ticket.id)


async def test_send_retries_with_same_idempotency_key(client, event_payload):
    ticket = await _make_draft(client, event_payload)
    response = await client.post(f"/api/tickets/{ticket.id}/approve", json={"message": "Відповідь клієнту"})
    assert response.status_code == 200

    calls: list[httpx.Request] = []
    statuses = iter([503, 201])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(next(statuses), json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sender = ReplySender(http)
        [claimed] = await tickets.claim_for_sending(10)
        await sender.process(claimed)

        async with SessionFactory() as session:
            after_failure = await session.get(Ticket, ticket.id)
        assert after_failure.status == TicketStatus.APPROVED
        assert "503" in after_failure.last_error

        await _force_due(ticket.id)
        [claimed] = await tickets.claim_for_sending(10)
        await sender.process(claimed)

    async with SessionFactory() as session:
        sent = await session.get(Ticket, ticket.id)
    assert sent.status == TicketStatus.SENT
    assert len(calls) == 2
    keys = {c.headers["Idempotency-Key"] for c in calls}
    assert keys == {sent.idempotency_key}
    assert calls[0].read() == calls[1].read()
    assert await tickets.claim_for_sending(10) == []


async def test_send_client_error_is_permanent(client, event_payload):
    ticket = await _make_draft(client, event_payload)
    await tickets.approve(ticket.id, None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400))) as http:
        [claimed] = await tickets.claim_for_sending(10)
        await ReplySender(http).process(claimed)

    async with SessionFactory() as session:
        failed = await session.get(Ticket, ticket.id)
    assert failed.status == TicketStatus.SEND_FAILED
    assert failed.final_message == failed.draft


async def test_expired_lease_is_reclaimed(client, event_payload):
    await client.post("/webhooks/helpdesk", json=event_payload)
    [first] = await tickets.claim_for_generation(1)
    assert await tickets.claim_for_generation(1) == []

    async with SessionFactory() as session, session.begin():
        await session.execute(update(Ticket).where(Ticket.id == first.id).values(locked_until=Ticket.created_at))

    [second] = await tickets.claim_for_generation(1)
    assert second.id == first.id
    assert second.lease_token != first.lease_token
    assert not await tickets.complete_generation(first, "stale", [], "stub")


async def test_rate_limiter_enforces_window():
    limiter = RateLimiter(limit=3, window_seconds=1.0)
    started = time.monotonic()
    for _ in range(4):
        await limiter.acquire()
    assert time.monotonic() - started >= 0.9
