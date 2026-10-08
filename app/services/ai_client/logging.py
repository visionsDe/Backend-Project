"""Persistence for AI call records.

Writes one row to ``ai_usage_log`` per attempt (success or failure).
Uses its own short-lived DB session so it can log even when the caller
holds no session open. Never raises — a logging failure must not sink
the AI call itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from app.database import SessionLocal
from app import models

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """One row's worth of AI-call telemetry."""

    feature: str
    provider: str
    model: str
    operation: str  # 'chat' / 'moderation' / 'embedding'
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_cents: Decimal = Decimal(0)
    latency_ms: int = 0
    success: bool = True
    error_class: Optional[str] = None
    request_id: Optional[str] = None
    user_id: Optional[int] = None
    context: dict[str, Any] = field(default_factory=dict)


def persist(record: UsageRecord) -> None:
    """Insert one usage-log row. Never raises."""
    try:
        db = SessionLocal()
        try:
            row = models.AIUsageLog(
                feature=record.feature,
                provider=record.provider,
                model=record.model,
                operation=record.operation,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                total_tokens=record.total_tokens,
                cost_cents=record.cost_cents,
                latency_ms=record.latency_ms,
                success=record.success,
                error_class=record.error_class,
                request_id=record.request_id,
                user_id=record.user_id,
                context=record.context or None,
            )
            db.add(row)
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover — best-effort logging
        logger.error("Failed to persist AI usage log: %s", exc)
