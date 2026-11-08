import asyncio
import logging
import os
import random
import uuid
from collections import Counter
from datetime import datetime, timezone
from html import escape

import httpx
from fastapi import FastAPI, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("helpdesk_mock")

FAIL_RATE = float(os.getenv("MOCK_FAIL_RATE", "0.2"))
SLOW_RATE = float(os.getenv("MOCK_SLOW_RATE", "0.1"))
SLOW_SECONDS = float(os.getenv("MOCK_SLOW_SECONDS", "15"))
WEBHOOK_URL = os.getenv("MOCK_WEBHOOK_URL", "http://web:8000/webhooks/helpdesk")
PUBLIC_URL = os.getenv("MOCK_PUBLIC_URL", "http://helpdesk-mock:8001").rstrip("/")
WEBHOOK_TIMEOUT = float(os.getenv("MOCK_WEBHOOK_TIMEOUT", "2"))
WEBHOOK_MAX_DELIVERIES = int(os.getenv("MOCK_WEBHOOK_MAX_DELIVERIES", "10"))
SIMULATION_CONCURRENCY = int(os.getenv("MOCK_SIMULATION_CONCURRENCY", "50"))

SAMPLE_MESSAGES = [
    "Не можу експортувати звіт у PDF",
    "Забув пароль і не можу увійти в акаунт",
    "Як змінити тариф з Pro на Business і де взяти рахунок?",
    "Не вдається підключити базу PostgreSQL, з'єднання не встановлюється",
    "Дані у звіті не оновлюються вже два дні",
    "Як налаштувати щотижневу розсилку звіту на пошту керівнику?",
    "Хочу запросити колегу в команду, але не бачу кнопки",
    "Як вбудувати дашборд на наш сайт?",
    "API повертає помилку 401, хоча токен правильний",
    "Як видалити акаунт і всі наші дані?",
    "Чи можна замовити піцу через ваш сервіс?",
]


class ReplyIn(BaseModel):
    ticket_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


app = FastAPI(title="Helpdesk mock")

replies: list[dict] = []
replies_by_key: dict[str, dict] = {}
stats: Counter = Counter()
lock = asyncio.Lock()


@app.get("/health", response_class=PlainTextResponse)
async def health() -> str:
    return "OK"


@app.post("/tickets/{ticket_id}/replies")
async def create_reply(ticket_id: str, body: ReplyIn, idempotency_key: str | None = Header(default=None)):
    stats["requests"] += 1
    roll = random.random()
    if roll < FAIL_RATE:
        stats["failed_5xx"] += 1
        log.info("Reply for %s: simulated 503", ticket_id)
        return JSONResponse({"error": "temporarily unavailable"}, status_code=503)

    async with lock:
        existing = replies_by_key.get(idempotency_key) if idempotency_key else None
        if existing is not None:
            stats["deduplicated"] += 1
            log.info("Reply for %s: duplicate request with key %s ignored", ticket_id, idempotency_key)
            return JSONResponse(existing, status_code=201)
        reply = {
            "id": len(replies) + 1,
            "ticket_id": ticket_id,
            "message": body.message,
            "idempotency_key": idempotency_key,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        replies.append(reply)
        if idempotency_key:
            replies_by_key[idempotency_key] = reply
        stats["delivered"] += 1

    if roll < FAIL_RATE + SLOW_RATE:
        stats["slow"] += 1
        log.info("Reply for %s: stored, simulating slow response (%ss)", ticket_id, SLOW_SECONDS)
        await asyncio.sleep(SLOW_SECONDS)
    log.info("Reply for %s delivered to customer", ticket_id)
    return JSONResponse(reply, status_code=201)


def _duplicate_deliveries() -> int:
    counts = Counter((r["ticket_id"], r["message"]) for r in replies)
    return sum(count - 1 for count in counts.values() if count > 1)


@app.get("/replies")
async def list_replies():
    return {
        "stats": dict(stats),
        "duplicate_deliveries": _duplicate_deliveries(),
        "replies": replies,
    }


async def _deliver(client: httpx.AsyncClient, event: dict, semaphore: asyncio.Semaphore) -> bool:
    for attempt in range(1, WEBHOOK_MAX_DELIVERIES + 1):
        try:
            async with semaphore:
                response = await client.post(WEBHOOK_URL, json=event, timeout=WEBHOOK_TIMEOUT)
            if response.is_success:
                return True
            log.warning("Webhook %s attempt %s: HTTP %s", event["event_id"], attempt, response.status_code)
        except httpx.HTTPError as exc:
            log.warning("Webhook %s attempt %s: %s", event["event_id"], attempt, type(exc).__name__)
        await asyncio.sleep(min(30, attempt))
    return False


def _make_event(message: str | None) -> dict:
    ticket_id = f"T-{uuid.uuid4().hex[:6].upper()}"
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "ticket_id": ticket_id,
        "customer_email": f"user{random.randint(1, 9999)}@example.com",
        "message": message or random.choice(SAMPLE_MESSAGES),
        "reply_url": f"{PUBLIC_URL}/tickets/{ticket_id}/replies",
    }


@app.post("/simulate")
async def simulate(
    count: int = Query(default=5, ge=1, le=1000),
    copies: int = Query(default=2, ge=1, le=10),
    message: str | None = Query(default=None),
):
    events = [_make_event(message) for _ in range(count)]
    semaphore = asyncio.Semaphore(SIMULATION_CONCURRENCY)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(_deliver(client, e, semaphore) for e in events for _ in range(copies)))
    return {
        "events": count,
        "deliveries": len(results),
        "acknowledged": sum(results),
        "failed": len(results) - sum(results),
        "event_ids": [e["event_id"] for e in events],
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    rows = "".join(
        f"<tr><td>{r['id']}</td><td>{escape(r['ticket_id'])}</td><td>{escape(r['received_at'])}</td>"
        f"<td><pre>{escape(r['message'])}</pre></td></tr>"
        for r in reversed(replies)
    )
    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Helpdesk mock</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px}}
td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}
table{{border-collapse:collapse;width:100%}}pre{{white-space:pre-wrap;margin:0}}</style></head>
<body><h1>Helpdesk mock</h1>
<form method="post" action="/simulate?count=5&amp;copies=2">
  <button type="submit">Надіслати 5 звернень (кожне доставляється двічі одночасно)</button>
</form>
<p>Статистика: {escape(str(dict(stats)))}; повторних доставок клієнту: {_duplicate_deliveries()}</p>
<h2>Відповіді, отримані клієнтами ({len(replies)})</h2>
<table><tr><th>#</th><th>Тікет</th><th>Отримано</th><th>Текст</th></tr>{rows}</table>
</body></html>"""
