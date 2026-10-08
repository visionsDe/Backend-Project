from pydantic import BaseModel, ConfigDict, Field, EmailStr
from typing import List, Optional, Dict, Text
from datetime import datetime
from decimal import Decimal
from fastapi import Query
from app.schemas.bookings import BookingItemImage

class createDocType(BaseModel):
    title: str
    is_active: bool

class CategoryCreateSchema(BaseModel):
    slug: str
    category_icon: str
    parent_id: Optional[int]
    status: str = '1'  # Default to 'Active'

class CategoryTranslationCreate(BaseModel):
    category_id: int
    language_id: int
    name: str
    icon_path: Optional[str] = None

class GetCategories(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    parent_id: Optional[int]=None
    name: Optional[str]=None
    icon_path: Optional[str]=None
    status: str
    sub_categories: List['GetCategories'] = []

class SupplierSubscription(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    supplier_id: int
    order_id: Optional[str] = None
    product_id: Optional[str] = None
    purchase_time: Optional[str] = None
    purchase_state: Optional[int] = None
    purchase_token: Optional[Text] = None
    quantity: Optional[int] = None
    auto_renewing: bool
    acknowledged: bool
    purchase_response: Optional[Dict] = None
    status: Optional[str] = None
    personal_subscription: Optional[bool] = False
    subscription_type: Optional[int] = None  # 0=standard, 1=gold, 2=silver, 3=diamond
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class OrganizationTranslationCreate(BaseModel):
    language_id: int
    org_name: str

class OrganizationMemberDiscountInput(BaseModel):
    """One row of the org's member-discount matrix"""
    subscription_type: str
    android_annual_product_id: Optional[str] = None
    android_monthly_product_id: Optional[str] = None
    ios_annual_product_id: Optional[str] = None
    ios_monthly_product_id: Optional[str] = None


class OrganizationCreate(BaseModel):
    slug: str
    country_id: int
    is_active: Optional[str] = '1'
    free_trial_days: Optional[str] = '0'
    member_discounts: Optional[List[OrganizationMemberDiscountInput]] = None
    translations: List[OrganizationTranslationCreate]

class OrganizationStatusUpdate(BaseModel):
    is_active: Optional[str] = None
    has_document: Optional[str] = None

class OrganizationUpdate(BaseModel):
    country_id: int
    is_active: Optional[str] = '1'
    has_document: bool
    responsible_person_name: Optional[str] = None
    responsible_person_email: Optional[str] = None
    free_trial_days: Optional[str] = None
    member_discounts: Optional[List[OrganizationMemberDiscountInput]] = None
    translations: List[OrganizationTranslationCreate]

class CategoryTranslation(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category_id: int
    language_id: int
    language_name: str
    name: str
    icon_path: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime]

class GetAdminBookings(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_number: str
    booking_date_time: Optional[datetime] = None
    customer_name: str
    customer_company_name: Optional[str] = None
    total_price: Optional[float]
    order_date_time: datetime
    supplier_name: str
    supplier_company_name: Optional[str] = None
    status: str
    currency_code: Optional[str]
    currency_symbol: Optional[str]
    profile_img: Optional[str] = None
    supplier_profile_img: Optional[str] = None
    expense_classification: str = "2"
    is_customer_supplier: bool = False

class GetAdminTransactions(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    booking_id: int
    order_number: str
    gross_amount: Optional[str] = None
    stripe_fee_amount: Optional[str] = None
    participation_fee: Optional[str] = None
    refunded_amount: Optional[str] = None
    net_amount: Optional[str] = None
    payment_holding_days: Optional[int] = None
    stripe_charge_id: Optional[str] = None
    transfer_id: Optional[str] = None
    transfer_status: Optional[str] = None
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    is_sct: bool = False
    created_at: datetime
    updated_at: datetime

class StatisticsResponse(BaseModel):
    total_users: int
    approved_users: int
    total_suppliers: int
    approved_suppliers: int
    total_bookings: int
    total_completed_bookings: int
    total_amount: str
    sales_overview: dict      

class updateBookingDisputeStatus(BaseModel):
    reaction_reason: str
    dispute_status: str


class CreateCountryTranslation(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    language_id: int
    country_name: str

class CountryTranslation(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    country_id: int
    language_id: int
    language_name: str
    language_code: str
    country_name: str
    created_at: datetime
    updated_at: Optional[datetime]

class NationalityTranslation(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    nationality_id: int
    language_id: int
    language_name: str
    language_code: str
    country_name: str
    created_at: datetime
    updated_at: Optional[datetime]

class AddChatMessage(BaseModel):
    body: Optional[str]
    attach_doc: Optional[str] = None

class ChatMessageResponse(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    body: Optional[str]
    message_status: Optional[str]
    attach_doc: Optional[str] = None
    created_at: datetime    

class ChatListResponse(BaseModel):
    conversation_id: int
    type: str
    booking_id: Optional[int]
    order_number: Optional[str]
    status: str
    created_at: datetime
    user_name: list
    user_company_name: Optional[list] = None
    profile_img: Optional[str] = None
    user_id: int
    last_message: Optional[ChatMessageResponse] = None

class GetAdminSettings(BaseModel):
    id: int
    key_name: str
    key: str
    value: Text
    value_type: str
    description: Optional[str]
    created_at : datetime 
    updated_at : Optional[datetime]
    
class UpdateAdminSettings(BaseModel):
    key: str
    value: Text
    value_type: str
    description: Optional[str]

class AddAdminSettings(BaseModel):
    key_name: str
    key: str
    value: Text
    value_type: str
    description: Optional[str]

class UpdateDispute(BaseModel):
    """Admin dispute-action payload."""
    payment_intent: str
    dispute_action: str
    reaction_reason: Optional[str] = None
    amount: Optional[float] = None

class UserAnalytics(BaseModel):
    user_status: Optional[str] = Query(default=None, description="Status of user")
    created_from_date: Optional[str] = Query(None, description="User registered from date e.g 01 Nov 24")
    created_to_date: Optional[str] = Query(None, description="User registered to date e.g 01 Nov 24")
    device_type: Optional[str] = Query(None, description="all / ios / android")
    country:  Optional[str] = Query(None, description="country ids with comma separate")

class UserChartData(BaseModel):
    id: int
    email: str
    first_name: Optional[str]
    country: Optional[int]
    device_type: Optional[str]
    status: str
    created_at: datetime

class BookingsDisputeAdminFilter(BaseModel):
    dispute_status: Optional[str] = Query(default=None, description="Status of dispute")
    payment_status: Optional[str] = Query(default=None, description="Payment status")
    dispute_from_date: Optional[str] = Query(None, description="Dispute from date e.g 01 Nov 24")
    dispute_to_date: Optional[str] = Query(None, description="Dispute to date e.g 01 Nov 24")
    sort_by: Optional[str] = Query('created_at', description="Field to sort by: 'order_number', 'status', 'order_date', 'booking_date', 'customer_name', amount")
    sort_order: Optional[str] = Query('desc', description="Sort order: 'asc' or 'desc'")
    search: Optional[str] = Query(default=None, description="Search Keyword")

class UpdateAdminProfile(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    password: Optional[str] = None    

class BookingAnalytics(BaseModel):
    booking_status: Optional[str] = Query(default=None, description="Status of booking")
    booking_from_date: Optional[str] = Query(None, description="Booking from date e.g 01 Nov 24")
    booking_to_date: Optional[str] = Query(None, description="Booking to date e.g 01 Nov 24")

class TransactionAnalytics(BaseModel):
    created_from_date: Optional[str] = None
    created_to_date: Optional[str] = None
    trx_status: Optional[str] = "all"
    amount_min: Optional[float] = None
    amount_max: Optional[float] = None

class GetAdminBookingItems(BaseModel):
    id: int
    item_name: str
    item_price: str
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    item_image: Optional[str] = None
    item_images: List[BookingItemImage] = []
    supplier_item_id: Optional[int] = None
    item_unit: str
    item_type: str = "0"
    service_started: bool = False
    service_started_at: Optional[datetime] = None
    service_ended: bool = False
    service_ended_at: Optional[datetime] = None
    quantity: int
    hours: int
    minutes: int

class GetAdminBookingDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_number: str
    status: str
    user_id: Optional[int] = None
    supplier_id: Optional[int] = None
    booking_date_time: Optional[datetime] = None
    customer_name: str
    customer_company_name: Optional[str] = None
    booking_address: Optional[str] = None
    booking_address_additional_direction: Optional[str] = None
    supplier_name: str
    supplier_company_name: Optional[str] = None
    additional_fee: Optional[str] = "0.00"
    discount: Optional[str] = "0.00"
    total_price: str
    additional_fee_reason: Optional[str] = None
    booking_additional_information: Optional[str] = None
    customer_email: Optional[EmailStr] = None
    order_date_time: datetime
    payment_mode: str
    latitude: Optional[str] = None
    longitude: Optional[str] = None
    city: Optional[str] = None
    apartment_zone: Optional[str] = None
    cancelled_reason: Optional[str]
    cancelled_by: Optional[int]
    rating: Optional[float] = 0.0
    supplier_email: Optional[EmailStr] = None
    supplier_user_id: Optional[int] = None
    discounted_price: Optional[str] = "0.00"
    currency_code: Optional[str]
    currency_symbol: Optional[str]
    transaction_date: Optional[str]
    bank_details_verified: str
    expense_classification: str = "2"
    is_customer_supplier: bool = False
    order_type: str = "0"
    # SCT money lifecycle
    is_sct: bool = False
    transfer_status: str = "pending"
    participation_fee: Optional[str] = None
    stripe_fee_amount: Optional[str] = None
    net_amount: Optional[str] = None
    refunded_amount: Optional[str] = None
    stripe_charge_id: Optional[str] = None
    transfer_id: Optional[str] = None
    review_window_ends_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    agreement_notes: Optional[str] = None
    booking_items: List[GetAdminBookingItems]


class LanguageDetail(BaseModel):
    id: int
    name: str
    code: str
    icon: Optional[str]


class TranslationModel(BaseModel):
    id: int
    org_name: Optional[str]
    language_detail: Optional[LanguageDetail]


class UserInfoModel(BaseModel):
    id: int
    organization_name: Optional[str]
    organization_number: Optional[str]
    duration_in_days: Optional[int]
    subscription_type: Optional[str] = None
    user_name: Optional[str]
    organization_id: Optional[int]
    country_id: Optional[int]
    user_id: Optional[int]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    membership_status: Optional[str] = None
    activation_date: Optional[datetime] = None
    deactivation_date: Optional[datetime] = None
    deactivation_reason_id: Optional[int] = None
    discount_eligible: Optional[str] = None
    organization_is_active: Optional[str] = None
    access_removed_at: Optional[datetime] = None
    has_active_member_for_identifier: Optional[bool] = None
    has_removed_access_for_identifier: Optional[bool] = None
    member_discounts: Optional[list] = None


class ApprovedUserInfoAdminModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[int]
    first_name: Optional[str]
    last_name: Optional[str]
    information: Optional[str]
    email_verified_at: Optional[datetime] = None
    about: Optional[str]
    email: str
    user_status: Optional[str]
    status_reason: Optional[str]
    company_name: Optional[str]
    gender: str
    time_zone: Optional[str]
    organization_id: Optional[int]
    organization_slug: Optional[str]
    organization_name: Optional[str] = None
    organization_document_number: Optional[str]
    organization_document_img: Optional[str]
    selected_language: str
    selected_language_icon: Optional[str]
    ip_address: Optional[str]
    nationality: Optional[str]
    currency: Optional[str]
    created_at: Optional[datetime] = Field(default=None)
    updated_at: Optional[datetime] = Field(default=None)
    deleted_at: Optional[datetime] = Field(default=None)
    address: Optional[str]
    city: Optional[str]
    country_id: Optional[int]
    country_name: Optional[str]
    apartment_zone: Optional[str]
    additional_direction: Optional[str]
    is_supplier: bool
    supplier_id: Optional[int]
    supplier_status: Optional[str]
    supplier_rejection_reason: Optional[str]
    total_review_as_customer: int
    total_review_as_supplier: int
    supplier_image: Optional[str]
    customer_image: Optional[str]
    is_customer_profile_completed: bool
    total_average_reviews:Optional[float]
    latitude:Optional[str]
    longitude:Optional[str]
    supplier_nationality_flag: Optional[str]
    customer_nationality_flag: Optional[str]
    supplier_user_status: Optional[str]
    supplier_user_status_reason: Optional[str]
    document_verification_status: Optional[str] = None
    document_verification_reason: Optional[str] = None


class ApprovedLogsModel(BaseModel):
    id: int
    approved_user_id: int
    organization_name: str
    organization_identification_number: str
    duration_in_days: int
    organization_id: int
    user_id: Optional[int] = None
    message: str
    action_type: str
    created_at: datetime
    first_name: Optional[str]
    last_name: Optional[str]
    email: Optional[str]


class ApprovedUserInfoResponse(BaseModel):
    user_info: UserInfoModel
    approved_user_info: Optional[ApprovedUserInfoAdminModel] = None
    translations: List[TranslationModel]
    approved_logs: List[ApprovedLogsModel] = None


class GetAllUsersList(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None


class GetReferralSettings(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subscription_type: str
    total_annual_subscription_points: Optional[Decimal] = None
    total_monthly_subscription_points: Optional[Decimal] = None
    is_monthly_points_enabled: bool
    created_at: datetime
    updated_at: datetime


class ItemModerationAction(BaseModel):
    action: str = Field(..., description="'approve' or 'reject'")
    note: Optional[str] = Field(None, description="Required when action is 'reject' — shown to the supplier")


class SubscriptionPlanProducts(BaseModel):
    annual: Optional[str] = None
    monthly: Optional[str] = None
    ios_annual: Optional[str] = None
    ios_monthly: Optional[str] = None


class UpdateSubscriptionProducts(BaseModel):
    standard: Optional[SubscriptionPlanProducts] = None
    gold: Optional[SubscriptionPlanProducts] = None
    silver: Optional[SubscriptionPlanProducts] = None
    diamond: Optional[SubscriptionPlanProducts] = None


class DeactivateApprovedUser(BaseModel):
    reason_id: int


class SetApprovedUserDiscountEligible(BaseModel):
    discount_eligible: str  # '0' or '1'


class RejoinApprovedUser(BaseModel):
    """Payload for rejoining a previously deactivated org member. Creates
    a new active row; the deactivated row is preserved untouched for
    history. Duration in days: 0 means no free access, so the discount
    applies at purchase time instead (only when discount_eligible='1')."""
    duration_in_days: int
    subscription_type: Optional[str] = '0'
    discount_eligible: Optional[str] = '1'


# ─────────────────────────────────────────────────────────────────────
# Broadcast audience configs (admin dashboard)
# ─────────────────────────────────────────────────────────────────────
# Filter DSL vocabulary — kept in Python so schema validation catches typos
# before the row is written. Phase 2's audience filter engine reads this
# too and rejects any DB row that carries an unknown field/operator.
BROADCAST_FILTER_FIELDS = {
    "has_orders": {"value_type": "bool"},
    "orders_completed_count": {"value_type": "int", "needs_window": True},
    "days_since_last_order": {"value_type": "int"},
    "days_since_first_order": {"value_type": "int"},
    "latest_rating": {"value_type": "int"},           # 0-5
    "avg_rating": {"value_type": "float", "needs_window": True},
    "has_rating": {"value_type": "bool"},
    "has_messaged_supplier": {"value_type": "bool"},
    "has_awaiting_confirmation_order": {"value_type": "bool"},
    # Special: compares customer's completed-order count to the supplier's
    # average completed orders per customer, in the same window.
    "orders_vs_supplier_average": {"value_type": "int", "needs_window": True},
}
BROADCAST_FILTER_OPERATORS = {'=', '!=', '>', '>=', '<', '<=', 'IN'}
BROADCAST_LOGIC_MODES = {'AND', 'OR'}


class BroadcastFilterCriterion(BaseModel):
    """One condition inside a broadcast-audience filter."""
    model_config = ConfigDict(from_attributes=True)

    field: str = Field(..., description="Field name from BROADCAST_FILTER_FIELDS")
    operator: str = Field(..., description="One of '=', '!=', '>', '>=', '<', '<=', 'IN'")
    value: object = Field(..., description="Value to compare against; type depends on the field")
    window_days: Optional[int] = Field(
        None,
        description=("Look-back window in days. Required for fields that "
                     "aggregate over time (orders_completed_count, "
                     "avg_rating, orders_vs_supplier_average)."),
    )

    def model_post_init(self, __context):
        spec = BROADCAST_FILTER_FIELDS.get(self.field)
        if spec is None:
            raise ValueError(
                f"unknown filter field: {self.field!r} — "
                f"supported: {sorted(BROADCAST_FILTER_FIELDS)}"
            )
        if self.operator not in BROADCAST_FILTER_OPERATORS:
            raise ValueError(
                f"unknown filter operator: {self.operator!r} — "
                f"supported: {sorted(BROADCAST_FILTER_OPERATORS)}"
            )
        if spec.get("needs_window") and self.window_days is None:
            raise ValueError(f"field {self.field!r} requires window_days")
        if self.operator == 'IN' and not isinstance(self.value, list):
            raise ValueError("operator 'IN' requires value to be a list")
        value_type = spec.get("value_type")
        if value_type == 'bool' and self.operator not in ('=', '!='):
            raise ValueError(
                f"field {self.field!r} is boolean — only '=' and '!=' operators are supported"
            )
        if value_type in ('int', 'float'):
            caster = int if value_type == 'int' else float
            items = self.value if self.operator == 'IN' else [self.value]
            if self.operator == 'IN' and not items:
                raise ValueError(
                    f"field {self.field!r} requires a non-empty list for 'IN'"
                )
            for item in items:
                if item is None or item == '':
                    raise ValueError(
                        f"field {self.field!r} requires a numeric value"
                    )
                try:
                    caster(item)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"field {self.field!r} requires a {value_type} value, "
                        f"got {item!r}"
                    )


class BroadcastFilterDefinition(BaseModel):
    """Full filter definition stored in
    ``broadcast_audience_configs.filter_definition``."""
    model_config = ConfigDict(from_attributes=True)

    logic: str = Field("AND", description="Combine criteria with AND or OR")
    criteria: List[BroadcastFilterCriterion] = Field(
        default_factory=list,
        description="Empty list = no filter (matches all interacting customers)",
    )

    def model_post_init(self, __context):
        if self.logic not in BROADCAST_LOGIC_MODES:
            raise ValueError(f"logic must be one of {sorted(BROADCAST_LOGIC_MODES)}")


_BROADCAST_MIN_TIERS = ('silver', 'gold', 'diamond')


class BroadcastAudienceTranslationIn(BaseModel):
    """One per-language label + description supplied on admin POST/PUT"""
    model_config = ConfigDict(from_attributes=True)

    language_id: int
    label: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None


class BroadcastAudienceTranslationOut(BaseModel):
    """Per-language label + description surfaced by admin GET endpoints."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    language_id: int
    label: str
    description: Optional[str] = None
    language_code: Optional[str] = None
    language_name: Optional[str] = None


class BroadcastAudienceConfigOut(BaseModel):
    """Response shape for the admin GET endpoints."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    label: str
    description: Optional[str] = None
    icon: Optional[str] = None
    is_preset: bool
    is_enabled: bool
    min_tier: str
    filter_definition: dict
    display_order: int
    created_at: datetime
    updated_at: datetime
    translations: List[BroadcastAudienceTranslationOut] = []


class BroadcastAudienceConfigCreate(BaseModel):
    """Admin creates a new custom audience through the dashboard"""
    model_config = ConfigDict(from_attributes=True)

    key: str = Field(..., pattern=r'^[a-z][a-z0-9_]{1,62}[a-z0-9]$',
                     description="snake_case identifier, 3-64 chars")
    label: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=500,
                                description="Path/URL to the uploaded icon")
    is_enabled: bool = True
    min_tier: str = Field(..., description="'silver', 'gold', or 'diamond'")
    filter_definition: BroadcastFilterDefinition
    display_order: int = 0
    translations: List[BroadcastAudienceTranslationIn] = []

    def model_post_init(self, __context):
        if self.min_tier not in _BROADCAST_MIN_TIERS:
            raise ValueError(f"min_tier must be one of {sorted(_BROADCAST_MIN_TIERS)}")


class BroadcastAudienceConfigUpdate(BaseModel):
    """Partial update. Any field left as ``None`` is preserved"""
    model_config = ConfigDict(from_attributes=True)

    label: Optional[str] = Field(None, min_length=1, max_length=128)
    description: Optional[str] = None
    icon: Optional[str] = Field(None, max_length=500)
    is_enabled: Optional[bool] = None
    min_tier: Optional[str] = None
    filter_definition: Optional[BroadcastFilterDefinition] = None
    display_order: Optional[int] = None
    translations: Optional[List[BroadcastAudienceTranslationIn]] = None

    def model_post_init(self, __context):
        if self.min_tier is not None and self.min_tier not in _BROADCAST_MIN_TIERS:
            raise ValueError(f"min_tier must be one of {sorted(_BROADCAST_MIN_TIERS)}")


# ─────────────────────────────────────────────────────────────────────
# Broadcast tier-limit settings (stored as rows in the shared `settings`
# table under keys like ``broadcast_monthly_limit_<tier>``).
# ─────────────────────────────────────────────────────────────────────
class BroadcastTierLimit(BaseModel):
    """One tier's monthly broadcast cap."""
    model_config = ConfigDict(from_attributes=True)

    subscription_type: str   # '0' | '1' | '2' | '3'
    tier_label: str          # 'standard' | 'gold' | 'silver' | 'diamond'
    monthly_limit: Optional[int] = None   # None = unlimited


class BroadcastTierLimitsUpdate(BaseModel):
    """Admin PUT payload. Any tier left None is left untouched. Send
    ``None`` for ``monthly_limit`` to mark that tier unlimited; ``0`` to
    block that tier."""
    model_config = ConfigDict(from_attributes=True)

    standard: Optional[Optional[int]] = None
    gold: Optional[Optional[int]] = None
    silver: Optional[Optional[int]] = None
    diamond: Optional[Optional[int]] = None
