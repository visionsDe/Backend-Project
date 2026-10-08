# coding: utf-8
from sqlalchemy import Column, ForeignKey, Index, Integer, JSON, TIMESTAMP, Table, Text, text, Boolean, Date
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER, TEXT, TINYINT, VARCHAR
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
from .base import Base, metadata


class WeekDay(Base):
    __tablename__ = 'week_days'

    id = Column(INTEGER, primary_key=True, comment='Unique identifier for the week day or translation')
    slug = Column(VARCHAR(255), nullable=False, unique=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the record was added')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the record was last updated')


class WeekDaysTranslation(Base):
    __tablename__ = 'week_days_translation'

    id = Column(INTEGER, primary_key=True, comment='Unique identifier for the week day or translation')
    week_day_id = Column(ForeignKey('week_days.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the week days')
    language_id = Column(ForeignKey('languages.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the language')
    day_name = Column(VARCHAR(255), nullable=False, comment='day name')
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the record was added')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the record was last updated')

    language = relationship('Language')
    week_day = relationship('WeekDay')


class Supplier(Base):
    __tablename__ = 'suppliers'
    __table_args__ = (
        Index('ix_suppliers_status', 'status'),
        Index('ix_suppliers_deleted_at', 'deleted_at'),
        Index('ix_suppliers_created_at', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the user meta record')
    user_id = Column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the user')
    status = Column(ENUM('Pending Approval', 'Approved', 'Rejected'), nullable=False, server_default=text("'Approved'"))
    rejection_reason = Column(VARCHAR(255))
    stripe_account_id = Column(VARCHAR(255))
    stripe_bank_account_id = Column(VARCHAR(255))
    stripe_verification_status = Column(ENUM('unverified', 'verified', 'pending'), server_default=text("'unverified'"))
    stripe_platform = Column(VARCHAR(10), index=True,
                             comment="Platform that owns the connected account: 'BR' or 'CH'. NULL until first onboarding.")
    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the record was created')
    updated_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the supplier was last updated')
    deleted_at = Column(TIMESTAMP)

    user = relationship('User') 



class SupplierSubscription(Base):
    __tablename__ = 'supplier_subscription'
    __table_args__ = (
        Index('ix_supplier_subscription_status', 'status'),
        Index('ix_supplier_subscription_expiry_date', 'expiry_date'),
        Index('ix_supplier_subscription_original_transaction_id', 'original_transaction_id'),
        Index('ix_supplier_subscription_provider', 'provider'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the subscription')
    supplier_id = Column(ForeignKey('suppliers.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the user')
    order_id = Column(VARCHAR(50), nullable=False)
    product_id = Column(VARCHAR(100), nullable=False, comment='Reference to the subscription type')
    purchase_time = Column(VARCHAR(20), nullable=False)
    purchase_state = Column(Integer, nullable=False)
    purchase_token = Column(Text, nullable=False)
    quantity = Column(Integer, nullable=False)
    auto_renewing = Column(Boolean, default=True)
    acknowledged = Column(Boolean, default=False)
    purchase_response = Column(JSON, comment='In-app Purchase response')
    status = Column(ENUM('active', 'inactive', 'cancelled'), nullable=False, server_default=text("'active'"), comment='Status of the subscription')
    subscription_type = Column(
        ENUM('0', '1', '2', '3'),
        nullable=False,
        server_default=text("'0'"),
        comment="0 = standard (default), 1 = gold, 2 = silver, 3 = diamond",
    )
    personal_subscription = Column(Boolean, default=True)
    personal_subscription_pending_days = Column(Integer, default=0)
    # Webhook routing & matching (added 2026-06). All nullable: existing rows have no provider set;
    # the backfill script populates them from purchase_response. New purchases set them at insert time.
    provider = Column(VARCHAR(20), comment="'apple' | 'google' | 'org_grant'")
    original_transaction_id = Column(VARCHAR(255), comment='Apple: immutable id of the first transaction in a renewal chain')
    linked_purchase_token = Column(TEXT, comment='Google: previous purchaseToken in an upgrade chain')
    last_event_id = Column(VARCHAR(255), comment='Last webhook event applied to this row')
    last_event_at = Column(TIMESTAMP, comment='When the last webhook event was applied')
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the subscription was created')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the subscription was last updated')
    expiry_date = Column(TIMESTAMP, comment="Timestamp when access expires")

    supplier = relationship('Supplier')


class WebhookEvent(Base):
    """Audit log + idempotency dedup for Apple App Store Server Notifications V2
    and Google Play RTDN. (provider, event_id) is unique — duplicate deliveries
    raise IntegrityError on insert and are treated as already-processed."""

    __tablename__ = 'webhook_events'
    __table_args__ = (
        Index('ix_webhook_events_supplier_subscription_id', 'supplier_subscription_id'),
        Index('ix_webhook_events_created_at', 'created_at'),
        Index('ix_webhook_events_event_type', 'event_type'),
    )

    id = Column(BIGINT, primary_key=True)
    provider = Column(VARCHAR(20), nullable=False, comment="'apple' or 'google'")
    event_id = Column(VARCHAR(255), nullable=False, comment="Apple notificationUUID / Google Pub/Sub messageId")
    event_type = Column(VARCHAR(100), comment="e.g. DID_RENEW, SUBSCRIPTION_PURCHASED")
    event_subtype = Column(VARCHAR(100), comment="Apple subtype field")
    supplier_subscription_id = Column(ForeignKey('supplier_subscription.id', ondelete='SET NULL'))
    original_transaction_id = Column(VARCHAR(255))
    purchase_token = Column(TEXT)
    signature_valid = Column(Boolean, comment="True if the JWS/JWT signature verified")
    outcome = Column(
        ENUM('received', 'processed', 'duplicate', 'signature_fail', 'handler_error', 'unmatched',
             name='webhook_event_outcome'),
        nullable=False,
        server_default=text("'received'"),
    )
    error_message = Column(TEXT)
    raw_payload = Column(JSON, comment="Full decoded payload for audit / replay")
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    processed_at = Column(TIMESTAMP)

    supplier_subscription = relationship('SupplierSubscription')


class SubscriptionRenewalLog(Base):
    """Snapshot of every /supplier-subscription reactivation.

    When a same-supplier purchase hits an existing inactive/cancelled row, the
    row is reactivated in place — keeping the same chain identifier so webhook
    routing stays correct, but overwriting ``purchase_response`` / ``order_id``
    / ``purchase_token``. This table preserves the per-renewal history so it can
    still be reconstructed for support / disputes.
    """

    __tablename__ = 'subscription_renewal_logs'
    __table_args__ = (
        Index('ix_subscription_renewal_logs_supplier_subscription_id', 'supplier_subscription_id'),
        Index('ix_subscription_renewal_logs_created_at', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True)
    supplier_subscription_id = Column(
        ForeignKey('supplier_subscription.id', ondelete='CASCADE'), nullable=False,
    )
    previous_status = Column(VARCHAR(20), comment="Status the row had immediately before reactivation")
    previous_supplier_id = Column(BIGINT, index=True,
                                  comment="supplier_id that owned the row before this renewal/reassignment — differs from the row's current supplier_id only when an inactive chain has been claimed by a new supplier")
    order_id = Column(VARCHAR(50))
    product_id = Column(VARCHAR(100))
    purchase_token = Column(TEXT)
    subscription_type = Column(VARCHAR(2),
                               comment="0=standard, 1=gold, 2=silver — useful when the supplier upgrades on renewal")
    purchase_response = Column(JSON, comment="Full payload at renewal time")
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    supplier_subscription = relationship('SupplierSubscription', backref='renewal_logs')


t_supplier_item_categories = Table(
    'supplier_item_categories', metadata,
    Column('supplier_item_id', ForeignKey('supplier_items.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True),
    Column('category_id', ForeignKey('categories.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True),
    Column('sub_category_id', ForeignKey('categories.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True)
)
