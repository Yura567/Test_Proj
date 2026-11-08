import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

import httpx

from app import tickets
from app.config import settings
from app.db import engine
from app.generation import DraftGenerator
from app.llm import LlmClient
from app.models import Ticket
from app.rate_limiter import RateLimiter
from app.sending import ReplySender

log = logging.getLogger("app.worker")

SHUTDOWN_GRACE_SECONDS = 30


class ProcessingLoop:
    def __init__(
        self,
        name: str,
        concurrency: int,
        claim: Callable[[int], Awaitable[list[Ticket]]],
        handle: Callable[[Ticket], Awaitable[None]],
    ) -> None:
        self.name = name
        self.concurrency = concurrency
        self.claim = claim
        self.handle = handle
        self.tasks: set[asyncio.Task] = set()

    async def run(self, stop: asyncio.Event) -> None:
        log.info("%s loop started (concurrency=%s)", self.name, self.concurrency)
        while not stop.is_set():
            claimed: list[Ticket] = []
            free = self.concurrency - len(self.tasks)
            if free > 0:
                try:
                    claimed = await self.claim(free)
                except Exception:
                    log.exception("%s loop failed to claim tickets", self.name)
            for ticket in claimed:
                task = asyncio.create_task(self._safe_handle(ticket))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            if not claimed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval)
                except TimeoutError:
                    pass
        if self.tasks:
            log.info("%s loop waiting for %s in-flight tasks", self.name, len(self.tasks))
            await asyncio.wait(self.tasks, timeout=SHUTDOWN_GRACE_SECONDS)

    async def _safe_handle(self, ticket: Ticket) -> None:
        try:
            await self.handle(ticket)
        except Exception:
            log.exception("%s loop failed to process ticket %s", self.name, ticket.id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    llm = LlmClient(RateLimiter(settings.llm_rate_limit_per_minute, 60.0))
    log.info("LLM mode: %s, model: %s", llm.mode, settings.llm_model)
    async with httpx.AsyncClient() as http:
        generator = DraftGenerator(llm)
        sender = ReplySender(http)
        loops = [
            ProcessingLoop("generation", settings.generation_concurrency, tickets.claim_for_generation, generator.process),
            ProcessingLoop("sending", settings.send_concurrency, tickets.claim_for_sending, sender.process),
        ]
        try:
            await asyncio.gather(*(processing_loop.run(stop) for processing_loop in loops))
        finally:
            await llm.close()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
