# Support Draft Assistant

Бекенд-сервіс, який приймає звернення з хелпдеску через вебхук, знаходить релевантні статті в базі знань, генерує чернетку відповіді за допомогою LLM (Claude), дає оператору підтвердити або відхилити її у веб-інтерфейсі та надсилає підтверджену відповідь клієнту через API хелпдеску.

## Стек

- Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg
- PostgreSQL 16 (дані, черга задач, повнотекстовий пошук по базі знань, глобальний rate limiter)
- Anthropic Python SDK, модель `claude-opus-5-5`
- Jinja2 для інтерфейсу оператора
- Docker Compose

## Швидкий старт

Потрібні Docker і Docker Compose v2.

```bash
git clone <repo-url> && cd <repo>
cp .env.example .env
docker compose up --build
```

Ключ API не обов'язковий. Щоб генерувати справжні відповіді, вкажіть у `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Після запуску:

| Що | URL |
|---|---|
| Інтерфейс оператора | http://localhost:8000/tickets |
| Health check | http://localhost:8000/health → `OK` |
| Вебхук | `POST http://localhost:8000/webhooks/helpdesk` |
| База знань | http://localhost:8000/kb |
| Swagger | http://localhost:8000/docs |
| Мок хелпдеску (відповіді клієнтам, симуляція) | http://localhost:8001 |

## Як перевірити

1. Відкрийте http://localhost:8001 і натисніть «Надіслати 5 звернень»: мок надішле 5 подій, кожну двічі одночасно.
2. Відкрийте http://localhost:8000/tickets: з'явиться 5 звернень (дублікати відкинуто). За 5–30 секунд з'являться чернетки.
3. Відкрийте звернення, за потреби відредагуйте текст і натисніть «Підтвердити й надіслати» або «Відхилити».
4. На http://localhost:8001 з'явиться відповідь клієнту. Мок навмисно повертає 503 (20%) і відповідає повільно (10%). Сервіс повторює надсилання, а лічильник «повторних доставок клієнту» лишається 0.

Вручну:

```bash
curl -X POST http://localhost:8000/webhooks/helpdesk \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"evt_8f2a","ticket_id":"T-1042","customer_email":"user@example.com","message":"Не можу експортувати звіт у PDF","reply_url":"https://helpdesk.example.com/tickets/T-1042/replies"}'

curl -X POST "http://localhost:8001/simulate?count=500&copies=2"
```

`reply_url` з доменом `https://helpdesk.example.com` локально переписується на мок (`REPLY_URL_REWRITE_FROM` / `REPLY_URL_REWRITE_TO`).

## Тести

```bash
docker compose exec web python -m pytest -q
```

Тести використовують окрему БД `support_test` у тому ж контейнері Postgres.

## Архітектура

```
helpdesk ──POST /webhooks/helpdesk──▶ web (FastAPI) ──INSERT ... ON CONFLICT DO NOTHING──▶ PostgreSQL
                                                                                          │
                     worker: generation loop ◀── claim (FOR UPDATE SKIP LOCKED) ──────────┤
                        ├─ пошук статей (tsvector)                                        │
                        ├─ rate limiter (60/хв, спільний через Postgres)                  │
                        └─ LLM → draft_ready ─────────────────────────────────────────────┤
operator UI ── approve / reject / regenerate ─────────────────────────────────────────────┤
                     worker: sending loop ◀── claim ──────────────────────────────────────┘
                        └─ POST reply_url з Idempotency-Key → sent
```

Сервіси в `docker-compose.yml`:

- `db`: PostgreSQL.
- `migrate`: одноразово створює схему і завантажує базу знань з `app/knowledge_base/articles.json`.
- `web`: вебхук, інтерфейс оператора, JSON API, база знань.
- `worker`: фонова генерація чернеток і надсилання відповідей.
- `helpdesk-mock`: мок хелпдеску.

### Статуси звернення

```
new → generating → draft_ready → approved → sending → sent
          │              │                     │
          ▼              ▼                     ▼
  generation_failed   rejected            send_failed
```

Зі статусів `draft_ready`, `rejected`, `generation_failed` можна запустити повторну генерацію, із `send_failed` — повторне надсилання.

## Як виконано вимоги

**Відповідь вебхука до 2 секунд.** Вебхук лише валідує тіло і робить один `INSERT`. LLM і надсилання виконує окремий процес `worker`, тож час відповіді не залежить від LLM.

**Повтори й одночасні дублікати.** `event_id` має унікальний індекс, вставка йде через `INSERT ... ON CONFLICT DO NOTHING`. Повтор отримує `200 {"status":"duplicate"}`, тож хелпдеск перестає ретраїти. Тест надсилає 20 однакових подій паралельно і перевіряє, що створено рівно одне звернення.

**Звернення не губляться.** Звернення записується в Postgres до відповіді `200`. Черга теж живе в Postgres. Воркер забирає задачі через `SELECT ... FOR UPDATE SKIP LOCKED` з орендою (`locked_until` + `lease_token`). Якщо воркер падає, після закінчення оренди задачу забирає інший. Завершити задачу може лише власник актуальної оренди.

**Пік 500 звернень за 10 хвилин при ліміті 60 запитів/хв.** Глобальний ліміт реалізовано через ковзне вікно в таблиці `llm_calls` під `pg_advisory_xact_lock`, тому він спільний для всіх воркерів. 500 звернень обробляються приблизно за 9 хвилин. Генерації виконуються паралельно (`GENERATION_CONCURRENCY=30`), бо один виклик триває 5–30 секунд. Тимчасові помилки LLM (429, 5xx, мережа) повторюються з експоненційною затримкою з урахуванням `retry-after`.

