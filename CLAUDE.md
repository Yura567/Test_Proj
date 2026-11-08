# CLAUDE.md

Backend service: helpdesk webhook → KB search → LLM draft → operator moderation → reply via helpdesk API. Python 3.12, FastAPI, async SQLAlchemy, PostgreSQL, Anthropic SDK. Task spec is summarized in README.md.

## Commands

- Run everything: `docker compose up --build`
- Tests: `docker compose exec web python -m pytest -q` (uses the `support_test` DB, created automatically)
- Load simulation: `curl -X POST "http://localhost:8001/simulate?count=50&copies=2"`

## Invariants (do not break)

- Webhook must answer well under 2s: only validate and `INSERT ... ON CONFLICT (event_id) DO NOTHING`. Never call LLM or external HTTP from the request path.
- Duplicate/concurrent webhook deliveries return 200 with `status: duplicate`, never 4xx/5xx.
- All background work goes through PostgreSQL: claim with `FOR UPDATE SKIP LOCKED` + `lease_token`/`locked_until`; every state transition by a worker is guarded by `_owned()` (id + lease_token + status).
- Operator actions are single atomic `UPDATE ... WHERE status = ...`; return 409/conflict when the row was not updated.
- A reply is sent with a stable `Idempotency-Key` (set once on approve) and an unchanged body on every retry. Retry only on network errors, 5xx, 408/425/429.
- LLM calls always go through `RateLimiter` (global, Postgres-backed, 60/min).
- Without `ANTHROPIC_API_KEY` the client still sends a real request to the provider, then returns `STATIC_RESPONSE` after a total 5–30s delay.
- LLM: `claude-opus-5-5`, `output_config.effort`, server-side fallback beta `server-side-fallback-2026-07-01` with `fallbacks: "default"`; check `stop_reason == "refusal"`.

## Conventions

- No code comments; keep functions small and names descriptive.
- User-facing text (UI, prompts, errors shown to operators) is Ukrainian.
- Config only via env vars in `app/config.py`; mirror new vars in `docker-compose.yml`, `.env.example`, README.
- Schema is created by `app/init_db.py` (`create_all`); KB is seeded from `app/knowledge_base/articles.json`.
- Add a test in `tests/` for every change to state transitions, dedup, or sending.
