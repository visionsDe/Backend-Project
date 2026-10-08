"""Apply state changes to SupplierSubscription from a verified webhook event.

Enqueued as a FastAPI ``BackgroundTask`` by the webhook endpoint after the
audit row is persisted. ``process_event(event_id)``:

  1. Opens its own DB session.
  2. Atomically claims the audit row (``SELECT … FOR UPDATE`` on
     outcome='received').
  3. Resolves the underlying ``SupplierSubscription`` by
     ``original_transaction_id`` (Apple) or ``purchase_token`` (Google).
  4. Decides what to apply based on ``event_type``:
        - terminal types (refund / revoke) → ``status = 'inactive'`` unconditionally
        - informational types (price-change confirmed, deferred, etc.) → no state change
        - Apple non-terminal → re-decode the inner ``signed_transaction_info`` JWS
          (already signature-verified by the webhook handler) and apply
          ``expiry_date`` + ``status`` straight from the signed claim.
        - Google non-terminal → derive status from the notificationType — RTDN
          tells us exactly what happened (renewed, cancelled, on-hold, grace);
          no Play Developer API call is made.
  5. Stamps the row with ``last_event_id`` / ``last_event_at`` / ``provider``.
  6. Marks the audit row ``outcome='processed'`` (or ``'unmatched'`` /
     ``'handler_error'``).

The CRON safety-net (``app.utils.tasks.check_user_subscriptions``) keeps
running for rows that haven't seen a webhook recently — it independently
flips ``status`` to inactive when a row's chain has lapsed and backfills
``original_transaction_id`` for older iOS rows so webhooks can route to
them. It does NOT refresh ``expiry_date`` on store-bought rows; nothing
in this codebase reads ``expiry_date`` for those rows
(every read is gated on ``purchase_response IS NULL`` /
``personal_subscription = False`` — the admin-grant path).

Never throws — exceptions become ``outcome='handler_error'`` with the message
recorded so admins can see what went wrong without the endpoint paying for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal
from app.utils.apple_webhook import get_verifier as _get_apple_verifier
from app.utils.logging import setup_logger

# Webhook-event processing logs (unmatched events, handler errors, per-event
# state outcomes) go to logs/app.log via the shared rotating file handler.
logger = setup_logger()


# ── Per-provider terminal events: force inactive, skip any further parsing ─
# These mean access must end NOW regardless of what any read-API currently
# says (Apple's verifyReceipt may still show a grace-period entry after a
# refund — we don't want to leave the user entitled).
_APPLE_TERMINAL_TYPES = frozenset({"REFUND", "REVOKE"})
_GOOGLE_TERMINAL_TYPES = frozenset({"SUBSCRIPTION_REVOKED"})

# Informational / heartbeat events — no state change at all.
_APPLE_NOOP_TYPES = frozenset({"TEST", "PRICE_INCREASE", "RENEWAL_EXTENDED"})
_GOOGLE_NOOP_TYPES = frozenset({
    "SUBSCRIPTION_PRICE_CHANGE_CONFIRMED",
    "SUBSCRIPTION_DEFERRED",
    "SUBSCRIPTION_PAUSE_SCHEDULE_CHANGED",
})

# Google: the user is paying / entitled. Set active + auto_renewing=True.
_GOOGLE_ACTIVE_TYPES = frozenset({
    "SUBSCRIPTION_PURCHASED",
    "SUBSCRIPTION_RECOVERED",
    "SUBSCRIPTION_RENEWED",
    "SUBSCRIPTION_RESTARTED",
    "SUBSCRIPTION_IN_GRACE_PERIOD",
})

# Google: user-initiated cancellation. Still entitled until expiry; only
# auto_renewing flips. The matching SUBSCRIPTION_EXPIRED arrives later and
# transitions status to inactive.
_GOOGLE_CANCEL_TYPES = frozenset({
    "SUBSCRIPTION_CANCELED",
    "SUBSCRIPTION_PENDING_PURCHASE_CANCELED",
})

# Google: access ends immediately even though the user hasn't refunded —
# payment is unrecoverable for the moment.
_GOOGLE_INACTIVE_TYPES = frozenset({
    "SUBSCRIPTION_ON_HOLD",
    "SUBSCRIPTION_PAUSED",
    "SUBSCRIPTION_EXPIRED",
})


def process_event(event_id: int) -> None:
    """Background entry point: take a freshly-inserted ``webhook_events`` row id
    and apply its state change to ``SupplierSubscription``.

    Safe to call from FastAPI ``BackgroundTasks`` or from a CRON job.
    """
    db: Session = SessionLocal()
    try:
        _process_event_inner(db, event_id)
    except Exception:
        # Final safety net — log and move on so the request that scheduled us
        # isn't affected. The audit row is left as-is for manual review.
        logger.exception("Unhandled error processing webhook event %s", event_id)
    finally:
        db.close()


def _process_event_inner(db: Session, event_id: int) -> None:
    # 1. Atomic claim — re-query inside this session's transaction with
    #    SELECT … FOR UPDATE so two workers can't both pick up the same row.
    event = (
        db.query(models.WebhookEvent)
        .filter(
            models.WebhookEvent.id == event_id,
            models.WebhookEvent.outcome == "received",
        )
        .with_for_update()
        .first()
    )
    if event is None:
        # Already processed by another worker, or audit row was inserted with
        # a non-received outcome (signature_fail). Nothing to do.
        return

    # 2. Resolve the SupplierSubscription that this notification refers to.
    try:
        subscription = _find_subscription(db, event)
    except Exception as exc:
        _mark_audit(event, "handler_error", error_message=f"Match lookup failed: {exc}")
        db.commit()
        logger.exception("webhook_event %s: subscription lookup failed", event_id)
        return

    if subscription is None:
        _mark_audit(event, "unmatched",
                    error_message="No SupplierSubscription matched this event")
        db.commit()
        logger.warning(
            "webhook_event %s (provider=%s type=%s) unmatched — "
            "the weekly check_user_subscriptions CRON will backfill the "
            "identifier for old rows on its next pass",
            event_id, event.provider, event.event_type,
        )
        return

    event.supplier_subscription_id = subscription.id

    # 3. Decide what to apply based on event_type.
    try:
        _dispatch(event, subscription)
    except Exception as exc:
        _mark_audit(event, "handler_error", error_message=str(exc))
        db.commit()
        logger.exception("webhook_event %s: dispatch failed", event_id)
        return

    # 4. Stamp the row + audit.
    subscription.provider = subscription.provider or event.provider
    subscription.last_event_id = f"{event.provider}:{event.event_id}"
    subscription.last_event_at = datetime.now()
    if event.original_transaction_id and not subscription.original_transaction_id:
        subscription.original_transaction_id = event.original_transaction_id

    _mark_audit(event, "processed")
    db.commit()
    logger.info(
        "webhook_event %s processed: subscription_id=%s status=%s",
        event_id, subscription.id, subscription.status,
    )


# ─────────────────────────────────────────────────────────────────────
# Matching
# ─────────────────────────────────────────────────────────────────────
def _find_subscription(db: Session, event) -> Optional[models.SupplierSubscription]:
    """Resolve a SupplierSubscription from the webhook event's identifiers.

    Match order:
      Apple  → original_transaction_id
      Google → purchase_token

    Rows that predate the webhook columns (no original_transaction_id) will
    not match here and show up in the audit log with outcome='unmatched'.
    """
    if event.provider == "apple":
        if event.original_transaction_id:
            return (
                db.query(models.SupplierSubscription)
                .filter(models.SupplierSubscription.original_transaction_id == event.original_transaction_id)
                .order_by(models.SupplierSubscription.id.desc())
                .first()
            )
        return None

    if event.provider == "google":
        if event.purchase_token:
            return (
                db.query(models.SupplierSubscription)
                .filter(models.SupplierSubscription.purchase_token == event.purchase_token)
                .order_by(models.SupplierSubscription.id.desc())
                .first()
            )
        return None

    return None


# ─────────────────────────────────────────────────────────────────────
# State dispatch
# ─────────────────────────────────────────────────────────────────────
def _dispatch(event, subscription: models.SupplierSubscription) -> None:
    """Apply the right state change for this event type."""
    event_type = (event.event_type or "").upper()

    if event.provider == "apple":
        if event_type in _APPLE_NOOP_TYPES:
            return
        if event_type in _APPLE_TERMINAL_TYPES:
            _terminate(subscription)
            return
        _apply_apple_payload(event, subscription)
        return

    if event.provider == "google":
        if event_type in _GOOGLE_NOOP_TYPES:
            return
        if event_type in _GOOGLE_TERMINAL_TYPES:
            _terminate(subscription)
            return
        _apply_google_event(event_type, subscription)
        return

    logger.warning("Unknown provider on event %s: %r", event.id, event.provider)


def _terminate(subscription: models.SupplierSubscription) -> None:
    """Force end-of-access — refund/revoke. Also cascade the referral-point cancellation."""
    subscription.status = "inactive"
    subscription.auto_renewing = False
    session = _session_for(subscription)
    if session is not None:
        try:
            from app.utils.referral_points import cancel_for_subscription
            deducted, shortfall = cancel_for_subscription(
                session,
                subscription_id=subscription.id,
                reason='store_refund_or_revoke',
            )
            if shortfall:
                logger.warning(
                    "supplier_subscription %s revoked but referrer balance couldn't cover "
                    "%s credited point(s) — no deduction applied",
                    subscription.id, shortfall,
                )
            if deducted:
                logger.info(
                    "supplier_subscription %s: deducted %s referral point(s) after refund/revoke",
                    subscription.id, deducted,
                )
        except Exception:
            logger.exception("Referral cascade failed for subscription %s", subscription.id)


def _session_for(instance) -> Optional[Session]:
    """Return the SQLAlchemy session backing this instance, if any."""
    from sqlalchemy import inspect as _inspect
    try:
        return _inspect(instance).session
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────
# Apple — parse the verified JWS payload
# ─────────────────────────────────────────────────────────────────────
def _apply_apple_payload(event, subscription: models.SupplierSubscription) -> None:
    """Set status + expiry_date directly from the JWS data Apple already sent.

    The notification body's ``data.signed_transaction_info`` is itself a JWS
    Apple signed. Re-decoding it (a few μs of in-process work, no network)
    gives us the authoritative ``expiresDate`` and renewal info without
    going back out to App Store Server APIs.

    We deliberately do **not** call the legacy ``verifyReceipt`` path here —
    that's left to the CRON safety net for rows that haven't seen any
    webhook traffic recently.
    """
    raw = event.raw_payload or {}
    data = raw.get("data") or {}
    # app-store-server-library declares these fields in camelCase, so
    # attrs.asdict (used by webhooks._decoded_payload_to_dict) emits camelCase
    # keys. The snake_case lookup is a defensive fallback in case the library
    # ever switches conventions.
    signed_transaction = data.get("signedTransactionInfo") or data.get("signed_transaction_info")
    signed_renewal = data.get("signedRenewalInfo") or data.get("signed_renewal_info")

    verifier = _get_apple_verifier()
    if verifier is None:
        logger.warning(
            "Apple verifier unavailable; cannot apply state from event %s — "
            "row left unchanged so the CRON fallback can pick it up later",
            event.id,
        )
        return

    expires_date = None
    if signed_transaction:
        try:
            txn = verifier.verify_and_decode_signed_transaction(signed_transaction)
            ms = (
                getattr(txn, "expiresDate", None)
                or getattr(txn, "expires_date", None)
            )
            if ms:
                # Apple sends ms since epoch.
                expires_date = datetime.fromtimestamp(int(ms) / 1000)
        except Exception as exc:
            logger.exception(
                "Apple signed_transaction_info decode failed on event %s: %s",
                event.id, exc,
            )

    auto_renew = None
    if signed_renewal:
        try:
            renewal = verifier.verify_and_decode_renewal_info(signed_renewal)
            ars = (
                getattr(renewal, "autoRenewStatus", None)
                if getattr(renewal, "autoRenewStatus", None) is not None
                else getattr(renewal, "auto_renew_status", None)
            )
            if ars is not None:
                # autoRenewStatus is 0 / 1, sometimes wrapped in an Enum.
                ars_val = getattr(ars, "value", ars)
                auto_renew = bool(int(ars_val))
        except Exception as exc:
            logger.exception(
                "Apple signed_renewal_info decode failed on event %s: %s",
                event.id, exc,
            )

    if expires_date is not None:
        # Don't persist expires_date on this row — the column is only read for
        # organisation/admin-granted subscriptions (purchase_response IS NULL,
        # personal_subscription=False). Store-bought rows never have their
        # expiry_date column consumed. We do still USE the JWS expiry here:
        # an EXPIRED notification carries a past expiresDate and that's how we
        # decide to flip status to inactive without a separate type-check.
        subscription.status = "active" if expires_date > datetime.now() else "inactive"
    if auto_renew is not None:
        subscription.auto_renewing = auto_renew


# ─────────────────────────────────────────────────────────────────────
# Google — derive status from notificationType
# ─────────────────────────────────────────────────────────────────────
def _apply_google_event(event_type: str, subscription: models.SupplierSubscription) -> None:
    """Apply state purely from the RTDN notificationType.

    Google's notification body does not include the new ``expiryTimeMillis``
    — getting it would cost a ``purchases.subscriptions.get`` round-trip.
    We deliberately skip that call: ``subscription.expiry_date`` is only
    consumed for organisation/admin-granted rows in this codebase
    (see ``app.utils.tasks.check_user_subscriptions`` — its expiry-driven
    branches sit inside ``if not subscription.purchase_response``), so
    store-bought rows wouldn't gain anything from a refreshed value.
    Status, which is what actually gates entitlement, is captured exactly
    by the notification type.
    """
    if event_type in _GOOGLE_ACTIVE_TYPES:
        subscription.status = "active"
        subscription.auto_renewing = True
        return
    if event_type in _GOOGLE_CANCEL_TYPES:
        # User cancelled but the paid window still applies. The matching
        # SUBSCRIPTION_EXPIRED arrives when the period ends and flips status.
        subscription.auto_renewing = False
        return
    if event_type in _GOOGLE_INACTIVE_TYPES:
        subscription.status = "inactive"
        return

    logger.warning(
        "Unhandled Google notification type %r on subscription %s — "
        "row left unchanged",
        event_type, subscription.id,
    )


# ─────────────────────────────────────────────────────────────────────
# Audit helpers
# ─────────────────────────────────────────────────────────────────────
def _mark_audit(event, outcome: str, *, error_message: Optional[str] = None) -> None:
    event.outcome = outcome
    event.error_message = error_message
    event.processed_at = datetime.now()
