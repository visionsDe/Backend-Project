"""AIClient — the provider-agnostic facade every feature calls.

Handles:
  1. Per-feature rate limiting (raises ``AIRateLimited`` when over).
  2. Provider call via the configured adapter, wrapped in retry.
  3. Usage-log row persisted to ``ai_usage_log`` (success or failure).
  4. Returns the raw provider response so callers can parse it their
     way — nothing about existing moderation.py parsing changes.
"""
from __future__ import annotations

import logging
import time
from threading import Lock
from typing import Any, Optional

from app.config import settings

from .exceptions import AIProviderError, AIRequestError
from .logging import UsageRecord, persist
from .openai_adapter import PROVIDER_NAME as OPENAI, OpenAIAdapter
from .pricing import compute_cost_cents
from .rate_limit import RateLimiter
from .retry import call_with_retry

logger = logging.getLogger(__name__)


_ADAPTERS = {OPENAI: OpenAIAdapter}


class AIClient:
    """Feature-facing entry point. Not usually constructed directly;
    fetch the process-wide instance with :func:`get_ai_client`."""

    def __init__(
        self,
        provider: str = OPENAI,
        rate_limits_per_minute: Optional[dict[str, int]] = None,
    ):
        adapter_cls = _ADAPTERS.get(provider)
        if adapter_cls is None:
            raise AIRequestError(f"Unknown AI provider: {provider!r}")
        self.provider = provider
        self._adapter = adapter_cls()
        self._limiter = RateLimiter(rate_limits_per_minute)

    # ── Moderation ───────────────────────────────────────────────────
    def moderate_text(
        self,
        text: str,
        *,
        feature: Any,
        model: str = "omni-moderation-latest",
        user_id: Optional[int] = None,
        context: Optional[dict] = None,
    ) -> Any:
        return self._run(
            operation="moderation",
            model=model,
            feature=feature,
            invoke=lambda: self._adapter.moderate_text(text, model=model),
            user_id=user_id,
            context=context,
        )

    def moderate_image(
        self,
        image_bytes: bytes,
        content_type: str,
        *,
        feature: Any,
        model: str = "omni-moderation-latest",
        user_id: Optional[int] = None,
        context: Optional[dict] = None,
    ) -> Any:
        return self._run(
            operation="moderation",
            model=model,
            feature=feature,
            invoke=lambda: self._adapter.moderate_image(image_bytes, content_type, model=model),
            user_id=user_id,
            context=context,
        )

    # ── Chat completions ─────────────────────────────────────────────
    def chat(
        self,
        messages: list[dict],
        *,
        feature: Any,
        model: str,
        response_format: Optional[dict] = None,
        temperature: float = 0,
        user_id: Optional[int] = None,
        context: Optional[dict] = None,
        operation: str = "chat",
    ) -> Any:
        """Chat completions call.

        ``operation`` is the label written to ``ai_usage_log.operation`` —
        defaults to ``"chat"`` but callers should override it when the
        call is semantically something else (e.g. the second-layer
        moderation pass passes ``operation="moderation_review"``)."""
        return self._run(
            operation=operation,
            model=model,
            feature=feature,
            invoke=lambda: self._adapter.chat(
                messages, model=model,
                response_format=response_format, temperature=temperature,
            ),
            user_id=user_id,
            context=context,
        )

    # ── Internals ────────────────────────────────────────────────────
    def _run(
        self,
        *,
        operation: str,
        model: str,
        feature: Any,
        invoke,
        user_id: Optional[int],
        context: Optional[dict],
    ) -> Any:
        feature_tag = feature.value if hasattr(feature, "value") else str(feature)
        self._limiter.check(feature_tag)
        started = time.monotonic()
        response: Any = None
        error: Optional[Exception] = None
        try:
            response = call_with_retry(invoke)
            return response
        except Exception as exc:
            error = exc
            raise AIProviderError(
                f"{self.provider} {operation} failed", provider=self.provider, original=exc,
            ) from exc
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            self._record(
                feature_tag=feature_tag,
                operation=operation,
                model=model,
                response=response,
                error=error,
                latency_ms=latency_ms,
                user_id=user_id,
                context=context,
            )

    def _record(
        self,
        *,
        feature_tag: str,
        operation: str,
        model: str,
        response: Any,
        error: Optional[Exception],
        latency_ms: int,
        user_id: Optional[int],
        context: Optional[dict],
    ) -> None:
        prompt_tokens, completion_tokens, total_tokens, request_id = _extract_usage(response)
        cost_cents = compute_cost_cents(self.provider, model, prompt_tokens, completion_tokens)
        record = UsageRecord(
            feature=feature_tag,
            provider=self.provider,
            model=model,
            operation=operation,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_cents=cost_cents,
            latency_ms=latency_ms,
            success=error is None,
            error_class=type(error).__name__ if error is not None else None,
            request_id=request_id,
            user_id=user_id,
            context=context or {},
        )
        persist(record)


# ── Token-usage extraction from provider responses ────────────────────
def _extract_usage(response: Any) -> tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    """Pull (prompt_tokens, completion_tokens, total_tokens, request_id)
    from a provider response object. Missing fields return None."""
    if response is None:
        return None, None, None, None
    prompt = completion = total = None
    usage = getattr(response, "usage", None)
    if usage is not None:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        total = getattr(usage, "total_tokens", None)
    request_id = getattr(response, "id", None) or getattr(response, "_request_id", None)
    return prompt, completion, total, request_id


# ── Process-wide singleton ────────────────────────────────────────────
_singleton_lock = Lock()
_singleton: Optional[AIClient] = None


def get_ai_client() -> AIClient:
    """Return the process-wide :class:`AIClient` instance.

    Config is read from :data:`app.config.settings` — ``AI_PROVIDER_DEFAULT``
    picks the provider (default ``openai``) and ``AI_RATE_LIMITS`` maps
    feature tags to requests-per-minute. Features not in the map are
    treated as unlimited.
    """
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            provider = getattr(settings, "AI_PROVIDER_DEFAULT", OPENAI) or OPENAI
            rate_limits = getattr(settings, "AI_RATE_LIMITS", None) or {}
            _singleton = AIClient(provider=provider, rate_limits_per_minute=rate_limits)
    return _singleton
