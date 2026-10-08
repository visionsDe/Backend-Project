"""Per-feature token-bucket limiter."""
import pytest

from app.services.ai_client.exceptions import AIRateLimited
from app.services.ai_client.rate_limit import RateLimiter


def test_unconfigured_feature_is_unlimited():
    limiter = RateLimiter({"moderation": 2})
    for _ in range(50):
        limiter.check("other-unlimited-feature")  # no exception


def test_configured_feature_runs_out_of_tokens():
    limiter = RateLimiter({"moderation": 2})
    limiter.check("moderation")
    limiter.check("moderation")
    with pytest.raises(AIRateLimited) as exc_info:
        limiter.check("moderation")
    assert exc_info.value.feature == "moderation"
    assert exc_info.value.retry_after_s is not None


def test_zero_or_negative_limit_is_treated_as_unlimited():
    limiter = RateLimiter({"moderation": 0})
    for _ in range(10):
        limiter.check("moderation")


def test_bucket_refills_over_time(monkeypatch):
    """Advance ``time.monotonic`` so the bucket refills without a real sleep."""
    import time as time_module

    limiter = RateLimiter({"moderation": 60})  # one per second
    # Drain the bucket.
    for _ in range(60):
        limiter.check("moderation")
    with pytest.raises(AIRateLimited):
        limiter.check("moderation")

    # Jump the clock forward 2 seconds — bucket should refill by ~2.
    original_monotonic = time_module.monotonic
    fake_now = original_monotonic() + 2
    monkeypatch.setattr(time_module, "monotonic", lambda: fake_now)
    limiter.check("moderation")
    limiter.check("moderation")
