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
