import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import tickets
from app.config import settings
from app.db import engine, get_session
from app.kb import article_url
from app.models import KbArticle, TicketStatus
from app.schemas import ActionResult, ApproveRequest, HelpdeskEvent, TicketOut, WebhookAck

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATUS_LABELS = {
    TicketStatus.NEW: "В черзі на генерацію",
    TicketStatus.GENERATING: "Генерується",
    TicketStatus.DRAFT_READY: "Чернетка готова",
    TicketStatus.GENERATION_FAILED: "Помилка генерації",
    TicketStatus.APPROVED: "Підтверджено, в черзі на надсилання",
    TicketStatus.SENDING: "Надсилається",
    TicketStatus.SENT: "Надіслано",
    TicketStatus.SEND_FAILED: "Помилка надсилання",
    TicketStatus.REJECTED: "Відхилено",
}

NOTICES = {
    "approved": "Відповідь підтверджено і поставлено в чергу на надсилання.",
    "rejected": "Чернетку відхилено.",
    "regenerating": "Звернення повернуто в чергу на генерацію.",
    "retrying": "Надсилання буде повторено.",
    "conflict": "Дію не виконано: статус звернення вже змінився.",
    "empty": "Текст відповіді не може бути порожнім.",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["STATUSES"] = TicketStatus.ALL

security = HTTPBasic(auto_error=False)


def require_operator(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not settings.operator_password:
        return
    if credentials is not None:
        user_ok = secrets.compare_digest(credentials.username.encode(), settings.operator_username.encode())
        password_ok = secrets.compare_digest(credentials.password.encode(), settings.operator_password.encode())
        if user_ok and password_ok:
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Support Draft Assistant", lifespan=lifespan)
operator = APIRouter(dependencies=[Depends(require_operator)])


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"


@app.post("/webhooks/helpdesk", response_model=WebhookAck)
async def helpdesk_webhook(event: HelpdeskEvent, session: AsyncSession = Depends(get_session)) -> WebhookAck:
    created = await tickets.create_from_event(
        session,
        {
            "event_id": event.event_id,
            "ticket_id": event.ticket_id,
            "customer_email": str(event.customer_email),
            "message": event.message,
            "reply_url": str(event.reply_url),
        },
    )
    return WebhookAck(status="accepted" if created else "duplicate", event_id=event.event_id)


@app.get("/kb", response_class=HTMLResponse)
async def kb_index(request: Request, session: AsyncSession = Depends(get_session)):
    articles = (await session.scalars(select(KbArticle).order_by(KbArticle.id))).all()
    return templates.TemplateResponse(request, "kb_list.html", {"articles": articles})


@app.get("/kb/{slug}", response_class=HTMLResponse)
async def kb_article(slug: str, request: Request, session: AsyncSession = Depends(get_session)):
    article = await session.scalar(select(KbArticle).where(KbArticle.slug == slug))
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return templates.TemplateResponse(
        request, "kb_article.html", {"article": article, "url": article_url(article.slug)}
    )


def _redirect(ticket_id: int, notice: str) -> RedirectResponse:
    return RedirectResponse(f"/tickets/{ticket_id}?notice={quote(notice)}", status_code=status.HTTP_303_SEE_OTHER)


@operator.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse("/tickets", status_code=status.HTTP_303_SEE_OTHER)


@operator.get("/tickets", response_class=HTMLResponse)
async def tickets_page(
    request: Request,
    status_filter: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    if status_filter not in TicketStatus.ALL:
        status_filter = None
    items = await tickets.list_tickets(session, status_filter)
    counts = await tickets.status_counts(session)
    return templates.TemplateResponse(
        request,
        "tickets.html",
        {"tickets": items, "counts": counts, "status_filter": status_filter, "total": sum(counts.values())},
    )


@operator.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def ticket_page(
    ticket_id: int,
    request: Request,
    notice: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    ticket = await tickets.get(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return templates.TemplateResponse(
        request, "ticket.html", {"ticket": ticket, "notice": NOTICES.get(notice or "")}
    )


@operator.post("/tickets/{ticket_id}/approve")
async def approve_form(ticket_id: int, message: str = Form("")) -> RedirectResponse:
    if not message.strip():
        return _redirect(ticket_id, "empty")
    ok = await tickets.approve(ticket_id, message)
    return _redirect(ticket_id, "approved" if ok else "conflict")


@operator.post("/tickets/{ticket_id}/reject")
async def reject_form(ticket_id: int) -> RedirectResponse:
    ok = await tickets.reject(ticket_id)
    return _redirect(ticket_id, "rejected" if ok else "conflict")


@operator.post("/tickets/{ticket_id}/regenerate")
async def regenerate_form(ticket_id: int) -> RedirectResponse:
    ok = await tickets.regenerate(ticket_id)
    return _redirect(ticket_id, "regenerating" if ok else "conflict")


@operator.post("/tickets/{ticket_id}/retry-send")
async def retry_send_form(ticket_id: int) -> RedirectResponse:
    ok = await tickets.retry_send(ticket_id)
    return _redirect(ticket_id, "retrying" if ok else "conflict")


async def _ticket_or_404(session: AsyncSession, ticket_id: int):
    ticket = await tickets.get(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


async def _action_result(session: AsyncSession, ticket_id: int, ok: bool) -> ActionResult:
    ticket = await _ticket_or_404(session, ticket_id)
    if not ok:
        raise HTTPException(status_code=409, detail=f"Ticket is in status '{ticket.status}'")
    return ActionResult(ok=True, status=ticket.status)


@operator.get("/api/tickets", response_model=list[TicketOut])
async def api_list_tickets(status_filter: str | None = None, session: AsyncSession = Depends(get_session)):
    return await tickets.list_tickets(session, status_filter)


@operator.get("/api/tickets/{ticket_id}", response_model=TicketOut)
async def api_get_ticket(ticket_id: int, session: AsyncSession = Depends(get_session)):
    return await _ticket_or_404(session, ticket_id)


@operator.post("/api/tickets/{ticket_id}/approve", response_model=ActionResult)
async def api_approve(
    ticket_id: int,
    body: ApproveRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    ok = await tickets.approve(ticket_id, body.message if body else None)
    return await _action_result(session, ticket_id, ok)


@operator.post("/api/tickets/{ticket_id}/reject", response_model=ActionResult)
async def api_reject(ticket_id: int, session: AsyncSession = Depends(get_session)):
    ok = await tickets.reject(ticket_id)
    return await _action_result(session, ticket_id, ok)


@operator.post("/api/tickets/{ticket_id}/regenerate", response_model=ActionResult)
async def api_regenerate(ticket_id: int, session: AsyncSession = Depends(get_session)):
    ok = await tickets.regenerate(ticket_id)
    return await _action_result(session, ticket_id, ok)


@operator.post("/api/tickets/{ticket_id}/retry-send", response_model=ActionResult)
async def api_retry_send(ticket_id: int, session: AsyncSession = Depends(get_session)):
    ok = await tickets.retry_send(ticket_id)
    return await _action_result(session, ticket_id, ok)


app.include_router(operator)
