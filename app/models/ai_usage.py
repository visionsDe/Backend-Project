"""``ai_usage_log`` — one row per AI-provider call.

Populated by :mod:`app.services.ai_client`. Feature-agnostic ops table
used by the admin usage/cost report; the per-item audit trail
(``item_moderation_logs``) is a separate, domain-specific table.
"""
from sqlalchemy import Boolean, Column, DECIMAL, Index, JSON, TIMESTAMP, text
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER, VARCHAR

from .base import Base


class AIUsageLog(Base):
    __tablename__ = "ai_usage_log"
    __table_args__ = (
        Index("ix_ai_usage_log_feature_created_at", "feature", "created_at"),
        Index("ix_ai_usage_log_created_at", "created_at"),
        Index("ix_ai_usage_log_model_created_at", "model", "created_at"),
    )

    id = Column(BIGINT, primary_key=True, comment="Unique identifier")

    feature = Column(
        ENUM("moderation", "other"),
        nullable=False,
        comment="AI feature that triggered the call — must match Feature enum in services.ai_client",
    )
    provider = Column(VARCHAR(32), nullable=False, comment="Provider slug, e.g. 'openai'")
    model = Column(VARCHAR(64), nullable=False, comment="Model id, e.g. 'gpt-4o-mini'")
    operation = Column(VARCHAR(32), nullable=False, comment="'chat' / 'moderation' / 'embedding'")

    prompt_tokens = Column(INTEGER, nullable=True, comment="Input tokens (nullable — moderation endpoint returns none)")
    completion_tokens = Column(INTEGER, nullable=True, comment="Output tokens (nullable)")
    total_tokens = Column(INTEGER, nullable=True, comment="Derived total; helps aggregate queries")

    cost_cents = Column(DECIMAL(16, 8), nullable=False, server_default=text("0"),
                        comment="US-cents estimate from pricing.py — DECIMAL(16,8) so sub-cent values (e.g. small gpt-4o-mini calls) are preserved")
    latency_ms = Column(INTEGER, nullable=False, server_default=text("0"), comment="Wall-clock request time")

    success = Column(Boolean, nullable=False, server_default=text("1"),
                     comment="True if the call returned successfully")
    error_class = Column(VARCHAR(128), nullable=True,
                         comment="Exception class name when success is false")
    request_id = Column(VARCHAR(128), nullable=True,
                        comment="Provider request id for cross-referencing with provider logs")

    user_id = Column(BIGINT, nullable=True, index=True,
                     comment="Optional caller user id — not a FK so log survives user delete")
    context = Column(JSON, nullable=True,
                     comment="Free-form JSON with additional trace info (item_id, booking_id, etc.)")

    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"),
                        nullable=False, comment="When the log row was written")
