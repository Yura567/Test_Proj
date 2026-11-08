import logging

import httpx

from app import tickets
from app.config import settings
from app.generation import backoff
from app.models import Ticket

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = {408, 425, 429}


def resolve_reply_url(url: str) -> str:
    source, target = settings.reply_url_rewrite_from, settings.reply_url_rewrite_to
    if source and target and url.startswith(source):
        return target.rstrip("/") + url[len(source.rstrip("/")) :]
    return url


class ReplySender:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def process(self, ticket: Ticket) -> None:
        if ticket.send_attempts > settings.send_max_attempts:
            await tickets.fail_send(ticket, "Перевищено кількість спроб надсилання")
            return

        url = resolve_reply_url(ticket.reply_url)
        try:
            response = await self.http.post(
                url,
                json={"ticket_id": ticket.ticket_id, "message": ticket.final_message},
                headers={"Idempotency-Key": ticket.idempotency_key},
                timeout=settings.send_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            await self._handle_failure(ticket, f"{type(exc).__name__}: {exc}", True)
            return

        if response.is_success:
            await tickets.mark_sent(ticket)
            log.info("Reply for ticket %s delivered (HTTP %s)", ticket.id, response.status_code)
            return

        error = f"Helpdesk HTTP {response.status_code}: {response.text[:300]}"
        retryable = response.status_code >= 500 or response.status_code in RETRYABLE_STATUSES
        await self._handle_failure(ticket, error, retryable)

    async def _handle_failure(self, ticket: Ticket, error: str, retryable: bool) -> None:
        if retryable and ticket.send_attempts < settings.send_max_attempts:
            delay = backoff(ticket.send_attempts, 5, 600)
            log.warning("Sending reply for ticket %s failed, retry in %.0fs: %s", ticket.id, delay, error)
            await tickets.reschedule_send(ticket, error, delay)
        else:
            log.error("Sending reply for ticket %s failed permanently: %s", ticket.id, error)
            await tickets.fail_send(ticket, error)
