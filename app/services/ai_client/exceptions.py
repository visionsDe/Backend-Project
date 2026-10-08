"""Exceptions raised by the shared AI client.

Callers should catch :class:`AIRequestError` (or its subclasses) so that
provider-specific exceptions never leak into feature code.
"""


class AIRequestError(Exception):
    """Base class for anything that goes wrong in an AI request."""


class AIRateLimited(AIRequestError):
    """The per-feature rate limit refused the request.

    ``retry_after_s`` is a best-effort suggestion — for now it's set to
    the token bucket's refill period; a scheduled retry can honor it.
    """

    def __init__(self, feature: str, retry_after_s: float | None = None):
        self.feature = feature
        self.retry_after_s = retry_after_s
        super().__init__(f"AI feature '{feature}' is rate-limited")


class AIProviderError(AIRequestError):
    """The upstream provider returned an error we couldn't recover from
    (after retries)."""

    def __init__(self, message: str, provider: str, original: Exception | None = None):
        self.provider = provider
        self.original = original
        super().__init__(message)
