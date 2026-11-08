import asyncio
import random

from sqlalchemy import text

from app.db import SessionFactory

LOCK_KEY = 815_001


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = float(window_seconds)

    async def acquire(self) -> None:
        while True:
            wait = await self._try_acquire()
            if wait is None:
                return
            await asyncio.sleep(wait + random.uniform(0, 0.1))

    async def _try_acquire(self) -> float | None:
        async with SessionFactory() as session, session.begin():
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
            await session.execute(
                text("DELETE FROM llm_calls WHERE created_at <= clock_timestamp() - make_interval(secs => :window)"),
                {"window": self.window},
            )
            count, oldest_age = (
                await session.execute(
                    text("SELECT count(*), extract(epoch FROM clock_timestamp() - min(created_at)) FROM llm_calls")
                )
            ).one()
            if count < self.limit:
                await session.execute(text("INSERT INTO llm_calls DEFAULT VALUES"))
                return None
            return max(0.05, self.window - float(oldest_age or 0))
