"""Apple App Store Server Notifications V2 + Google Play RTDN endpoints.

Both endpoints follow the same shape:
  1. Verify the signature (Apple JWS / Google Pub/Sub OIDC token).
  2. Persist a ``webhook_events`` audit row. The unique
     ``(provider, event_id)`` index dedups any retries automatically — an
     ``IntegrityError`` here is treated as a successful no-op.
  3. Enqueue a ``BackgroundTask`` that resolves the underlying
     ``SupplierSubscription`` and applies any state change. The dispatch
     logic lives in ``app.utils.subscription_state.process_event``.
  4. Always return 200 fast. Apple/Google retry aggressively when the
     handler is slow, and the audit row + background task pattern means we
     never need to do the heavy work inline.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal
from app.utils.apple_webhook import verify_and_decode as verify_apple
from app.utils.google_webhook import verify_and_decode as verify_google
from app.utils.logging import setup_logger
from app.utils.subscription_state import process_event

logger = setup_logger()

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


# ─────────────────────────────────────────────────────────────────────
# Apple App Store Server Notifications V2
# ─────────────────────────────────────────────────────────────────────
@router.post("/apple-app-store")
async def apple_app_store_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive an App Store Server Notification V2.

    Always returns 200 — even on signature failures we record the attempt in
    the audit log (with ``outcome='signature_fail'``) so we can spot probing
    and so Apple doesn't retry endlessly. The only way to get a 4xx/5xx out
    of here is a genuinely unexpected exception, which we want Apple to retry.
    """
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except json.JSONDecodeError:
        body = {}

    signed_payload = body.get("signedPayload") if isinstance(body, dict) else None
    if not signed_payload:
        _persist_audit_safely(
            provider="apple",
            event_id=None,
            event_type=None,
            event_subtype=None,
            outcome="signature_fail",
            signature_valid=False,
            error_message="Body missing signedPayload",
            raw_payload=body if isinstance(body, dict) else None,
        )
        return _ok()

    result = verify_apple(signed_payload)
    event_id = result.notification_uuid

    if not result.signature_valid:
        _persist_audit_safely(
            provider="apple",
            event_id=event_id,
            event_type=result.notification_type,
            event_subtype=result.notification_subtype,
            outcome="signature_fail",
            signature_valid=False,
            error_message=result.error_message,
            raw_payload={"signedPayload": signed_payload[:200] + "..."},  # truncated; full body is the JWS itself
        )
        return _ok()

    # Signature valid — write the audit row. The unique (provider, event_id)
    # constraint dedups any retry; an IntegrityError means we've already
    # processed this notification, which is itself a successful outcome.
    persisted = _persist_audit_safely(
        provider="apple",
        event_id=event_id,
        event_type=result.notification_type,
        event_subtype=result.notification_subtype,
        outcome="received",
        signature_valid=True,
        error_message=None,
        raw_payload=_decoded_payload_to_dict(result.decoded_payload),
        original_transaction_id=result.original_transaction_id,
    )
    logger.info(
        "Apple notification %s type=%s subtype=%s persisted_id=%s",
        event_id, result.notification_type, result.notification_subtype, persisted,
    )
    # Apply state asynchronously — only for fresh events we successfully persisted.
    # Duplicates (persisted is None) are already-handled.
    if persisted is not None:
        background_tasks.add_task(process_event, persisted)
        # Fan out an Apple-offer-code confirmation when the notification is an
        # OFFER_REDEEMED for an offer-code (offerType == 3).
        if (
            (result.notification_type or "").upper() == "OFFER_REDEEMED"
            and result.offer_type == 3
            and result.offer_identifier
        ):
            from app.utils.apple_offer_codes import confirm_offer_redemption

            background_tasks.add_task(
                confirm_offer_redemption,
                result.offer_identifier,
                result.app_account_token,
            )
    return _ok()


