# Standard library
import logging
import os
from contextlib import asynccontextmanager

# Third-party
import firebase_admin
import socketio
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from firebase_admin import credentials

# FastAPI / Starlette
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.applications import Starlette
from starlette.middleware import Middleware

# App
from app.config import settings
from app.database import SessionLocal
from app.middleware.exception_logging import ExceptionLoggingMiddleware
from app.routes import (
    additional_expenses, admin, auth, balance_report, bookings, broadcasts,
    categories, chat, common, deactivation_reason, dispute_type, document_type, items, measurements,
    notifications, referral, regions, suppliers, users, webhooks,
)
from app.routes.v2 import bookings as bookings_v2, items as items_v2, referral as referral_v2
from app.routes.socketio_server import app as socket_app
from app.utils.tasks import auto_release_held_transfers, capture_order_payments, check_user_subscriptions, send_account_completion_reminders
from seeders import (
    account_completion_reminder_seeder,
    agreement_notes_notification_seeder,
    approved_seeder, broadcast_notification_seeder,
    cancel_dispute_notification_seeder, dispute_seeder,
    email_seeder, expired_seeder,
    inappropriate_content_seeder, item_moderation_result_seeder,
    region_translations_seeder, revoked_seeder,
    review_request_seeder,
    seed_country_data, seed_nationality_data, seed_user_data,
    service_state_notification_seeder, settings_seeder,
    subscription_email, supplier_seeder, weekday_seeder,
)

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ──────────────────────────────────────────────
# Lifespan — runs seeders and scheduler on startup
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    db = SessionLocal()
    try:
        seed_user_data(db)
        seed_country_data(db)
        seed_nationality_data(db)
        settings_seeder(db)
        region_translations_seeder(db)
        weekday_seeder(db)
        email_seeder(db)
        dispute_seeder(db)
        supplier_seeder(db)
        approved_seeder(db)
        expired_seeder(db)
        revoked_seeder(db)
        subscription_email(db)
        inappropriate_content_seeder(db)
        item_moderation_result_seeder(db)
        account_completion_reminder_seeder(db)
        broadcast_notification_seeder(db)
        review_request_seeder(db)
        cancel_dispute_notification_seeder(db)
        service_state_notification_seeder(db)
        agreement_notes_notification_seeder(db)
        scheduler.start()
        yield
        scheduler.shutdown()
    finally:
        db.close()


# ──────────────────────────────────────────────
# App & scheduler initialization
# ──────────────────────────────────────────────
app = FastAPI(lifespan=lifespan)
scheduler = BackgroundScheduler()

# Scheduled background jobs. The job functions live in app.utils.tasks and
# are only wired up when ``settings.ENABLE_SCHEDULER`` is true so unit tests
# and local runs don't fire them unexpectedly.
if settings.ENABLE_SCHEDULER:
    scheduler.add_job(capture_order_payments, trigger=CronTrigger(minute=0))
    scheduler.add_job(check_user_subscriptions, trigger=CronTrigger(hour=0, minute=0))
    scheduler.add_job(auto_release_held_transfers, trigger=CronTrigger(minute="*/15"))
    scheduler.add_job(send_account_completion_reminders, trigger=CronTrigger(hour="*/6"))

# Socket.IO server
sio = socketio.AsyncServer(cors_allowed_origins="*", async_mode='asgi')

# Firebase initialization for push notifications
cred = credentials.Certificate(os.path.join(BASE_DIR, settings.FIREBASE_CREDENTIALS_FILE))
firebase_admin.initialize_app(cred)