**Клієнт не отримує ту саму відповідь двічі.**
- Підтвердити звернення можна лише один раз: атомарний `UPDATE ... WHERE status='draft_ready'`. Подвійний клік чи двоє операторів не створять другої відправки.
- Під час підтвердження генерується `idempotency_key`. Він передається в заголовку `Idempotency-Key` при кожній спробі, і текст між спробами не змінюється.
- При 5xx, таймауті чи мережевій помилці надсилання повторюється з тим самим ключем, тому хелпдеск може відкинути дубль. Мок так і робить, а його лічильник `duplicate_deliveries` показує, чи дійшов дубль до клієнта.
- 4xx (крім 408/425/429) вважаються постійною помилкою (`send_failed`). Оператор може повторити надсилання вручну.

**Режим без API-ключа.** Якщо `ANTHROPIC_API_KEY` порожній, сервіс усе одно робить справжній запит до `api.anthropic.com` (отримує 401), а потім повертає статичну відповідь. Загальна затримка випадкова, 5–30 секунд. Rate limiter, черга й решта логіки працюють так само. У картці звернення видно режим `stub` чи `live`.

**Пошук по базі знань.** У базі 10 статей про вигаданий продукт ReportFlow (`app/knowledge_base/articles.json`, згенеровано AI). Для пошуку використовується Postgres full-text search: згенерована колонка `tsvector` з вагами (заголовок і ключові слова — A, текст — B) та GIN-індекс. Запит будується з префіксів слів, щоб працювати з українською морфологією без словника. У промпт передаються топ-3 статті, а посилання на них гарантовано є в чернетці: якщо модель їх не вставила, сервіс додає їх сам.

**LLM.** Модель `claude-opus-5-5` з `effort: low`. Увімкнено серверний fallback на випадок відмови моделі (`server-side-fallback-2026-07-01`, `fallbacks: "default"`). Текст звернення передається як дані в окремому тегу, а системний промпт забороняє виконувати інструкції з нього.

## Конфігурація

Усі параметри задаються змінними оточення (див. `app/config.py`). Основні:

| Змінна | За замовчуванням | Опис |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Ключ API; порожній = режим статичних відповідей |
| `LLM_MODEL` | `claude-opus-5-5` | Модель |
| `LLM_EFFORT` | `low` | Рівень effort |
| `LLM_RATE_LIMIT_PER_MINUTE` | `60` | Глобальний ліміт викликів LLM |
| `GENERATION_CONCURRENCY` | `30` | Паралельні генерації на воркер |
| `SEND_CONCURRENCY` | `10` | Паралельні надсилання на воркер |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Базовий URL для посилань на статті |
| `OPERATOR_USERNAME` / `OPERATOR_PASSWORD` | `operator` / — | Basic Auth для інтерфейсу оператора (вимкнено, якщо пароль порожній) |
| `REPLY_URL_REWRITE_FROM` / `REPLY_URL_REWRITE_TO` | `https://helpdesk.example.com` → `http://helpdesk-mock:8001` | Підміна адреси хелпдеску на мок |
| `MOCK_FAIL_RATE` / `MOCK_SLOW_RATE` / `MOCK_SLOW_SECONDS` | `0.2` / `0.1` / `15` | Поведінка мока |

## JSON API оператора

- `GET /api/tickets?status_filter=draft_ready`
- `GET /api/tickets/{id}`
- `POST /api/tickets/{id}/approve` (тіло `{"message": "..."}` необов'язкове; без нього надсилається чернетка)
- `POST /api/tickets/{id}/reject`
- `POST /api/tickets/{id}/regenerate`
- `POST /api/tickets/{id}/retry-send`

Якщо статус звернення не дозволяє дію, повертається `409`.

## Деплой

Сервіс розгортається на будь-якому хостингу з Docker Compose (VPS) або як окремі сервіси `web` і `worker` з керованим Postgres. На хостингу треба задати:

- `PUBLIC_BASE_URL`: публічна адреса сервісу (для посилань на статті);
- `OPERATOR_PASSWORD`: щоб закрити інтерфейс оператора;
- `REPLY_URL_REWRITE_FROM` / `REPLY_URL_REWRITE_TO`: порожні, якщо використовується справжній хелпдеск.

`Dockerfile` слухає `$PORT` (за замовчуванням 8000) і довіряє `X-Forwarded-*`, тому підходить для Render, Railway, Fly.io тощо.

## Структура

```
app/
  main.py          вебхук, інтерфейс оператора, JSON API, KB
  tickets.py       стан звернень, атомарні переходи, claim з орендою
  worker.py        фонові цикли generation / sending
  generation.py    пошук статей + LLM → чернетка
  sending.py       надсилання відповіді з Idempotency-Key і ретраями
  llm.py           клієнт Anthropic, режим без ключа
  rate_limiter.py  глобальний ліміт 60/хв через Postgres
  kb.py            завантаження і пошук по базі знань
  models.py        моделі SQLAlchemy
  init_db.py       створення схеми і сидинг KB
  knowledge_base/articles.json
  templates/       Jinja2-шаблони
helpdesk_mock/     мок хелпдеску
tests/             pytest
```
