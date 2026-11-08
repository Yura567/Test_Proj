import asyncio
import os

import asyncpg
import pytest

BASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://support:support@localhost:5432/support")
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or BASE_URL.rsplit("/", 1)[0] + "/support_test"

os.environ.update(
    {
        "DATABASE_URL": TEST_DATABASE_URL,
        "DB_NULL_POOL": "true",
        "ANTHROPIC_API_KEY": "",
        "OPERATOR_PASSWORD": "",
        "PUBLIC_BASE_URL": "http://testserver",
        "REPLY_URL_REWRITE_FROM": "",
        "REPLY_URL_REWRITE_TO": "",
        "LLM_STUB_MIN_DELAY": "0",
        "LLM_STUB_MAX_DELAY": "0.05",
    }
)


async def _prepare_database() -> None:
    server_url, name = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit("/", 1)
    conn = await asyncpg.connect(f"{server_url}/postgres")
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()

    from app.init_db import init_db

    await init_db()


asyncio.run(_prepare_database())


@pytest.fixture(autouse=True)
async def clean_tables():
    from sqlalchemy import text

    from app.db import engine

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE tickets, llm_calls RESTART IDENTITY"))
    yield


@pytest.fixture
async def client():
    import httpx

    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        yield http


@pytest.fixture
def event_payload():
    return {
        "event_id": "evt_8f2a",
        "ticket_id": "T-1042",
        "customer_email": "user@example.com",
        "message": "Не можу експортувати звіт у PDF",
        "reply_url": "https://helpdesk.example.com/tickets/T-1042/replies",
    }