# ──────────────────────────────────────────────
# Middleware
# ──────────────────────────────────────────────
# CORS. ``allow_credentials=True`` and a ``*`` origin cannot be combined per
# the CORS spec; pull the allowlist from settings and only enable credentials
# when at least one explicit origin is configured.
_allowed_origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["*"],
    allow_credentials=bool(_allowed_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

if settings.ENABLE_EXCEPTION_LOGGING:
    app.add_middleware(ExceptionLoggingMiddleware)


# ──────────────────────────────────────────────
# Routers
# ──────────────────────────────────────────────
app.include_router(admin.router, prefix='/api')
app.include_router(notifications.router, prefix='/api')
app.include_router(document_type.router, prefix='/api')
app.include_router(dispute_type.router, prefix='/api')
app.include_router(deactivation_reason.router, prefix='/api')
app.include_router(measurements.router, prefix='/api')
app.include_router(regions.router, prefix='/api')
app.include_router(auth.router, prefix='/api')
app.include_router(users.router, prefix='/api')
app.include_router(categories.router, prefix='/api')
app.include_router(common.router, prefix='/api')
app.include_router(items.router, prefix='/api')
app.include_router(suppliers.router, prefix='/api')
app.include_router(bookings.router, prefix='/api')
app.include_router(referral.router, prefix='/api')
app.include_router(chat.router, prefix='/api')
app.include_router(webhooks.router, prefix='/api')
app.include_router(broadcasts.router, prefix='/api')
app.include_router(additional_expenses.router, prefix='/api')
app.include_router(balance_report.router, prefix='/api')

# v2 API
app.include_router(items_v2.router, prefix='/api/v2')
app.include_router(bookings_v2.router, prefix='/api/v2')
app.include_router(referral_v2.router, prefix='/api/v2')


# ──────────────────────────────────────────────
# Static files
# ──────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=settings.FILE_DIR_PATH), name="static")
app.mount("/assets", StaticFiles(directory="static_files"), name="assets")


# ──────────────────────────────────────────────
# Stripe callback routes
# ──────────────────────────────────────────────

@app.get("/stripe_reauth", tags=["Base"])
def stripe_reauth():
    return


@app.get("/stripe_return", tags=["Base"])
def stripe_return():
    return


@app.get("/stripe_webhook", tags=["Base"])
def stripe_webhook_return():
    return


# ──────────────────────────────────────────────
# Exception handlers
# ──────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return Pydantic validation errors in the app's standard error format.

    If the raised ValueError carried a translation key (e.g. password_too_short)
    we resolve it against the caller's ``selected_language`` cookie before
    returning. Unknown keys / non-key messages are passed through unchanged.
    """
    from app.helpers.messages import messages

    first_error = exc.errors()[0] if exc.errors() else {}
    msg = first_error.get("msg", "Invalid value")
    if msg.startswith("Value error, "):
        msg = msg.replace("Value error, ", "", 1)
    # Prefer the ?lang= query param (used by email-link endpoints like
    # /reset-password/ where no cookie is set yet); fall back to the
    # selected_language cookie for the standard in-app flow.
    selected_language = (
        request.query_params.get("lang")
        or request.cookies.get("selected_language")
        or "en"
    )
    lang_messages = messages.get(selected_language) or messages.get("en") or {}
    msg = lang_messages.get(msg, msg)
    return JSONResponse(
        status_code=200,
        content={"message": msg, "status": False, "statusCode": 403},
    )


from app.utils.stripe_client import StripeCountryNotSupported


@app.exception_handler(StripeCountryNotSupported)
async def stripe_country_not_supported_handler(request: Request, exc: StripeCountryNotSupported):
    """Any Stripe-key resolver call for a supplier whose country isn't
    served by MEIAPPLI Brazil or Switzerland raises this. Translate to
    HTTP 400 with the localised ``stripe_country_not_supported`` message."""
    from app.helpers.messages import messages

    selected_language = request.cookies.get("selected_language", "en")
    lang_messages = messages.get(selected_language) or messages.get("en") or {}
    return JSONResponse(
        status_code=400,
        content={
            "message": lang_messages.get("stripe_country_not_supported"),
            "status": False,
            "statusCode": 400,
            "data": [],
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions — logs and returns a generic 500."""
    logger.error("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred. Please try again later."},
    )


# ──────────────────────────────────────────────
# Starlette wrapper — combines FastAPI + Socket.IO
# ──────────────────────────────────────────────
app_with_socket = Starlette(
    routes=app.routes,
    middleware=app.user_middleware,
    lifespan=lifespan,
)

# Re-register exception handlers on the Starlette wrapper so they apply to
# requests matched at the Starlette routing level (not just mounted FastAPI).
app_with_socket.add_exception_handler(RequestValidationError, validation_exception_handler)
app_with_socket.add_exception_handler(StripeCountryNotSupported, stripe_country_not_supported_handler)
app_with_socket.add_exception_handler(Exception, global_exception_handler)

app_with_socket.mount('/api', app)
app_with_socket.mount('/', socket_app)


if __name__ == "__main__":
    uvicorn.run(app_with_socket, host="0.0.0.0", port=8000)
