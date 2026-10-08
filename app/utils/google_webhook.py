"""Google Play Real-Time Developer Notifications (RTDN) — Pub/Sub push verification.

Pub/Sub push delivery posts a JSON envelope to our endpoint with shape::

    {
      "message": {
        "data": "<base64-encoded JSON DeveloperNotification>",
        "messageId": "<unique id, used for idempotency>",
        "publishTime": "<RFC 3339 timestamp>",
        "attributes": {...}
      },
      "subscription": "projects/.../subscriptions/..."
    }

Pub/Sub also includes an ``Authorization: Bearer <jwt>`` header — an OIDC
identity token signed by Google. We verify that JWT against Google's public
keys and check the audience / issuer match. Without that check, anyone can
POST a fake DeveloperNotification and trigger renewals or cancellations.

Once the OIDC token is verified, the notification body itself is the
authoritative state signal. ``app.utils.subscription_state.process_event``
derives ``status`` straight from ``notificationType`` (renewed / cancelled
/ on-hold / grace / expired / revoked) and does NOT call back to the Play
Developer API — that's reserved for the CRON safety-net path that
reconciles rows which haven't seen webhook traffic recently.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.utils.logging import setup_logger

logger = setup_logger()

# google.oauth2.id_token is provided by google-auth (already in requirements.txt).
try:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token
    _LIB_AVAILABLE = True
except ImportError:  # pragma: no cover
    google_requests = None  # type: ignore
    google_id_token = None  # type: ignore
    _LIB_AVAILABLE = False

# Pub/Sub notification types — keep the set in sync with Play Console.
GOOGLE_NOTIFICATION_TYPES = {
    1: "SUBSCRIPTION_RECOVERED",
    2: "SUBSCRIPTION_RENEWED",
    3: "SUBSCRIPTION_CANCELED",
    4: "SUBSCRIPTION_PURCHASED",
    5: "SUBSCRIPTION_ON_HOLD",
    6: "SUBSCRIPTION_IN_GRACE_PERIOD",
    7: "SUBSCRIPTION_RESTARTED",
    8: "SUBSCRIPTION_PRICE_CHANGE_CONFIRMED",
    9: "SUBSCRIPTION_DEFERRED",
    10: "SUBSCRIPTION_PAUSED",
    11: "SUBSCRIPTION_PAUSE_SCHEDULE_CHANGED",
    12: "SUBSCRIPTION_REVOKED",
    13: "SUBSCRIPTION_EXPIRED",
    20: "SUBSCRIPTION_PENDING_PURCHASE_CANCELED",
}


@dataclass
class GoogleVerificationResult:
    signature_valid: bool
    error_message: Optional[str]
    event_id: Optional[str]           # Pub/Sub messageId — primary idempotency key
    event_type: Optional[str]         # Mapped string (e.g. SUBSCRIPTION_RENEWED)
    purchase_token: Optional[str]
    subscription_id: Optional[str]    # productId / SKU
    package_name: Optional[str]
    raw_payload: Optional[dict]       # Decoded DeveloperNotification


def _extract_bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    if not authorization_header:
        return None
    parts = authorization_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def verify_and_decode(
    authorization_header: Optional[str],
    envelope: dict,
) -> GoogleVerificationResult:
    """Verify the Pub/Sub push OIDC token + decode the DeveloperNotification.

    Never raises — caller should audit-log the outcome and return 200 fast.
    """
    if not _LIB_AVAILABLE:
        return _fail("google-auth not installed; verification disabled", envelope)

    if not settings.GOOGLE_WEBHOOK_AUDIENCE:
        return _fail("GOOGLE_WEBHOOK_AUDIENCE not configured", envelope)

    token = _extract_bearer_token(authorization_header)
    if not token:
        return _fail("Missing or malformed Authorization Bearer token", envelope)

    try:
        claims = google_id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=settings.GOOGLE_WEBHOOK_AUDIENCE,
        )
    except Exception as exc:
        return _fail(f"OIDC token verification failed: {exc}", envelope)

    # Issuer must be Google.
    issuer = claims.get("iss")
    if issuer not in ("https://accounts.google.com", "accounts.google.com"):
        return _fail(f"Unexpected token issuer: {issuer}", envelope)

    # Optional: pin the service account that's allowed to sign these tokens.
    expected_email = settings.GOOGLE_WEBHOOK_SERVICE_ACCOUNT_EMAIL
    if expected_email and claims.get("email") != expected_email:
        return _fail(
            f"Token signed by {claims.get('email')}, expected {expected_email}",
            envelope,
        )

    # Envelope -> message -> base64(data) -> DeveloperNotification JSON
    message = (envelope or {}).get("message") or {}
    event_id = message.get("messageId") or message.get("message_id")
    data_b64 = message.get("data")
    if not data_b64:
        return _fail("Envelope missing message.data", envelope, event_id=event_id)

    try:
        notification_json = base64.b64decode(data_b64).decode("utf-8")
        notification = json.loads(notification_json)
    except Exception as exc:
        return _fail(f"Failed to decode message.data: {exc}", envelope, event_id=event_id)

    package_name = notification.get("packageName")
    subscription_notification = notification.get("subscriptionNotification") or {}
    one_time_notification = notification.get("oneTimeProductNotification") or {}
    notification_type = subscription_notification.get("notificationType") or one_time_notification.get("notificationType")
    event_type = GOOGLE_NOTIFICATION_TYPES.get(notification_type) if notification_type else None

    purchase_token = subscription_notification.get("purchaseToken") or one_time_notification.get("purchaseToken")
    subscription_id = subscription_notification.get("subscriptionId") or one_time_notification.get("sku")

    return GoogleVerificationResult(
        signature_valid=True,
        error_message=None,
        event_id=event_id,
        event_type=event_type,
        purchase_token=purchase_token,
        subscription_id=subscription_id,
        package_name=package_name,
        raw_payload=notification,
    )


def _fail(message: str, envelope: dict, *, event_id: Optional[str] = None) -> GoogleVerificationResult:
    if event_id is None and envelope:
        event_id = ((envelope.get("message") or {}).get("messageId")
                    or (envelope.get("message") or {}).get("message_id"))
    return GoogleVerificationResult(
        signature_valid=False,
        error_message=message,
        event_id=event_id,
        event_type=None,
        purchase_token=None,
        subscription_id=None,
        package_name=None,
        raw_payload=None,
    )
