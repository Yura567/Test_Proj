import asyncio

from sqlalchemy import func, select

from app.db import SessionFactory
from app.models import Ticket, TicketStatus


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.text == "OK"


async def test_webhook_stores_ticket(client, event_payload):
    response = await client.post("/webhooks/helpdesk", json=event_payload)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "event_id": "evt_8f2a"}

    async with SessionFactory() as session:
        ticket = await session.scalar(select(Ticket))
    assert ticket.ticket_id == "T-1042"
    assert ticket.status == TicketStatus.NEW


async def test_webhook_deduplicates_concurrent_deliveries(client, event_payload):
    responses = await asyncio.gather(
        *(client.post("/webhooks/helpdesk", json=event_payload) for _ in range(20))
    )
    assert all(r.status_code == 200 for r in responses)
    statuses = [r.json()["status"] for r in responses]
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == 19

    async with SessionFactory() as session:
        assert await session.scalar(select(func.count()).select_from(Ticket)) == 1


async def test_webhook_rejects_invalid_payload(client, event_payload):
    event_payload["customer_email"] = "not-an-email"
    response = await client.post("/webhooks/helpdesk", json=event_payload)
    assert response.status_code == 422
