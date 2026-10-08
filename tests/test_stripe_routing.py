"""Country → platform routing for Stripe SCT."""
from app.utils.stripe_client import (
    PLATFORM_PRIMARY,
    PLATFORM_SECONDARY,
    _platform_for_country_code,
    stripe_supported_platform_for_country_code,
)


def test_brazil_routes_to_primary():
    assert _platform_for_country_code("BR") == PLATFORM_PRIMARY
    assert _platform_for_country_code("BRA") == PLATFORM_PRIMARY


def test_switzerland_routes_to_secondary():
    assert _platform_for_country_code("CH") == PLATFORM_SECONDARY
    assert _platform_for_country_code("CHE") == PLATFORM_SECONDARY


def test_eu_countries_route_to_secondary():
    for code in ("DE", "FR", "ES", "IT", "DEU"):
        assert _platform_for_country_code(code) == PLATFORM_SECONDARY, code


def test_unsupported_country_returns_none():
    # UK is deliberately excluded — the EEA/UK sits outside the current set.
    assert _platform_for_country_code("GB") is None
    assert _platform_for_country_code("US") is None
    assert _platform_for_country_code("") is None
    assert _platform_for_country_code(None) is None


def test_public_resolver_is_non_raising():
    """``stripe_supported_platform_for_country_code`` is the non-raising
    counterpart used by the connected-account gate; it must never raise."""
    assert stripe_supported_platform_for_country_code("ZZ") is None
    assert stripe_supported_platform_for_country_code("BR") == PLATFORM_PRIMARY
