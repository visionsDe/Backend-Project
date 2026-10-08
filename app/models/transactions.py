# coding: utf-8
from sqlalchemy import BigInteger, Boolean, Column, DECIMAL, Date, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, TIMESTAMP, Table, Text, Time, text
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER, TEXT, TINYINT, VARCHAR
from sqlalchemy.orm import relationship
import datetime
from sqlalchemy.ext.declarative import declarative_base
from .base import Base, metadata


class BookingTransaction(Base):
    __tablename__ = 'booking_transactions'
    __table_args__ = (
        Index('ix_booking_transactions_trx_status', 'trx_status'),
        Index('ix_booking_transactions_payment_intent', 'payment_intent'),
        Index('ix_booking_transactions_trx_id', 'trx_id'),
        Index('ix_booking_transactions_created_at', 'created_at'),
        Index('ix_booking_transactions_transfer_status', 'transfer_status'),
        Index('ix_booking_transactions_review_window', 'transfer_status', 'review_window_ends_at'),
        Index('ix_booking_transactions_stripe_charge_id', 'stripe_charge_id'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the transaction')
    booking_id = Column(ForeignKey('bookings.id', ondelete='CASCADE'), nullable=False, index=True, comment='Reference to the bookings')
    amount = Column(DECIMAL(10, 2), comment='Total price of the booking')
    payment_holding_days = Column(Integer)
    payment_intent = Column(VARCHAR(100), nullable=False, comment='Reference to the payment intent')
    trx_id = Column(VARCHAR(50), comment='Reference to the stripe transaction id')
    trx_status = Column(ENUM('0', '1', '2', '3', '4'), nullable=False, server_default=text("'0'"), comment='0 => Failed, 1 => Success 2 => Pending 3 => Expired 4 => canceled')
    trx_response = Column(JSON, comment='transaction stripe response')

    # SCT money-lifecycle. trx_status stays payment-side; transfer_status
    # tracks what has happened with the funds between MEIAPPLI and the supplier.
    is_sct = Column(Boolean, nullable=False, server_default=text("0"),
                    comment='True for orders created under SCT (platform charge). Legacy destination-charge orders stay False.')
    stripe_charge_id = Column(VARCHAR(100), nullable=True,
                              comment='Denormalized charge id; used as Transfer source_transaction and Refund charge')
    transfer_id = Column(VARCHAR(100), nullable=True,
                         comment='Stripe transfer id set once release executes')
    transfer_status = Column(
        ENUM('pending', 'held', 'released', 'blocked', 'refunded', 'failed'),
        nullable=False,
        server_default=text("'pending'"),
        comment='Money-lifecycle state (payment vs payout are decoupled under SCT)',
    )
    participation_fee = Column(DECIMAL(10, 2), nullable=False, server_default=text("0"),
                               comment='MEIAPPLI 8% of the ultimately-not-refunded amount')
    net_amount = Column(DECIMAL(10, 2), nullable=True,
                        comment='Amount actually transferred to the supplier (after fee + Stripe fee)')
    refunded_amount = Column(DECIMAL(10, 2), nullable=False, server_default=text("0"),
                             comment='Sum of successful refunds against this transaction')
    stripe_fee_amount = Column(DECIMAL(10, 2), nullable=True,
                               comment='Stripe processing fee for this charge, pulled from BalanceTransaction')
    captured_at = Column(TIMESTAMP, nullable=True,
                         comment='When funds landed in the MEIAPPLI platform balance')
    released_at = Column(TIMESTAMP, nullable=True, comment='When Transfer to supplier fired')
    refunded_at = Column(TIMESTAMP, nullable=True, comment='Timestamp of the most recent refund on this transaction')
    review_window_ends_at = Column(TIMESTAMP, nullable=True,
                                   comment='Set when order transitions to user_completed; auto-release fires after this')

    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the transaction was added.')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"), comment='Timestamp indicating when the record was last updated.')

    booking = relationship('Booking', back_populates='transactions')
    refunds = relationship('BookingRefund', back_populates='transaction', cascade='all, delete-orphan')


class BookingRefund(Base):
    """One row per Stripe refund against a booking_transaction.

    Full + repeated Partial refunds are both possible in principle, so
    history is preserved here rather than folded into a single column
    on booking_transactions.
    """
    __tablename__ = 'booking_refunds'
    __table_args__ = (
        Index('ix_booking_refunds_transaction', 'booking_transaction_id'),
        Index('ix_booking_refunds_stripe_refund_id', 'stripe_refund_id'),
        Index('ix_booking_refunds_status_created', 'status', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True)
    booking_transaction_id = Column(ForeignKey('booking_transactions.id', ondelete='CASCADE'), nullable=False)
    stripe_refund_id = Column(VARCHAR(100), nullable=True, comment='Stripe re_… id when refund succeeded')
    amount = Column(DECIMAL(10, 2), nullable=False, comment='Refunded amount (positive)')
    reason = Column(TEXT, nullable=True, comment='Admin/customer-supplied reason')
    refunded_by = Column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True,
                         comment='Admin/user who issued the refund; null for system')
    status = Column(
        ENUM('pending', 'succeeded', 'failed'),
        nullable=False,
        server_default=text("'pending'"),
        comment='Local mirror of Stripe refund status',
    )
    stripe_response = Column(JSON, nullable=True, comment='Full Stripe response for audit')
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"))

    transaction = relationship('BookingTransaction', back_populates='refunds')
    actor = relationship('User')
