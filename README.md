# EVE Diagnostics API

A backend service for **diagnostic test bookings** with a **simulated payment provider** and an
**idempotent payment webhook**. Built with FastAPI, PostgreSQL (async SQLAlchemy 2.0 + Alembic),
Redis (caching and rate limiting) and Docker.

- [Run it](#run-it) · [API](#api) · [Design](#design) · [Database](#database) ·
  [Assumptions](#assumptions) · [Edge cases](#edge-cases-and-where-theyre-tested) ·
  [Testing](#testing) · [Next steps](#what-id-improve-with-more-time)

## Run it

### With Docker (recommended)

```bash
cp .env.example .env            # optional: compose already sets working defaults
docker compose up --build       # api + postgres 16 + redis 7; migrations run on start
docker compose exec api python -m scripts.seed    # demo centres, tests, prices + an admin
```

- Swagger UI: <http://localhost:8000/docs> · ReDoc: <http://localhost:8000/redoc>
- Seeded admin: `admin@eve.health` / `Admin@12345` (demo only; admins can't sign up via the API)
- Create more admins: `docker compose exec api python -m scripts.create_admin you@x.com 'pw-12345' 'Name'`

### Without Docker

Needs Python 3.11+, a PostgreSQL 14+ database and (optionally) Redis. Without Redis everything
still works: the cache and rate limiter fall back gracefully (see [Design](#design)).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/eve
export WEBHOOK_TARGET_URL=http://localhost:8000/payments/webhook/   # must match the port below
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --port 8000
```

All settings are environment variables; see [.env.example](.env.example).

## API

Errors always use one envelope: `{"error": {"code", "message", "details", "request_id"}}`
(`request_id` also comes back in the `X-Request-ID` header). Lists are paginated with `limit`
(1–100, default 20) and `offset` and return `{items, total, limit, offset}`. Money is returned as a
decimal string (`"499.00"`).

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /auth/signup` | – | Register a patient (201). Duplicate email → 409 |
| `POST /auth/login` | – | Returns a JWT `{access_token, token_type, expires_in}` |
| `GET /auth/me` | user | Current user |
| `GET /centres/` | – | List centres; filters `city`, `test_id`; cached |
| `GET /centres/{id}` | – | Centre with its tests and prices; cached |
| `POST/PATCH/DELETE /centres/…` | admin | Manage centres (DELETE is a soft delete) |
| `PUT/DELETE /centres/{id}/tests/{test_id}` | admin | Set a test's price/availability at a centre |
| `GET /tests/`, `GET /tests/{id}` | – | Test catalogue (`search` filter); cached |
| `POST/PATCH/DELETE /tests/…` | admin | Manage the catalogue |
| `POST /bookings/` | user | Book `{centre_id, test_id, appointment_at}` → `PENDING` |
| `GET /bookings/`, `GET /bookings/{id}` | user | Own bookings (admin: all); `status` filter |
| `POST /bookings/{id}/cancel` | owner | Cancel a `PENDING`/`CONFIRMED` booking |
| `POST /payments/` | user | Start a simulated payment → **202** `PROCESSING` |
| `GET /payments/{id}` | owner/admin | Payment status |
| `POST /payments/webhook/` | HMAC | Provider status update; **idempotent** |
| `GET /health` | – | Database and Redis status |

### Example: book and pay

```bash
BASE=http://localhost:8000

curl -s $BASE/auth/signup -H 'Content-Type: application/json' \
  -d '{"email":"jane@example.com","password":"Passw0rd!23","full_name":"Jane Doe"}'

TOKEN=$(curl -s $BASE/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"jane@example.com","password":"Passw0rd!23"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s "$BASE/centres/?city=Bengaluru"                 # pick a centre id, then:
curl -s "$BASE/centres/<centre_id>"                     # pick a test_id from its tests

curl -s $BASE/bookings/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"centre_id":"<centre_id>","test_id":"<test_id>","appointment_at":"2026-12-01T09:00:00+05:30"}'

curl -s $BASE/payments/ -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: pay-001' \
  -d '{"booking_id":"<booking_id>","simulate_outcome":"success"}'     # 202 PROCESSING

curl -s $BASE/bookings/<booking_id> -H "Authorization: Bearer $TOKEN"  # CONFIRMED a moment later
```

`simulate_outcome` (`success` | `failure`) is optional; when omitted the provider succeeds with
probability `PAYMENT_SUCCESS_RATE` (default 0.8). It exists so tests and demos are deterministic.
`appointment_at` must carry a UTC offset and be in the future (within 90 days).

### Example: webhook (what the simulated provider sends)

```bash
BODY='{"event_id":"evt_123","payment_id":"<payment_id>","status":"SUCCESS","occurred_at":"2026-10-02T10:00:00Z"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')
curl -s $BASE/payments/webhook/ -H 'Content-Type: application/json' -H "X-Webhook-Signature: $SIG" -d "$BODY"
# -> {"event_id":"evt_123","payment_id":"…","result":"APPLIED"}
# send the same request again -> "result":"DUPLICATE" and nothing changes
```

## Design

```
routers (app/api)  →  services (app/services)  →  models (app/models)
   HTTP only            business rules +             tables, constraints
                        transactions
app/domain: enums + state machine      app/core: config, errors, security, cache, rate limit, logging
```

**Payment flow.** `POST /payments/` creates a `PROCESSING` payment and returns 202. A background
task plays the payment provider: it decides success/failure, then calls our own
`POST /payments/webhook/` with a signed event. **The webhook is the only place a payment or booking
reaches its final state**, exactly as with a real gateway.

```
client ─POST /payments/──▶ API ── payment=PROCESSING (202) ──▶ client
                            │
                            └─background─▶ provider sim ──signed webhook──▶ POST /payments/webhook/
                                                                              │ payment SUCCESS/FAILED
                                                                              └ booking CONFIRMED/FAILED
```

**Webhook idempotency** (`app/services/webhook.py`), all in one transaction:
1. Verify the HMAC-SHA256 signature of the raw body (`X-Webhook-Signature`) → 401 if wrong.
2. Lock the booking, then the payment (`SELECT … FOR UPDATE`), so events for one payment are serialised.
3. `INSERT INTO webhook_events … ON CONFLICT (event_id) DO NOTHING`. If nothing was inserted this
   `event_id` was already seen → return `200 DUPLICATE`, change nothing. The unique index is the
   guarantee; two simultaneous copies cannot both succeed.
4. If the payment is already final (a *different* event decided it), record the event as `IGNORED`.
   Otherwise apply the transition through the state machine and record `APPLIED`.

Duplicates return 200 (not an error) because real providers keep retrying on non-2xx responses.
The simulator retries failed deliveries with exponential backoff and **reuses the same
`event_id`**, so a retry after a timeout can't double-apply.

**Idempotent `POST /payments/`.** The `Idempotency-Key` header is required. Same key + same body
returns the original payment (`200`, `Idempotent-Replayed: true`); same key + different body → 422.
`payments.booking_id` is unique, so a booking can never be charged twice even under a race.

**State machine** (`app/domain/state_machine.py`, every status change goes through it):

```
Booking:  PENDING ──payment SUCCESS──▶ CONFIRMED ──cancel──▶ CANCELLED
             │──payment FAILED───▶ FAILED (final)
             └──cancel───────────▶ CANCELLED (final)
Payment:  PROCESSING ──▶ SUCCESS | FAILED   (both final)
```

**Other decisions**
- **Server-side pricing.** The booking amount is copied from the centre's current price; the client
  can't send one (extra fields are rejected). Later price changes don't alter existing bookings.
- **Authorization.** Public reads; catalogue writes are admin-only (403). Someone else's booking or
  payment returns **404**, not 403, so IDs can't be probed. Admins are never created via signup.
- **Auth.** bcrypt password hashes, short-lived JWT access tokens. Login failures are
  indistinguishable between "unknown email" and "wrong password", including timing.
- **Redis is optional.** Catalogue reads use cache-aside (5 min TTL; writes bump a version key to
  invalidate). If Redis is down the cache is skipped and the rate limiter falls back to
  per-process memory; the API keeps working and logs a warning.
- **Rate limits** (slowapi): login/signup 5/min per IP, payments 10/min per user. Webhook exempt.
- **Logging.** structlog JSON, one `request_id` per request, and every state change is logged with
  `booking_id`/`payment_id`.

## Database

```mermaid
erDiagram
    users ||--o{ bookings : makes
    users ||--o{ payments : pays
    centres ||--o{ centre_tests : offers
    diagnostic_tests ||--o{ centre_tests : "offered as"
    centres ||--o{ bookings : hosts
    diagnostic_tests ||--o{ bookings : "is booked as"
    bookings ||--o| payments : "paid by"
    payments ||--o{ webhook_events : "updated by"

    users { uuid id PK
            string email UK
            string password_hash
            enum role "PATIENT|ADMIN" }
    centres { uuid id PK
              string name
              string city
              bool is_active }
    diagnostic_tests { uuid id PK
                       string code UK
                       string name }
    centre_tests { uuid centre_id FK
                   uuid test_id FK
                   numeric price "> 0"
                   bool is_available }
    bookings { uuid id PK
               uuid user_id FK
               uuid centre_id FK
               uuid test_id FK
               timestamptz appointment_at
               numeric amount "price snapshot"
               enum status }
    payments { uuid id PK
               uuid booking_id FK,UK
               numeric amount
               enum status
               string idempotency_key
               string provider_ref UK
               enum refund_status }
    webhook_events { uuid id PK
                     string event_id UK
                     uuid payment_id FK
                     jsonb payload
                     enum result "APPLIED|IGNORED" }
```

Key constraints (all enforced by the database, not just the code):

| Constraint | Why |
|---|---|
| `centre_tests` UNIQUE(centre_id, test_id), `price > 0` | One price per test per centre, so the same test can cost different amounts at different centres |
| `bookings` partial UNIQUE (user, centre, test, appointment_at) **WHERE status IN (PENDING, CONFIRMED)** | No double-booking a slot, but failed/cancelled bookings don't block re-booking |
| `payments.booking_id` UNIQUE | A booking is never charged twice |
| `payments` UNIQUE(user_id, idempotency_key) | Idempotent `POST /payments/` |
| `webhook_events.event_id` UNIQUE | Webhook idempotency |
| All FKs `ON DELETE RESTRICT`; catalogue deletes are soft | Booking history is never orphaned |
| `NUMERIC(10,2)` money, `TIMESTAMPTZ` times | No float rounding; no timezone ambiguity |

Schema is managed by Alembic (`alembic/versions/0001_initial.py`).

## Assumptions

- **A failed payment is final.** The booking becomes `FAILED` and the user books again (the old slot
  is free again thanks to the partial unique index). Retrying payment on the same booking is out of scope.
- **Cancelling** is allowed for `PENDING`/`CONFIRMED` bookings before the appointment, and not while
  a payment is `PROCESSING`. Cancelling a paid booking marks the payment `REFUND_PENDING`; no real
  refund is simulated.
- **The webhook secret is shared** between "provider" and API (`WEBHOOK_SECRET`), as with real gateways.
- **Currency is INR**; there is no slot-capacity model (a centre can take any number of bookings per slot).
- **Background delivery uses FastAPI `BackgroundTasks`** (no extra service). If the process dies
  between accepting a payment and delivering the webhook, the payment stays `PROCESSING`.
- `simulate_outcome` is a testing aid and would not exist against a real provider.

## Edge cases and where they're tested

| Case | Behaviour | Tests |
|---|---|---|
| Invalid / expired / missing JWT | 401 | `test_auth.py` |
| Non-admin writes to the catalogue | 403 | `test_catalog.py` |
| Other user's booking / payment | 404 | `test_bookings.py`, `test_payments.py` |
| Unknown / malformed IDs | 404 / 422 | all integration suites |
| Past appointment, naive datetime, `amount` in body | 422 | `test_bookings.py` |
| Unavailable or unoffered test | 422 | `test_bookings.py` |
| Duplicate active booking | 409 | `test_bookings.py` |
| Pay for a non-`PENDING` booking, or twice | 409 | `test_payments.py` |
| Missing `Idempotency-Key`; key reused with another body | 422 | `test_payments.py` |
| Failed payment | payment `FAILED`, booking `FAILED` | `test_payments.py` |
| Bad / missing webhook signature | 401, state untouched | `test_webhook.py` |
| Duplicate webhook event | 200 `DUPLICATE`, one stored event, no state change | `test_webhook.py` |
| **Concurrent** duplicate webhooks | exactly one `APPLIED` | `test_webhook.py` |
| Conflicting later event after a final one | `IGNORED` | `test_webhook.py` |
| Webhook delivery failures | retried with the same `event_id` | `test_provider_simulator.py` |
| Cancel while paying / after appointment / twice | 409 | `test_bookings.py` |
| Rate limit exceeded | 429 | `test_rate_limit.py` |
| Redis unavailable | API keeps working (cache and limiter degrade) | whole suite passes without Redis |

## Testing

Integration tests run against a **real PostgreSQL** (row locks, partial unique indexes and
`ON CONFLICT` are part of what's being tested). They apply the real Alembic migrations once, then
truncate tables between tests. Unit tests (state machine, security, provider simulator) need no database.

```bash
docker compose exec api pytest -q          # inside compose (uses the eve_test database)

# or locally, pointing at any Postgres you can create databases on:
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/eve_test pytest -q
ruff check . && ruff format --check .
```

Redis is optional for tests; one cache-hit test is skipped when it isn't reachable.

## What I'd improve with more time

- **Durable webhook delivery**: replace `BackgroundTasks` with a transactional outbox plus a worker
  (Celery/arq), and a reconciliation job that polls providers for payments stuck in `PROCESSING`.
- **Expire stale `PENDING` bookings** (unpaid after N minutes) so slots free up.
- **Refresh tokens / token revocation**, email verification and password reset.
- **Slot capacity and centre opening hours** modelled in the database.
- **Webhook replay protection by timestamp** (reject events older than a few minutes) and secret rotation.
- **Real refunds** and a payments ledger, plus Prometheus metrics and tracing.
- CI pipeline (lint + tests on Postgres) and a coverage gate.
