# coding: utf-8
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, TIMESTAMP, Table, text, Boolean, JSON, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, ENUM, INTEGER, TEXT, TINYINT, VARCHAR
from sqlalchemy.orm import relationship
import datetime
from sqlalchemy.ext.declarative import declarative_base
from .base import Base, metadata
from sqlalchemy.inspection import inspect

class User(Base):
    __tablename__ = 'users'
    __table_args__ = (
        Index('ix_users_status', 'status'),
        Index('ix_users_deleted_at', 'deleted_at'),
        Index('ix_users_created_at', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the user')
    company_name = Column(VARCHAR(255), comment='company name')
    company_name_hash = Column(VARCHAR(255), index=True, comment='Deterministic hash of company_name for encrypted-column search (chat, bookings)')
    first_name = Column(VARCHAR(255), comment="User's first name")
    first_name_hash = Column(VARCHAR(255), comment="User's first name hash")
    last_name = Column(VARCHAR(255), comment="User's last name")
    last_name_hash = Column(VARCHAR(255), comment="User's last name hash")
    gender = Column(ENUM('0', '1', '2'), server_default=text("'2'"), comment='0 = Male, 1 = Female, 2 = default Non identified')
    email = Column(VARCHAR(255), nullable=False, unique=True, comment="User's email address (unique)")
    email_verified_at = Column(TIMESTAMP, comment="it's used to save the email verification datetime")
    password = Column(VARCHAR(255), nullable=False, comment="User's password")
    remember_token = Column(VARCHAR(100))
    profile_img = Column(TEXT, comment="URL to the user's profile image")
    latitude = Column(VARCHAR(255), comment='Latitude')
    longitude = Column(VARCHAR(255), comment='Longitude')
    time_zone = Column(VARCHAR(255), comment='TimeZone')
    information = Column(TEXT, comment='Information about user')
    document_type_id = Column(ForeignKey('document_type.id', ondelete='SET NULL', onupdate='RESTRICT'), nullable=True, index=True)
    document_number = Column(VARCHAR(255), nullable=True)
    document_img = Column(VARCHAR(255), nullable=True)
    organization_id = Column(ForeignKey('organizations.id', ondelete='SET NULL', onupdate='RESTRICT'), nullable=True, index=True)
    organization_document_number = Column(VARCHAR(255), nullable=True)
    organization_document_number_hash = Column(VARCHAR(255), nullable=True)
    organization_document_img = Column(VARCHAR(255), nullable=True)
    status = Column(ENUM('Incomplete', 'Pending', 'Approved', 'Rejected', 'Deleted'), nullable=False, server_default=text("'Incomplete'"))
    status_reason = Column(TEXT, comment='Reason for account status')
    role_id = Column(ForeignKey('roles.id', ondelete='SET NULL'), index=True, server_default='2')
    login_attempts = Column(TINYINT, nullable=False, server_default=text("'0'"))
    password_reset_token = Column(VARCHAR(255))
    password_reset_expires_at = Column(TIMESTAMP)
    password_reset_requested_at = Column(TIMESTAMP)
    address = Column(VARCHAR(255), comment='Address')
    apartment_zone = Column(VARCHAR(255), comment='Apartment zone')
    additional_direction = Column(VARCHAR(255), comment='Additional Direction')
    language_id = Column(ForeignKey('languages.id', ondelete='SET NULL', onupdate='RESTRICT'), nullable=True, index=True)
    country_id = Column(ForeignKey('countries.id', ondelete='SET NULL'), nullable=True, index=True, comment='Country ID referencing the Country')
    city = Column(VARCHAR(255), comment='City')
    ip_address = Column(VARCHAR(255), comment='User IP Address')
    user_ip = Column(VARCHAR(255), comment='IP address from which the last update to the user was made.')
    fcm_token = Column(VARCHAR(255))
    device_type = Column(VARCHAR(255))
    device_type_hash = Column(VARCHAR(255))
    send_push_notification = Column(ENUM('0', '1'), nullable=False, server_default=text("'0'"), comment='0 = default enabled, 1 = disabled')
    send_mail_notification = Column(ENUM('0', '1'), nullable=False, server_default=text("'0'"), comment='0 = default enabled, 1 = disabled')
    about = Column(TEXT, comment='Information about user')
    address_country = Column(VARCHAR(255), comment='Address country of user')
    currency_id = Column(ForeignKey('currencies.id', ondelete='SET NULL'), index=True, comment='Reference to the currency')
    nationality_id = Column(ForeignKey('nationality.id', ondelete='SET NULL'), nullable=True, index=True, comment='Nationality ID referencing the nationality')
    state = Column(VARCHAR(255), comment='State')
    district = Column(VARCHAR(255), comment='District')
    booking_address_response = Column(TEXT, comment='Booking Address response')
    document_session_id = Column(TEXT, comment='Veriff session id')
    document_verification_response = Column(TEXT, comment='Document verification response')
    document_verification_status = Column(VARCHAR(20), nullable=True, comment='Document verification status')
    document_verification_reason = Column(TEXT, comment='Reason for document verification status')
    referral_code = Column(VARCHAR(10), unique=True, nullable=True, comment='Referral code of user')
    referred_by = Column(VARCHAR(10), nullable=True, comment='Referral code of referred user')
    annual_subscription_product_id = Column(VARCHAR(100), nullable=False, default="annually", server_default="annually", comment='[Deprecated — mirrors subscription_products.standard.annual] Annual subscription option')
    monthly_subscription_product_id = Column(VARCHAR(100), nullable=False, default="monthly", server_default="monthly", comment='[Deprecated — mirrors subscription_products.standard.monthly] Monthly subscription option')
    ios_annual_subscription_product_id = Column(VARCHAR(100), nullable=False, default="", server_default="", comment='[Deprecated — mirrors subscription_products.standard.ios_annual] IOS Annual subscription option')
    ios_monthly_subscription_product_id = Column(VARCHAR(100), nullable=False, default="", server_default="", comment='[Deprecated — mirrors subscription_products.standard.ios_monthly] IOS Monthly subscription option')
    subscription_products = Column(JSON, nullable=True, comment='Per-plan product IDs: {standard|gold|silver: {annual, monthly, ios_annual, ios_monthly}}')
    session_token_id = Column(VARCHAR(36), nullable=True, comment='Rotated on every login — JWT sid claim must match this for the token to be accepted, so a fresh login on any device kills sessions on previous devices')
    account_completion_reminder_count = Column(Integer, nullable=False, server_default=text("0"), comment='How many account-completion reminder emails have been sent (0, 1, 2, or 3 — capped at 3)')
    account_completion_reminder_last_sent_at = Column(TIMESTAMP, nullable=True, comment='Timestamp of the most recent account-completion reminder email')
    website_url = Column(VARCHAR(500), nullable=True, comment="User's website URL")
    instagram_url = Column(VARCHAR(500), nullable=True, comment="User's Instagram profile URL")
    created_at = Column(DateTime, default=datetime.datetime.now)
    updated_at = Column(DateTime, onupdate=datetime.datetime.now)
    deleted_at = Column(TIMESTAMP)

    document_type = relationship('DocumentType')
    organization = relationship('Organization')
    language = relationship('Language')
    country = relationship('Country')
    role = relationship('Role')
    currency = relationship('Currency')
    nationality = relationship('Nationality')

    def to_dict(self):
        return {c.key: getattr(self, c.key) for c in inspect(self).mapper.column_attrs}

class Role(Base):
    __tablename__ = 'roles'

    id = Column(INTEGER, primary_key=True)
    name = Column(VARCHAR(100), nullable=False)
    guard_name = Column(VARCHAR(100), nullable=False, unique=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class UserReviewPromptRequest(Base):
    """One row per native review-prompt request."""
    __tablename__ = 'user_review_prompt_requests'

    id = Column(BIGINT, primary_key=True)
    user_id = Column(
        ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    trigger_kind = Column(
        ENUM(
            'customer_first_order',
            'supplier_onboarding',
            'completed_order_1',
            'completed_order_3',
        ),
        nullable=False,
        comment='Which engagement moment fired the request',
    )
    app_version = Column(VARCHAR(50), nullable=True,
                         comment='App version reported by the client at request time')
    requested_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    user = relationship('User')
