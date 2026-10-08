"""Multi-platform Stripe configuration helper."""

from __future__ import annotations

from typing import Optional

from app.config import settings


PLATFORM_BR = "BR"
PLATFORM_CH = "CH"

# EU-27 country codes (ISO-2 + ISO-3) — routed through the Swiss platform.
# EEA (Norway/Iceland/Liechtenstein) and the UK are NOT included; extend
# the set if / when those become supported.
_EU_COUNTRY_CODES = {
    # ISO-2
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE",
    "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE",
    # ISO-3
    "AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA",
    "DEU", "GRC", "HUN", "IRL", "ITA", "LVA", "LTU", "LUX", "MLT", "NLD",
    "POL", "PRT", "ROU", "SVK", "SVN", "ESP", "SWE",
}


class StripeCountryNotSupported(Exception):
    """Raised when a supplier's country isn't served by any configured
    Stripe platform (i.e. outside BR + CH + EU-27) AND the supplier has
    no stored ``stripe_platform`` to fall back on.
    """


def _platform_for_country_code(code: Optional[str]) -> Optional[str]:
    """Map an ISO country code to the Stripe platform that serves it.
    Returns ``None`` for anything outside the supported set."""
    if not code:
        return None
    up = code.upper()
    if up in ("BR", "BRA"):
        return PLATFORM_BR
    if up in ("CH", "CHE") or up in _EU_COUNTRY_CODES:
        return PLATFORM_CH
    return None


def _platform_for_supplier(supplier) -> Optional[str]:
    """Resolve a supplier row to a platform"""
    if supplier is None:
        return None
    stored = getattr(supplier, "stripe_platform", None)
    if stored:
        stored_upper = stored.upper()
        if stored_upper in (PLATFORM_BR, PLATFORM_CH):
            return stored_upper
    user = getattr(supplier, "user", None)
    country = getattr(user, "country", None) if user else None
    code = getattr(country, "country_code", None) if country else None
    return _platform_for_country_code(code)


# ─────────────────────────────────────────────────────────────────────
# Public lookups
# ─────────────────────────────────────────────────────────────────────
def stripe_api_key_for(supplier) -> str:
    """Return the Stripe secret key for the platform that owns this
    supplier's account. Raises :class:`StripeCountryNotSupported` if we
    can't resolve a platform (unknown country + no stored platform)."""
    platform = _platform_for_supplier(supplier)
    if not platform:
        raise StripeCountryNotSupported()
    return _api_key_for_platform(platform)


def stripe_api_key_for_country_code(country_code: Optional[str]) -> str:
    """Same, but for a raw ISO country code — raises if unsupported."""
    platform = _platform_for_country_code(country_code)
    if not platform:
        raise StripeCountryNotSupported()
    return _api_key_for_platform(platform)


def stripe_api_key_for_platform(platform: str) -> str:
    """Return the Stripe secret key for an already-resolved platform code."""
    return _api_key_for_platform(platform)


def stripe_platform_for_supplier(supplier) -> Optional[str]:
    """Expose the resolved platform key (``'BR'`` / ``'CH'`` / ``None``)"""
    return _platform_for_supplier(supplier)


def stripe_supported_platform_for_country_code(code: Optional[str]) -> Optional[str]:
    """Public — resolve a country code to a platform, ``None`` if the
    country is outside the supported set. Non-raising counterpart to
    :func:`stripe_api_key_for_country_code`; used by the
    ``create-connected-account`` gate."""
    return _platform_for_country_code(code)


def stripe_webhook_secret_for_platform(platform: str) -> str:
    """Return the webhook-signing secret for a platform key."""
    if platform == PLATFORM_CH and settings.STRIPE_CH_WEBHOOK_SECRET:
        return settings.STRIPE_CH_WEBHOOK_SECRET
    # BR or fallback when CH secret not yet provisioned.
    return settings.STRIPE_BR_WEBHOOK_SECRET or settings.MEI_STRIPE_WEBHOOK_SECRET_KEY


def stripe_business_website_for_platform(platform: str) -> str:
    """Return the ``business_profile.url`` to send with ``Account.create``."""
    if platform == PLATFORM_CH and settings.STRIPE_CH_WEBSITE:
        return settings.STRIPE_CH_WEBSITE
    return settings.STRIPE_BR_WEBSITE or settings.STRIPE_DEFAULT_WEBSITE


# ─────────────────────────────────────────────────────────────────────
# Internal
# ─────────────────────────────────────────────────────────────────────
def _api_key_for_platform(platform: str) -> str:
    if platform == PLATFORM_CH and settings.STRIPE_CH_SECRET_KEY:
        return settings.STRIPE_CH_SECRET_KEY
    return settings.STRIPE_BR_SECRET_KEY or settings.MEI_STRIPE_SECRET_KEY


def stripe_country_for_platform(platform: str) -> str:
    """Canonical ISO-2 country code for the platform (``'BR'`` / ``'CH'``)"""
    if platform == PLATFORM_CH:
        return "CH"
    return "BR"


# ─────────────────────────────────────────────────────────────────────
# Connected-account verification state
# ─────────────────────────────────────────────────────────────────────
def map_stripe_requirements_to_status(requirements: Optional[dict]) -> str:
    """Map Stripe's ``account.requirements`` object to the
    ``Supplier.stripe_verification_status`` enum.
    """
    if not requirements:
        return "unverified"
    currently_due = requirements.get("currently_due") or []
    pending = requirements.get("pending_verification") or []
    if not currently_due and not pending:
        return "verified"
    return "unverified"
