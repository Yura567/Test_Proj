import logging
import random

from app import kb, tickets
from app.config import settings
from app.db import SessionFactory
from app.llm import LlmClient, LlmError
from app.models import Ticket

log = logging.getLogger(__name__)


def backoff(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * 2 ** max(0, attempt - 1)) + random.uniform(0, base)


def ensure_article_links(text: str, refs: list[dict]) -> str:
    if not refs or any(ref["url"] in text for ref in refs):
        return text
    links = "\n".join(f"- {ref['title']}: {ref['url']}" for ref in refs)
    return f"{text.rstrip()}\n\nКорисні статті:\n{links}"


class DraftGenerator:
    def __init__(self, llm: LlmClient) -> None:
        self.llm = llm

    async def process(self, ticket: Ticket) -> None:
        if ticket.generation_attempts > settings.generation_max_attempts:
            await tickets.fail_generation(ticket, "Перевищено кількість спроб генерації")
            return

        async with SessionFactory() as session:
            found = await kb.search_articles(session, ticket.message, settings.kb_top_k)
        refs = [kb.article_ref(article) for article in found]
        context = [{**ref, "body": article.body} for ref, article in zip(refs, found)]

        try:
            result = await self.llm.generate(ticket.message, context)
        except LlmError as exc:
            await self._handle_failure(ticket, str(exc), exc.retryable, exc.retry_after)
            return
        except Exception as exc:
            log.exception("Unexpected error while generating draft for ticket %s", ticket.id)
            await self._handle_failure(ticket, f"{type(exc).__name__}: {exc}", True, None)
            return

        draft = ensure_article_links(result.text, refs)
        if await tickets.complete_generation(ticket, draft, refs, result.mode):
            log.info("Draft ready for ticket %s (%s mode)", ticket.id, result.mode)
        else:
            log.warning("Lease lost for ticket %s, draft discarded", ticket.id)

    async def _handle_failure(self, ticket: Ticket, error: str, retryable: bool, retry_after: float | None) -> None:
        if retryable and ticket.generation_attempts < settings.generation_max_attempts:
            delay = max(retry_after or 0.0, backoff(ticket.generation_attempts, 10, 300))
            log.warning("Generation for ticket %s failed, retry in %.0fs: %s", ticket.id, delay, error)
            await tickets.reschedule_generation(ticket, error, delay)
        else:
            log.error("Generation for ticket %s failed permanently: %s", ticket.id, error)
            await tickets.fail_generation(ticket, error)
