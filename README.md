# Sample Marketplace Backend

Production patterns extracted from a real mobile-marketplace project, sanitized
for sharing. Not a runnable product — a curated slice of code structured as a
reference for how I organize models, routes, schemas, services, utilities,
migrations, and tests.

## Modules included

| Module | What it demonstrates | Key paths |
| --- | --- | --- |
| **1. Multi-country Stripe marketplace (SCT)** | Separate Charges & Transfers pattern, multi-region Stripe account routing, held funds, participation fees, refund with transfer reversal, dispute-blocks-transfer | `app/routes/payments.py`, `app/utils/stripe_client.py`, `app/models/transactions.py`, `app/models/bookings.py` |
| **2. Mobile subscription webhooks** | Apple Server Notifications V2 JWS verification, Google Play RTDN Pub/Sub JWT auth, idempotent event processing, state-machine dispatch | `app/routes/webhooks.py`, `app/utils/apple_webhook.py`, `app/utils/google_webhook.py`, `app/utils/subscription_state.py` |
| **3. Real-time chat** | Socket.IO server mounted alongside FastAPI, JWT auth on connect, rooms per conversation, encrypted message content with hash-sidecar search | `app/routes/socketio_server.py`, `app/routes/chat.py`, `app/models/chats.py` |
| **4. Admin dashboard patterns** | Paginated filtering, CSV export, PDF export (ReportLab), reconciliation views with CASE expressions, deactivation flow with audit | `app/routes/admin.py`, `app/schemas/admin.py` |
| **5. AI client wrapper** | Provider-agnostic client with pluggable adapters (OpenAI), token/cost pricing, rate limiting, retry with backoff, usage logging | `app/services/ai_client/`, `app/utils/moderation.py`, `app/models/ai_usage.py` |

## Project layout

```
app/
├── main.py                FastAPI app, lifespan, router wiring
├── config.py              pydantic-settings
├── database.py            SQLAlchemy session factory
├── controller/            response envelope
├── helpers/               i18n message catalog
├── models/                SQLAlchemy models (one file per domain)
├── schemas/               pydantic request/response models
├── routes/                FastAPI routers (one file per feature area)
├── services/ai_client/    provider-agnostic AI client package
└── utils/                 cross-cutting helpers (auth, crypto, webhooks, …)
alembic/                   migrations
tests/                     pytest, one or two per module
docker-compose.yml         MySQL + Redis + app
Dockerfile
```

## How to run

```bash
cp .env.example .env
# fill in STRIPE_SECRET_KEY, OPENAI_API_KEY, etc.
docker compose up --build
docker compose exec app alembic upgrade head
```

Service runs on `http://localhost:8000`, OpenAPI docs at `/docs`.

## How to run the tests

```bash
pip install -r requirements.txt
pytest
```

Tests target the pure/stateless pieces — fee math, country→platform
resolution, token-bucket rate limiter, encryption round-trip — so a
reviewer can clone and get a green run without provisioning MySQL,
Apple certs or a Stripe account.

## What's deliberately not here

- Mobile-specific endpoints (profile, uploads, deep-linking)
- Organization-membership business logic (client-specific)
- Pre-generated seed data
- All real client identifiers, bundle IDs, campaign IDs
- Real UUIDs, keys, or credentials

## Patterns worth looking at

- **Response envelope**: `app/controller/base_controller.py` — every response goes through `BaseController.success(...)` / `errorGeneral(...)` so the mobile client always gets `{status, statusCode, message, data}`.
- **Encrypted columns with search**: `app/utils/encryption.py` — Fernet for storage, SHA-256 first-char prefix for sortable hash sidecars so you can `ORDER BY name_hash` on encrypted columns.
- **Webhook idempotency**: `app/utils/subscription_state.py` — `(provider, event_id)` unique constraint plus `SELECT ... FOR UPDATE` on the audit row so two workers can't double-process.
- **Multi-country Stripe routing**: `app/utils/stripe_client.py` — selects the right Stripe account per supplier country, with fallback to a default key.
- **AI client structure**: `app/services/ai_client/` — provider adapters are swappable; pricing, retry, rate-limit, and usage logging are cross-cutting concerns separated into their own modules.
