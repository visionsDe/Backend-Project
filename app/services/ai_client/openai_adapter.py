"""OpenAI adapter for the shared AI client.

Concrete provider implementation — the only adapter today. Adding a
second provider means dropping a sibling file with the same three
methods (``moderate_text``, ``moderate_image``, ``chat``) and pointing
the client at it via the ``AI_PROVIDER_DEFAULT`` config knob.
"""
from __future__ import annotations

import base64
from typing import Any, Optional

from openai import OpenAI

from app.config import settings


PROVIDER_NAME = "openai"


class OpenAIAdapter:
    """Thin wrapper around ``openai.OpenAI`` — no retry, no logging, no
    rate limiting. Those concerns live in :mod:`.client`."""

    def __init__(self, api_key: Optional[str] = None):
        self._client = OpenAI(api_key=api_key or settings.OPENAI_API_KEY)

    # ── Moderation ───────────────────────────────────────────────────
    def moderate_text(self, text: str, model: str) -> Any:
        return self._client.moderations.create(model=model, input=text)

    def moderate_image(self, image_bytes: bytes, content_type: str, model: str) -> Any:
        data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
        return self._client.moderations.create(
            model=model,
            input=[{"type": "image_url", "image_url": {"url": data_url}}],
        )

    # ── Chat completions ─────────────────────────────────────────────
    def chat(
        self,
        messages: list[dict],
        model: str,
        response_format: Optional[dict] = None,
        temperature: float = 0,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        return self._client.chat.completions.create(**kwargs)
