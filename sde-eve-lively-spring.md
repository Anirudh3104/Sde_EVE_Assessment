# EVE Healthcare – Diagnostic Booking & Simulated Payments Backend

## Context
This is the EVE Healthcare SDE-intern take-home (BRD: `EVE_SDE_Intern_Hiring_Assignment_*.pdf`): a small backend service for diagnostic-test bookings with simulated payments and an idempotent payment webhook. The grading weights are code quality 20, API design 20, DB design 15, edge cases 15, tests 10, docs 10 and bonus 10. The brief says a small, well-tested design beats a large one, and the interview may ask for a live change, so every part must be simple enough to explain. The project starts from an empty folder (only the PDF is there).

**Decisions confirmed with the user**
- Stack: FastAPI + PostgreSQL (async SQLAlchemy 2.0 + asyncpg, Alembic, Pydantic v2).
- Infrastructure: the user installs Docker Desktop. docker-compose runs `api`, `db` (Postgres 16) and `redis`.
- Payments: asynchronous. `POST /payments/` returns 202 PROCESSING. A simulated provider (FastAPI BackgroundTasks) sends a signed webhook, and the webhook is the only place that finalizes state.
- Bonuses: Docker/compose, Redis caching, rate limiting, plus the cheap ones: Swagger, pagination, structured logging and tests. No Celery.
- Catalogue writes are ADMIN-only. Reads are public.
- Booking rules: amount is copied from the server-side price; appointments must be in the future; no duplicate active bookings; FAILED is final; the owner can cancel a PENDING or CONFIRMED booking before the appointment.
- **No git operations**: no `git init`, no commits, no CI workflow.

## Tech choices
`fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `pydantic>=2`, `pydantic-settings`, `email-validator`, `pyjwt`, `bcrypt`, `httpx`, `redis>=5` (asyncio), `slowapi`, `structlog`. Dev tools: `pytest`, `pytest-asyncio`, `ruff`. Python 3.11, matching the local install and the Docker image `python:3.11-slim`. Dependencies go in `requirements.txt` and `requirements-dev.txt`. `pyproject.toml` holds the ruff and pytest config.

## Project layout
```
app/
  main.py                 app factory, routers, middleware, exception handlers
  core/  config.py (Settings), security.py (bcrypt, JWT), errors.py (AppError family + handlers),
         logging.py (structlog JSON + request-id middleware), cache.py (Redis cache-aside, fail-open),
         rate_limit.py (slowapi Limiter, Redis storage), deps.py (get_db, get_current_user, require_admin)
  db/    base.py, session.py (async engine/sessionmaker)
  models/ user.py, catalog.py (Centre, DiagnosticTest, CentreTest), booking.py, payment.py (Payment, WebhookEvent)
  domain/ enums.py, state_machine.py   ← pure transition rules, unit-tested
  schemas/ auth.py, catalog.py, booking.py, payment.py, common.py (Page[T], ErrorResponse)
  services/ auth.py, catalog.py, booking.py, payment.py, webhook.py, provider_simulator.py
  api/   auth.py, centres.py, tests.py, bookings.py, payments.py
