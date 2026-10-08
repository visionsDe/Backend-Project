# coding: utf-8
from sqlalchemy import BigInteger, Boolean, Column, DECIMAL, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, TIMESTAMP, Table, Text, Time, text
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER, TEXT, FLOAT, VARCHAR
from sqlalchemy.orm import relationship
import datetime
from sqlalchemy.ext.declarative import declarative_base
from .base import Base, metadata


class DisputeType(Base):
    __tablename__ = 'dispute_types'

    id = Column(INTEGER, primary_key=True)
    slug = Column(VARCHAR(255), nullable=False, unique=True)
    status = Column(ENUM('0', '1'), nullable=False, server_default=text("'0'"), comment='0 => Deactivated, 1 => Activated')
    category = Column(
        ENUM('dispute', 'cancellation'),
        nullable=False,
        server_default=text("'dispute'"),
        comment="Grouping — 'dispute' for admin disputes, 'cancellation' for the customer-cancel reason list",
    )
    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(TIMESTAMP)


class DisputeTypeTranslation(Base):
    __tablename__ = 'dispute_type_translation'

    id = Column(BIGINT, primary_key=True)
    dispute_type_id = Column(ForeignKey('dispute_types.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the dispute types')
    language_id = Column(ForeignKey('languages.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the languages')
    dispute_title = Column(VARCHAR(255), nullable=False, comment='dispute title')
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)

    dispute_type = relationship('DisputeType')
    language = relationship('Language')


class Booking(Base):
    __tablename__ = 'bookings'
    __table_args__ = (
        Index('ix_bookings_status', 'status'),
        Index('ix_bookings_created_at', 'created_at'),
        Index('ix_bookings_order_number', 'order_number'),
        Index('ix_bookings_supplier_status', 'supplier_id', 'status'),
        Index('ix_bookings_user_status', 'user_id', 'status'),
        Index('ix_bookings_expense_classification', 'expense_classification'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the booking')
    user_id = Column(ForeignKey('users.id', ondelete='SET NULL'), index=True, comment='Reference to the user being booked')
    supplier_id = Column(ForeignKey('suppliers.id', ondelete='SET NULL'), index=True, comment='Reference to the service provider supplier')
    status = Column(ENUM('pending', 'accepted', 'payment_pending', 'confirmed', 'complete', 'complete_closed', 'uncomplete', 'uncomplete_closed', 'supplier_completed', 'user_completed', 'rejected', 'cancelled', 'dispute'), nullable=False, server_default=text("'pending'"), comment='Status of the booking')
    cancelled_reason = Column(VARCHAR(255), comment='Cancelled reason')
    cancelled_by = Column(ForeignKey('users.id', ondelete='SET NULL'), index=True, comment='booking cancelled by user or supplier')
    payment_mode = Column(ENUM('offline', 'online'), nullable=False, server_default=text("'offline'"), comment='booking payment mode')
    booking_date_time = Column(DateTime, nullable=True)
    is_delivery = Column(ENUM('0', '1'), nullable=False, server_default=text("'0'"), comment='0 = self pick up, 1 = delivery required')
    customer_name = Column(VARCHAR(100), nullable=False)
    customer_company_name = Column(VARCHAR(255), comment='Snapshot of customer company_name at booking creation')
    booking_address = Column(TEXT, nullable=False)
    booking_address_additional_direction = Column(VARCHAR(255))
    supplier_name = Column(VARCHAR(100), nullable=False)
    supplier_company_name = Column(VARCHAR(255), comment='Snapshot of supplier company_name at booking creation')
    additional_fee = Column(Float(5))
    discount = Column(Float(5))
    total_price = Column(Float(10), nullable=False)
    additional_fee_reason = Column(TEXT)
    booking_additional_information = Column(TEXT)
    agreement_notes = Column(TEXT, nullable=True, comment='Optional pre-acceptance note recorded by the entrepreneur; locks once status moves past pending')
    order_date_time = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    customer_email = Column(VARCHAR(255), nullable=False, comment="Customer's email address")
    latitude = Column(VARCHAR(30), comment='Latitude')
    longitude = Column(VARCHAR(30), comment='Longitude')
    city = Column(VARCHAR(150), comment='City')
    apartment_zone = Column(VARCHAR(80), comment='Apartment zone')
    order_number = Column(VARCHAR(30), comment='Order Number')
    currency_code = Column(VARCHAR(11), nullable=False, comment='Currency code (e.g., USD, EUR)')
    currency_symbol = Column(VARCHAR(10), nullable=True, comment='Symbol of the currency')
    order_deadline_days = Column(Integer)
    state = Column(VARCHAR(150), comment='State')
    district = Column(VARCHAR(150), comment='District')
    country_id = Column(ForeignKey('countries.id', ondelete='SET NULL'), nullable=True, index=True, comment='Country ID referencing the Country')
    booking_address_response = Column(JSON, comment='Booking Address response')
    expense_classification = Column(
        ENUM('0', '1', '2'),
        nullable=False,
        server_default=text("'2'"),
        comment='Supplier-as-buyer expense classification. 0 = personal, 1 = business, 2 = non_identified (default). Meaningful only for orders placed by suppliers.',
    )
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the booking was made')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the booking was last updated')

    user = relationship('User', primaryjoin='Booking.cancelled_by == User.id')
    supplier = relationship('Supplier')
    user1 = relationship('User', primaryjoin='Booking.user_id == User.id')
    transactions = relationship('BookingTransaction', back_populates='booking')
    country = relationship('Country')



class BookingDispute(Base):
    __tablename__ = 'booking_disputes'
    __table_args__ = (
        Index('ix_booking_disputes_status', 'dispute_status'),
        Index('ix_booking_disputes_created_at', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the dispute')
    booking_id = Column(ForeignKey('bookings.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the bookings')
    dispute_type_id = Column(ForeignKey('dispute_types.id', ondelete='SET NULL'), index=True, comment='Reference to the dispute types')
    dispute_reason = Column(TEXT, comment='Reason for dispute')
    dispute_img = Column(VARCHAR(255), comment='dispute picture')
    dispute_status = Column(ENUM('0', '1'), nullable=False, server_default=text("'0'"), comment='0 => Pending, 1 => Closed')
    reacted_by = Column(ForeignKey('users.id', ondelete='SET NULL'), index=True, comment='Reference to the user/admin who resolved or rejected the dispute')
    reaction_reason = Column(TEXT, comment='Reason for dispute reaction reason')
    created_by = Column(ForeignKey('users.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True)
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the service order was added.')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the record was last updated.')

    booking = relationship('Booking')
    user = relationship('User', primaryjoin='BookingDispute.created_by == User.id')
    dispute_type = relationship('DisputeType')
    user1 = relationship('User', primaryjoin='BookingDispute.reacted_by == User.id')


class BookingItem(Base):
    __tablename__ = 'booking_items'

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the booking services')
    booking_id = Column(ForeignKey('bookings.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the bookings')
    supplier_item_id = Column(ForeignKey('supplier_items.id', ondelete='SET NULL'), index=True, comment='Reference to the supplier services')
    item_name = Column(VARCHAR(255), nullable=False)
    item_price = Column(Float(10), comment='price')
    item_unit = Column(ENUM('0', '1'), nullable=False, server_default=text("'0'"), comment='0 => default Hourly, 1 => Quantity')
    item_type = Column(
        ENUM('0', '1'),
        nullable=False,
        server_default=text("'0'"),
        comment="'0' = product, '1' = service.",
    )
    quantity = Column(Integer, comment='Quantity')
    hours = Column(Integer, comment='Hours')
    minutes = Column(Integer, comment='Minutes')
    item_image = Column(TEXT, comment="URL to the item's image")
    # Service lifecycle. Flags flip once, timestamps stay immutable.
    service_started = Column(Boolean, nullable=False, server_default=text("0"),
                             comment="Once supplier marks a service item started. Immutable.")
    service_started_at = Column(TIMESTAMP, nullable=True,
                                comment="When service_started flipped to 1. Immutable once set.")
    service_ended = Column(Boolean, nullable=False, server_default=text("0"),
                           comment="Once supplier marks a service item ended. Immutable.")
    service_ended_at = Column(TIMESTAMP, nullable=True,
                              comment="When service_ended flipped to 1. Immutable once set.")
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the booking was made')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the booking was last updated')

    booking = relationship('Booking')
    supplier_item = relationship('SupplierItem')


class BookingItemImage(Base):
    __tablename__ = 'booking_item_images'
    __table_args__ = (
        Index('ix_booking_item_images_booking_item_id', 'booking_item_id'),
    )

    id = Column(BIGINT, primary_key=True)
    booking_item_id = Column(ForeignKey('booking_items.id', ondelete='CASCADE'), nullable=False)
    image_url = Column(VARCHAR(500), nullable=False)
    sort_order = Column(Integer, nullable=False, server_default=text("'0'"))
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    booking_item = relationship('BookingItem', backref='images')


class BookingAuditLog(Base):
    """Order-level audit trail."""
    __tablename__ = 'booking_audit_log'
    __table_args__ = (
        Index('ix_booking_audit_log_booking_created', 'booking_id', 'created_at'),
        Index('ix_booking_audit_log_event_created', 'event', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True)
    booking_id = Column(ForeignKey('bookings.id', ondelete='CASCADE'), nullable=False,
                        comment='Order this audit event belongs to')
    booking_item_id = Column(ForeignKey('booking_items.id', ondelete='SET NULL'), nullable=True,
                             comment='Specific booking_item when the event is per-item; null for order-level events')
    actor_id = Column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
                      comment='User who performed the action; null for system/cron events')
    event = Column(
        ENUM(
            'service_started', 'service_ended', 'item_type_set',
            'payment_captured', 'review_window_opened', 'transfer_released',
            'transfer_blocked', 'refund_issued', 'admin_decision',
            'customer_confirmed', 'auto_released', 'customer_cancelled',
        ),
        nullable=False,
        comment='Extended by payment-flow events',
    )
    payload = Column(JSON, nullable=True,
                     comment='Free-form event context (before/after values, notes, etc.)')
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    booking = relationship('Booking')
    booking_item = relationship('BookingItem')
    actor = relationship('User')

t_booking_reviews = Table(
    'booking_reviews', metadata,
    Column('id', BIGINT, primary_key=True, autoincrement=True, nullable=False, comment='Unique identifier for the review and rating'),
    Column('sender_id', ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the user'),
    Column('receiver_id', ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the bookings'),
    Column('booking_id', ForeignKey('bookings.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the service provider being reviewed and rated'),
    Column('review_text', TEXT, comment='The content of the review'),
    Column('rating_value', FLOAT, comment='Rating value (e.g., 1.0 to 5.0 stars)'),
    Column('review_anonymous', ENUM('0', '1'), nullable=False, server_default=text("'0'"), comment='0 => display, 1 => not display the name'),
    Column('reviewer_type', ENUM('0', '1'), nullable=False, comment='0 => supplier, 1 => customer'),
    Column('created_at', TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the review or rating was made')
)
