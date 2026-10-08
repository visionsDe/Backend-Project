"""Apple App Store Server Notifications V2 — JWS verification + payload decode.

Notifications arrive as a single JSON body with one field: ``signedPayload``.
That field is a JWS whose certificate chain rolls up to one of Apple's root
CAs. The inner claim is a ``ResponseBodyV2DecodedPayload`` which itself
contains further signed JWS fields (``data.signedTransactionInfo`` and
``data.signedRenewalInfo``).

We use Apple's official ``app-store-server-library`` for verification — it
walks the x5c chain, checks the signature, and validates the bundle ID /
environment claims against config. The library expects Apple's root CA
certificates loaded from disk; the path is configured via
``settings.APPLE_ROOT_CAS_DIR``. Download Apple's roots from
https://www.apple.com/certificateauthority/ and place the ``.cer`` files
(AppleRootCA-G2.cer, AppleRootCA-G3.cer, AppleIncRootCertificate.cer,
AppleComputerRootCertificate.cer) in that directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.config import settings
from app.utils.logging import setup_logger

logger = setup_logger()

# Static-type-only import: the real symbol is loaded conditionally below.
# This branch never executes at runtime; it's purely for type checkers so
# `Optional["SignedDataVerifier"]` resolves to the real class, not None.
if TYPE_CHECKING:
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

# The library is imported lazily at runtime so that pytest collection / startup
# doesn't fail in environments where the package hasn't been installed yet
# (e.g. local dev before `pip install -r requirements.txt`).
try:
    from appstoreserverlibrary.signed_data_verifier import (
        SignedDataVerifier,
        VerificationException,
    )
    from appstoreserverlibrary.models.Environment import Environment
    _LIB_AVAILABLE = True
except ImportError:  # pragma: no cover
    SignedDataVerifier = None  # type: ignore[assignment,misc]
    VerificationException = Exception  # type: ignore[assignment,misc]
    Environment = None  # type: ignore[assignment,misc]
    _LIB_AVAILABLE = False


@dataclass
class AppleVerificationResult:
    signature_valid: bool
    error_message: Optional[str]
    decoded_payload: Optional[object]  # ResponseBodyV2DecodedPayload when valid
    notification_uuid: Optional[str]
    notification_type: Optional[str]
    notification_subtype: Optional[str]
    original_transaction_id: Optional[str]
    environment: Optional[str]
    offer_type: Optional[int] = None
    offer_identifier: Optional[str] = None
    app_account_token: Optional[str] = None


def _load_root_certs() -> list[bytes]:
    """Read Apple root CA .cer files from APPLE_ROOT_CAS_DIR.

    Apple publishes the certs as DER-encoded .cer files. Returns the raw bytes
    of each cert in the configured directory. An empty list means the verifier
    can't be constructed and every signature check will fail safely.
    """
    if not settings.APPLE_ROOT_CAS_DIR or not os.path.isdir(settings.APPLE_ROOT_CAS_DIR):
        return []
    certs: list[bytes] = []
    for name in sorted(os.listdir(settings.APPLE_ROOT_CAS_DIR)):
        if not name.lower().endswith(".cer"):
            continue
        path = os.path.join(settings.APPLE_ROOT_CAS_DIR, name)
        try:
            with open(path, "rb") as f:
                certs.append(f.read())
        except OSError as exc:  # pragma: no cover - filesystem error
            logger.error("Failed to read Apple root cert %s: %s", path, exc)
    return certs


_verifier: Optional["SignedDataVerifier"] = None


def get_verifier():
    """Public alias for ``_get_verifier()`` — other modules (e.g.
    subscription_state) need the verifier to decode the inner
    ``signed_transaction_info`` / ``signed_renewal_info`` JWS strings
    carried by every notification. Returns the SignedDataVerifier or
    ``None`` when config is incomplete (same as ``_get_verifier``).
    """
    return _get_verifier()


def _get_verifier() -> Optional["SignedDataVerifier"]:
    """Lazy-init the SignedDataVerifier. Returns None if config is incomplete."""
    global _verifier
    if _verifier is not None:
        return _verifier
    if not _LIB_AVAILABLE:
        logger.error("app-store-server-library not installed; Apple webhook verification disabled")
        return None
    root_certs = _load_root_certs()
    if not root_certs:
        logger.error("APPLE_ROOT_CAS_DIR is empty or unset; Apple webhook verification will fail")
        return None
    if not settings.APPLE_BUNDLE_ID or not settings.APPLE_APP_APPLE_ID:
        logger.error("APPLE_BUNDLE_ID / APPLE_APP_APPLE_ID not configured; Apple webhook verification will fail")
        return None
    try:
        env = Environment.PRODUCTION if settings.APPLE_WEBHOOK_ENVIRONMENT.lower() == "production" else Environment.SANDBOX
        _verifier = SignedDataVerifier(
            root_certificates=root_certs,
            enable_online_checks=False,
            environment=env,
            bundle_id=settings.APPLE_BUNDLE_ID,
            app_apple_id=settings.APPLE_APP_APPLE_ID,
        )
        return _verifier
    except Exception as exc:  # pragma: no cover - library init error
        logger.exception("Failed to initialise SignedDataVerifier: %s", exc)
        return None


def verify_and_decode(signed_payload: str) -> AppleVerificationResult:
    """Verify the App Store Server Notification JWS and return the decoded claims.

    Never raises — invalid payloads are returned with ``signature_valid=False``
    and an ``error_message`` so the caller can audit-log and return 200.
    """
    verifier = _get_verifier()
    if verifier is None:
        return AppleVerificationResult(
            signature_valid=False,
            error_message="Verifier not configured (root certs / bundle id / app apple id missing)",
            decoded_payload=None,
            notification_uuid=None,
            notification_type=None,
            notification_subtype=None,
            original_transaction_id=None,
            environment=None,
        )

    try:
        decoded = verifier.verify_and_decode_notification(signed_payload)
    except VerificationException as exc:
        return AppleVerificationResult(
            signature_valid=False,
            error_message=f"JWS verification failed: {exc}",
            decoded_payload=None,
            notification_uuid=None,
            notification_type=None,
            notification_subtype=None,
            original_transaction_id=None,
            environment=None,
        )
    except Exception as exc:  # pragma: no cover - unexpected parse error
        logger.exception("Unexpected Apple webhook decode error")
        return AppleVerificationResult(
            signature_valid=False,
            error_message=f"Unexpected decode error: {exc}",
            decoded_payload=None,
            notification_uuid=None,
            notification_type=None,
            notification_subtype=None,
            original_transaction_id=None,
            environment=None,
        )

    # The verifier returns a ResponseBodyV2DecodedPayload. Pull the audit fields
    # we want to persist on `webhook_events` for indexing and matching.
    notification_uuid = getattr(decoded, "notificationUUID", None) or getattr(decoded, "notification_uuid", None)
    notification_type = _enum_value(getattr(decoded, "notificationType", None) or getattr(decoded, "notification_type", None))
    notification_subtype = _enum_value(getattr(decoded, "subtype", None))

    data = getattr(decoded, "data", None)
    original_transaction_id = None
    environment = _enum_value(getattr(data, "environment", None)) if data is not None else None

    # Inner signed_transaction_info is also JWS-signed by Apple — verify it to
    # pull the original_transaction_id we use for routing back to a row.
    offer_type: Optional[int] = None
    offer_identifier: Optional[str] = None
    app_account_token: Optional[str] = None
    signed_transaction = getattr(data, "signedTransactionInfo", None) or getattr(data, "signed_transaction_info", None) if data is not None else None
    if signed_transaction:
        try:
            transaction = verifier.verify_and_decode_signed_transaction(signed_transaction)
            original_transaction_id = getattr(transaction, "originalTransactionId", None) or getattr(transaction, "original_transaction_id", None)
            offer_type_raw = getattr(transaction, "offerType", None) or getattr(transaction, "offer_type", None)
            offer_type = int(getattr(offer_type_raw, "value", offer_type_raw)) if offer_type_raw is not None else None
            offer_identifier = getattr(transaction, "offerIdentifier", None) or getattr(transaction, "offer_identifier", None)
            app_account_token = getattr(transaction, "appAccountToken", None) or getattr(transaction, "app_account_token", None)
        except Exception as exc:  # pragma: no cover - inner JWS issue
            logger.warning("Apple signed_transaction_info inner verification failed: %s", exc)

    return AppleVerificationResult(
        signature_valid=True,
        error_message=None,
        decoded_payload=decoded,
        notification_uuid=notification_uuid,
        notification_type=notification_type,
        notification_subtype=notification_subtype,
        original_transaction_id=original_transaction_id,
        environment=environment,
        offer_type=offer_type,
        offer_identifier=offer_identifier,
        app_account_token=str(app_account_token) if app_account_token else None,
    )


def _enum_value(value):
    """Coerce a library enum to its string value when present."""
    if value is None:
        return None
    return getattr(value, "value", None) or str(value)