alembic/ (env.py async, versions/0001_initial.py)
scripts/ seed.py (centres, tests, prices, admin user), create_admin.py
tests/  conftest.py, unit/, integration/
Dockerfile, docker-compose.yml, entrypoint.sh (alembic upgrade head → uvicorn), .env.example, .gitignore, README.md
```
Layering: routers handle only HTTP; services hold business logic and transactions; models hold persistence. Routers never use the ORM directly.

## Database design (Postgres)
| Table | Key columns / constraints |
|---|---|
| `users` | id UUID PK, email UNIQUE (stored lower-case), password_hash, full_name, role ENUM(PATIENT, ADMIN), created_at, updated_at |
| `centres` | id UUID, name, address, city, pincode, is_active, timestamps. UNIQUE(name, city). Index on city |
| `diagnostic_tests` | id UUID, code UNIQUE (e.g. `CBC`), name, description, is_active |
| `centre_tests` | id, centre_id FK, test_id FK, price NUMERIC(10,2) CHECK > 0, is_available. UNIQUE(centre_id, test_id) |
| `bookings` | id UUID, user_id FK, centre_id FK, test_id FK, appointment_at TIMESTAMPTZ, amount NUMERIC(10,2) (price snapshot), currency default 'INR', status ENUM(PENDING, CONFIRMED, FAILED, CANCELLED), cancelled_at, timestamps. **Partial UNIQUE (user_id, centre_id, test_id, appointment_at) WHERE status IN (PENDING, CONFIRMED)**. Index (user_id, created_at) |
| `payments` | id UUID, booking_id FK **UNIQUE** (FAILED is final, so one payment per booking and never a double charge), user_id FK, amount, status ENUM(PROCESSING, SUCCESS, FAILED), idempotency_key, request_hash, provider_ref UNIQUE, failure_reason, refund_status NULL/REFUND_PENDING, timestamps. UNIQUE(user_id, idempotency_key) |
| `webhook_events` | id, event_id **UNIQUE**, payment_id FK, status, payload JSONB, signature_valid, result ENUM(APPLIED, DUPLICATE, IGNORED), received_at, processed_at |

All FKs use ON DELETE RESTRICT. Catalogue "delete" is a soft delete (`is_active=false`) so booking history is preserved. Money uses `Decimal`, never float. The ER diagram goes in the README as Mermaid.

## State machine (`app/domain/state_machine.py`)
- Booking: PENDING → CONFIRMED (payment SUCCESS) | FAILED (payment FAILED) | CANCELLED (owner). CONFIRMED → CANCELLED (owner, before the appointment; payment.refund_status is set to REFUND_PENDING). FAILED and CANCELLED are final.
- Payment: PROCESSING → SUCCESS | FAILED. Both are final.
- An invalid transition raises `InvalidStateTransition`, returned as 409.

## API (paths follow the BRD, with trailing slashes)
| Method & path | Auth | Notes |
|---|---|---|
| POST `/auth/signup` | – | Email + password (min 8 characters) + name. Returns 201 with the user. Duplicate email returns 409 |
| POST `/auth/login` | – | Returns `{access_token, token_type, expires_in}`. Rate-limited to 5/min per IP |
| GET `/auth/me` | user | |
| GET `/centres/` | – | Pagination with `limit`/`offset`, filters `city`, `test_id`. **Cached** |
| GET `/centres/{id}` | – | Includes offered tests and prices. **Cached** |
| POST / PATCH / DELETE `/centres/…` | admin | DELETE is a soft delete. Writes invalidate the cache |
| GET `/tests/`, GET `/tests/{id}`; POST / PATCH / DELETE `/tests/…` | public / admin | |
| PUT / DELETE `/centres/{id}/tests/{test_id}` | admin | Upserts price and availability |
| POST `/bookings/` | user | Body `{centre_id, test_id, appointment_at}`. Amount comes from the server. Returns 201 PENDING |
| GET `/bookings/` and `/bookings/{id}` | user | Owners see their own bookings; admins see all. Someone else's booking returns **404** (prevents ID enumeration). Filter by `status` |
| POST `/bookings/{id}/cancel` | owner | Returns 409 if the payment is PROCESSING, the booking is final, or the appointment has passed |
| POST `/payments/` | user | `Idempotency-Key` header is required. Body `{booking_id, simulate_outcome?: "success"\|"failure"}`. Returns **202** PROCESSING. Rate-limited to 10/min per user |
| GET `/payments/{id}` | owner/admin | |
| POST `/payments/webhook/` | HMAC | Header `X-Webhook-Signature` (HMAC-SHA256 of the raw body). Body `{event_id, payment_id, status, occurred_at}`. Exempt from rate limiting |
| GET `/health` | – | Checks DB and Redis |

Every error uses one envelope: `{"error": {"code", "message", "details", "request_id"}}`. Status codes: 401, 403, 404, 409, 422, 429.

## Core flows
**Create payment** (`services/payment.py`), all in one transaction:
1. If a payment with the same (user, Idempotency-Key) exists and the request hash matches, return it unchanged. A different body with the same key returns 422.
2. Lock the booking with `SELECT … FOR UPDATE`. It must be owned by the caller (404 otherwise) and be PENDING (409 otherwise).
3. Insert a Payment(PROCESSING) row. The unique booking_id constraint turns a race into a 409.
4. Commit, then schedule `provider_simulator.deliver(payment)` as a BackgroundTask.

**Provider simulator**: picks the outcome from `simulate_outcome`, otherwise at random using `PAYMENT_SUCCESS_RATE`. It waits briefly, builds an event with a fresh `event_id`, signs it with `WEBHOOK_SECRET`, and POSTs it to `WEBHOOK_TARGET_URL` (itself) via httpx. It retries 3 times with exponential backoff, **reusing the same event_id**. An optional `SIMULATE_DUPLICATE_WEBHOOKS` flag sends the event twice to demonstrate idempotency.

**Webhook** (`services/webhook.py`), all in one transaction:
1. Verify the HMAC with `hmac.compare_digest`. Failure returns 401.
2. Run `INSERT INTO webhook_events … ON CONFLICT (event_id) DO NOTHING RETURNING id`. If no row comes back, return 200 `{"result":"duplicate"}` without touching anything else.
3. Lock the payment with `FOR UPDATE`, then the booking. If the payment is already final, because a different event already decided it, record IGNORED and return 200.
4. Apply the state-machine transitions to the payment and booking, then log the transition.

The unique constraint plus row locks make this safe under concurrent duplicate deliveries. Unknown payment_id returns 404, and a malformed payload returns 422.

**Caching**: cache-aside under keys `catalog:v{n}:…` with a 5-minute TTL. Admin writes `INCR catalog:version` to invalidate. If Redis is down, the app skips the cache and logs a warning (fail open).
**Rate limiting**: slowapi with Redis storage. It is disabled in tests through a setting, except for one dedicated test.
**Logging**: structlog JSON output. A middleware sets and returns `X-Request-ID`, and every state change is logged with booking_id and payment_id.

## Execution plan (parallel subagents with disjoint file ownership, since there's no git)
- **Phase 0 – Foundation (me, sequential; this is the contract every agent builds on):** requirements files, pyproject, `core/config|errors|security|deps|logging`, `db/*`, **all models + enums + state_machine**, Alembic initial migration, `main.py` with router stubs, `schemas/common.py`, `tests/conftest.py` (test DB on the compose Postgres, per-test truncation, async httpx client, `user_token` and `admin_token` fixtures), Dockerfile, docker-compose, `.env.example`. Then confirm `docker compose up` works and the migration applies.
- **Phase 1 – Features (3 general-purpose agents in parallel, each writing only its own files):**
  - A: auth + catalogue → `schemas/auth|catalog`, `services/auth|catalog`, `api/auth|centres|tests`, `core/cache.py`, their tests.
  - B: bookings → `schemas/booking`, `services/booking`, `api/bookings`, booking tests and state-machine unit tests.
  - C: payments + webhook → `schemas/payment`, `services/payment|webhook|provider_simulator`, `api/payments`, payment and webhook tests (duplicate events, concurrent duplicates via `asyncio.gather`, bad signature, final-state no-ops).
- **Phase 2 – Integration (me):** wire in `rate_limit.py`, `scripts/seed.py` and `create_admin.py`, run the full test suite and ruff, and fix anything across modules.
- **Phase 3 – Docs + review (in parallel):** one agent writes README.md (run steps, endpoint table + curl examples, ER diagram, design decisions/assumptions, edge-case matrix, future improvements such as an outbox/durable queue instead of BackgroundTasks, refresh tokens, slot capacity, and auto-expiring stale PENDING bookings). Meanwhile I review the code for correctness and consistency.

## Edge cases covered by tests
Invalid or expired JWT (401); non-admin catalogue write (403); another user's booking or payment (404); missing or unknown centre, test or centre_test (404/422); unavailable test; appointment in the past (422); duplicate active booking (409); paying for a non-PENDING booking (409); reused Idempotency-Key with the same body (same response) or a different body (422); a second payment for one booking (409); duplicate webhook event (200 duplicate, state unchanged); conflicting late event (IGNORED); bad signature (401); cancelling while PROCESSING or after the appointment (409); failed payment → booking FAILED; malformed bodies (422); rate limit exceeded (429).

## Verification
1. Prerequisite: Docker Desktop is installed and running.
2. `docker compose up -d --build`. The entrypoint runs migrations. Then `docker compose exec api python -m scripts.seed`.
3. `docker compose exec api pytest -q` passes, and `ruff check .` is clean.
4. Manual end-to-end check in Swagger (`/docs`) or with curl: signup → login → GET /centres/ → POST /bookings/ → POST /payments/ (simulate success) → GET /bookings/{id} shows CONFIRMED. Repeat with failure → FAILED. Replay the same webhook event → `duplicate`, no state change. A second user GETs the first user's booking → 404. Seven quick logins → 429. Stop Redis → catalogue still works (cache fails open).
