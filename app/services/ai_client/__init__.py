"""Centralized AI integration layer.

All AI calls go through :func:`get_ai_client` so that logging, retry,
per-feature rate limiting and cost tracking are uniform across features.

Public surface:

    from app.services.ai_client import get_ai_client, Feature

    client = get_ai_client()
    result = client.moderate_text("hello", feature=Feature.MODERATION)
    reply = client.chat(messages, model="gpt-4o-mini", feature=Feature.MODERATION)
"""
from enum import Enum

from .client import AIClient, get_ai_client
from .exceptions import (
    AIProviderError,
    AIRateLimited,
    AIRequestError,
)


class Feature(str, Enum):
    """Feature tags stamped on every AI usage-log row.

    Kept in sync with the ENUM values in the ``ai_usage_log`` migration.
    Add new members here and add a new Alembic migration extending the
    ENUM when introducing an AI-backed feature; downstream reporting
    groups by this tag.

    ``OTHER`` is intentionally kept as a catch-all so an ad-hoc call
    that hasn't declared its own feature tag still logs cleanly.
    """

    MODERATION = "moderation"
    OTHER = "other"


__all__ = [
    "AIClient",
    "AIProviderError",
    "AIRateLimited",
    "AIRequestError",
    "Feature",
    "get_ai_client",
]
