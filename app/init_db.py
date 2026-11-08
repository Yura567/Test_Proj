import asyncio
import logging

from sqlalchemy import text

from app.db import SessionFactory, engine
from app.kb import seed_knowledge_base
from app.models import Base

log = logging.getLogger("app.init_db")

SCHEMA_LOCK_KEY = 815_000


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
        await conn.run_sync(Base.metadata.create_all)
    async with SessionFactory() as session:
        count = await seed_knowledge_base(session)
    log.info("Database schema ready, knowledge base contains %s articles", count)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        await init_db()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