# ─────────────────────────────────────────────────────────────────────
# Google Play Real-Time Developer Notifications (Pub/Sub push)
# ─────────────────────────────────────────────────────────────────────
@router.post("/google-play")
async def google_play_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
):
    """Receive a Pub/Sub push delivery of a Google Play RTDN."""
    body_bytes = await request.body()
    try:
        envelope = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
    except json.JSONDecodeError:
        envelope = {}

    if not isinstance(envelope, dict):
        envelope = {}

    result = verify_google(authorization, envelope)

    if not result.signature_valid:
        _persist_audit_safely(
            provider="google",
            event_id=result.event_id,
            event_type=result.event_type,
            event_subtype=None,
            outcome="signature_fail",
            signature_valid=False,
            error_message=result.error_message,
            raw_payload=envelope,
            purchase_token=result.purchase_token,
        )
        return _ok()

    persisted = _persist_audit_safely(
        provider="google",
        event_id=result.event_id,
        event_type=result.event_type,
        event_subtype=None,
        outcome="received",
        signature_valid=True,
        error_message=None,
        raw_payload=result.raw_payload,
        purchase_token=result.purchase_token,
    )
    logger.info(
        "Google RTDN %s type=%s subscription=%s persisted_id=%s",
        result.event_id, result.event_type, result.subscription_id, persisted,
    )
    if persisted is not None:
        background_tasks.add_task(process_event, persisted)
    return _ok()


# ─────────────────────────────────────────────────────────────────────
# Audit-log writer
# ─────────────────────────────────────────────────────────────────────
def _persist_audit_safely(
    *,
    provider: str,
    event_id: Optional[str],
    event_type: Optional[str],
    event_subtype: Optional[str],
    outcome: str,
    signature_valid: bool,
    error_message: Optional[str],
    raw_payload: Optional[dict],
    original_transaction_id: Optional[str] = None,
    purchase_token: Optional[str] = None,
) -> Optional[int]:
    """Insert into ``webhook_events`` with full exception swallowing.

    Returns the new row id on success, ``None`` on duplicate / DB failure.
    Never raises — webhook endpoints must always return 200 even if our
    audit-log database hiccups.
    """
    # Open a dedicated session because the endpoint is async and may not have
    # a request-scoped Session injected. Keeping this self-contained also means
    # any DB error here cannot poison the request lifecycle.
    db: Session = SessionLocal()
    try:
        event = models.WebhookEvent(
            provider=provider,
            event_id=event_id or "",
            event_type=event_type,
            event_subtype=event_subtype,
            outcome=outcome,
            signature_valid=signature_valid,
            error_message=error_message,
            raw_payload=raw_payload,
            original_transaction_id=original_transaction_id,
            purchase_token=purchase_token,
        )
        db.add(event)
        db.commit()
        return event.id
    except IntegrityError:
        # Duplicate (provider, event_id) — Apple/Google retried a notification
        # we've already persisted. Treat as success.
        db.rollback()
        logger.info(
            "Duplicate webhook delivery: provider=%s event_id=%s — ignored",
            provider, event_id,
        )
        return None
    except Exception:  # pragma: no cover - DB error
        db.rollback()
        logger.exception("Failed to persist webhook audit row")
        return None
    finally:
        db.close()


def _decoded_payload_to_dict(decoded) -> Optional[dict]:
    """Best-effort JSON-isable view of the Apple decoded payload for audit logging.

    ``app-store-server-library`` uses attrs classes with ``slots=True``, so the
    objects have no ``__dict__`` and a generic JSON fallback ends up just
    storing ``repr(decoded)`` as a string. ``attrs.asdict`` walks the nested
    structure properly. The value_serializer converts Enums (notification type,
    environment, subtype) and datetimes that json doesn't natively handle.
    """
    if decoded is None:
        return None
    try:
        import attrs
        from datetime import date, datetime
        from enum import Enum

        if attrs.has(decoded):
            def _serialize(_inst, _field, value):
                if isinstance(value, Enum):
                    return value.value
                if isinstance(value, (datetime, date)):
                    return value.isoformat()
                return value

            return attrs.asdict(decoded, recurse=True, value_serializer=_serialize)
    except Exception:  # pragma: no cover - import or unexpected attrs error
        logger.exception("attrs.asdict failed for Apple payload; falling back to repr")

    # Final fallback — better than nothing if the object isn't attrs-based.
    try:
        return json.loads(json.dumps(decoded, default=lambda o: getattr(o, "__dict__", str(o))))
    except Exception:  # pragma: no cover
        return {"repr": repr(decoded)}


def _ok():
    """Standard 200 response — content is irrelevant to Apple/Google."""
    return JSONResponse(status_code=status.HTTP_200_OK, content={"received": True})
