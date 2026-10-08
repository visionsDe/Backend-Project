"""Retry helper for AI provider calls.

Wraps a single provider call in exponential-backoff retry on transient
errors — 429 rate-limit responses, 5xx server errors, and network-level
failures. Non-transient errors (bad request, auth) bubble up on the
first attempt.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, TypeVar

import openai

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient error classes — anything else is treated as "don't retry".
_TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def call_with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 2,
    base_delay_s: float = 0.5,
    max_delay_s: float = 8.0,
) -> T:
    """Invoke ``fn`` up to ``max_attempts`` times with exponential backoff.

    Sleeps ``base_delay_s * 2**(attempt-1)`` (jittered by up to 25%)
    between attempts, capped at ``max_delay_s``. Raises the last
    transient exception if every attempt fails, or the first
    non-transient exception it sees.
    """
    attempt = 0
    last_exc: Exception | None = None
    while attempt < max_attempts:
        attempt += 1
        try:
            return fn()
        except _TRANSIENT_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = min(base_delay_s * (2 ** (attempt - 1)), max_delay_s)
            delay += delay * random.uniform(0, 0.25)
            logger.warning(
                "AI call transient failure (attempt %d/%d): %s — retrying in %.2fs",
                attempt, max_attempts, exc, delay,
            )
            time.sleep(delay)
    assert last_exc is not None  # loop always sets it before break
    raise last_exc
