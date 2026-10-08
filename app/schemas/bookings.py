from pydantic import BaseModel, ConfigDict, EmailStr, Field
from typing import Optional, List, Dict, Any
from fastapi import Query
from datetime import datetime
from app.schemas.chat import ChatMessageResponse


EXPENSE_CLASSIFICATIONS = ("0", "1", "2")  # 0 = personal, 1 = business, 2 = non_identified


class CreateBookingItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    supplier_item_id: int
    item_price: float
    item_unit: str
    quantity: int
    hours: int
    minutes: int


class CustomerCancel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    dispute_type_id: int
    reason: Optional[str] = None


class UpdateServiceState(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    action: str

    def model_post_init(self, __context):
        if self.action not in ('start', 'end'):
            raise ValueError("action must be 'start' or 'end'")


class ServiceStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    booking_item_id: int
    service_started: bool = False
    service_started_at: Optional[datetime] = None
    service_ended: bool = False
    service_ended_at: Optional[datetime] = None


class CustomerCancelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    booking_id: int
    status: str


class CreateBooking(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    supplier_id: int
    booking_date_time: Optional[str] = None
    is_delivery: bool
    booking_address: Optional[str] = None
    booking_address_additional_direction: Optional[str] = None
    additional_fee: Optional[float] = 0.0
    discount: Optional[float] = 0.0
    total_price: float
    additional_fee_reason: Optional[str] = None
    booking_additional_information: Optional[str] = None
    payment_mode: str
    latitude: str
    longitude: str
    city: str
    apartment_zone: str
    state: Optional[str] = None
    district: Optional[str] = None
    address_country: Optional[str] = None
    booking_address_response: Optional[Dict[str, Any]] = None
    expense_classification: Optional[str] = Field(
        default="2",
        description="Supplier-as-buyer expense classification. 0 = personal, 1 = business, 2 = non_identified (default).",
    )
    booking_items: List[CreateBookingItem]

    def model_post_init(self, __context):
        if self.expense_classification is None:
            self.expense_classification = "2"
        if self.expense_classification not in EXPENSE_CLASSIFICATIONS:
            raise ValueError(
                f"expense_classification must be one of {list(EXPENSE_CLASSIFICATIONS)}"
            )


class BookingItemImage(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    image_url: str
    sort_order: int


class GetBookingItems(CreateBookingItem):
    id: int
    item_name: str
    item_price: str
    currency_code: Optional[str] = None
    currency_symbol: Optional[str] = None
    item_image: Optional[str] = None
    item_images: List[BookingItemImage] = []
    supplier_item_id: Optional[int] = None
    item_type: str = "0"
    service_started: bool = False
    service_started_at: Optional[datetime] = None
    service_ended: bool = False
    service_ended_at: Optional[datetime] = None

class GetBookingDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_number: str
    status: str
    user_id: int
    supplier_id: int
    booking_date_time: Optional[datetime] = None
    is_delivery: bool
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
    agreement_notes: Optional[str] = None
    customer_email: EmailStr
    order_date_time: datetime
    payment_mode: str
    order_deadline_days: Optional[int] = None
    latitude: Optional[str] = None
    longitude: Optional[str] = None
    city: Optional[str] = None
    apartment_zone: Optional[str] = None
    cancelled_reason: Optional[str]
    cancelled_by: Optional[int]
    rating: Optional[float] = 0.0
    bank_details_verified: str
    supplier_email: EmailStr
    is_review_posted: bool
    is_customer_review_posted: bool
    supplier_user_id: int
    supplier_country_name: Optional[str] = None
    supplier_country_code: Optional[str] = None
    stripe_country_matched: bool = False
    payment_intent: Optional[str] = None
    discounted_price: Optional[str] = "0.00"
    currency_code: Optional[str]
    currency_symbol: Optional[str]
    is_bank_detail_added: bool
    is_cancel_active: bool
    cancel_requires_dispute: bool = False
    transaction_date: Optional[str]
    current_selected_plan_type: Optional[int] = None  # 0=standard, 1=gold, 2=silver, 3=diamond
    expense_classification: str = "2"
    can_update_classification: bool = False
    order_type: str = "0"
    is_sct: bool = False
    transfer_status: str = "pending"
    participation_fee: Optional[str] = None
    net_amount: Optional[str] = None
    refunded_amount: Optional[str] = None
    payment_protection_ends_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    refunded_at: Optional[datetime] = None
    booking_items: List[GetBookingItems]

class GetBookings(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_number: str
    booking_date_time: Optional[datetime] = None
    customer_name: str
    customer_company_name: Optional[str] = None
    total_price: str
    order_date_time: datetime
    supplier_name: str
    supplier_company_name: Optional[str] = None
    status: str
    currency_code: Optional[str]
    currency_symbol: Optional[str]
    profile_img: Optional[str] = None
    expense_classification: str = "2"
    agreement_notes: Optional[str] = None


class NewBookingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    booking_id: int
    order_number: str
    should_request_review: bool = False


class BookingsListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    total: Optional[int] = None
    page: Optional[int] = None
    per_page: Optional[int] = None
    data: List[GetBookings] = []
    statusCode: Optional[int] = 200
    status: Optional[bool] = True
    message: Optional[str] = None
    is_supplier: bool = False
    is_subscription_active: bool = False
    current_selected_plan_type: Optional[int] = None  # 0=standard, 1=gold, 2=silver, 3=diamond

class BookingsFilter(BaseModel):
    order_status: Optional[str] = Query(default=None, description="Status of order")
    order_number: Optional[str] = Query(default=None, description="Order Number")
    order_date: Optional[str] = Query(None, description="Order Date")
    booking_date: Optional[str] = Query(None, description="Booking Date")
    sort_by: Optional[str] = Query('order_date', description="Field to sort by: 'order_number', 'status', 'order_date', 'booking_date', 'customer_name', amount")
    sort_order: Optional[str] = Query('desc', description="Sort order: 'asc' or 'desc'")
    search: Optional[str] = Query(default=None, description="Search Keyword")


class BookingsFilterV2(BaseModel):
    order_status: Optional[str] = Query(default=None, description="Status of order")
    order_number: Optional[str] = Query(default=None, description="Order Number")
    order_from_date: Optional[str] = Query(None, description="Order from date e.g 01 Nov 24. If only this is set, end date defaults to today.")
    order_to_date: Optional[str] = Query(None, description="Order to date e.g 01 Nov 24. If only this is set, all bookings up to this date are returned.")
    booking_from_date: Optional[str] = Query(None, description="Booking from date e.g 01 Nov 24. If only this is set, end date defaults to today.")
    booking_to_date: Optional[str] = Query(None, description="Booking to date e.g 01 Nov 24. If only this is set, all bookings up to this date are returned.")
    sort_by: Optional[str] = Query('order_date', description="Field to sort by: 'order_number', 'status', 'order_date', 'booking_date', 'customer_name', amount")
    sort_order: Optional[str] = Query('desc', description="Sort order: 'asc' or 'desc'")
    search: Optional[str] = Query(default=None, description="Search Keyword")

class BookingsAdminFilter(BaseModel):
    order_status: Optional[str] = Query(default=None, description="Status of order")
    order_number: Optional[str] = Query(default=None, description="Order Number")
    order_from_date: Optional[str] = Query(None, description="Order from date e.g 01 Nov 24")
    order_to_date: Optional[str] = Query(None, description="Order to date e.g 01 Nov 24")
    booking_from_date: Optional[str] = Query(None, description="Booking from Date e.g 01 Nov 24")
    booking_to_date: Optional[str] = Query(None, description="Booking to Date e.g 01 Nov 24")
    sort_by: Optional[str] = Query('order_date', description="Field to sort by: 'order_number', 'status', 'order_date', 'booking_date', 'customer_name', amount")
    sort_order: Optional[str] = Query('desc', description="Sort order: 'asc' or 'desc'")
    search: Optional[str] = Query(default=None, description="Search Keyword")
    expense_classification: Optional[str] = Query(
        default=None,
        description="Filter by supplier-as-buyer classification: 0 = personal, 1 = business, 2 = non_identified",
    )


class UpdateBooking(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    additional_fee: Optional[float] = 0.0
    discount: Optional[float] = 0.0
    additional_fee_reason: Optional[str] = None
    payment_mode: str
    status: str


class UpdateAgreementNotes(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    agreement_notes: Optional[str] = None


class UpdateExpenseClassification(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    expense_classification: str = Field(..., description="0 = personal, 1 = business, 2 = non_identified")

    def model_post_init(self, __context):
        if self.expense_classification not in EXPENSE_CLASSIFICATIONS:
            raise ValueError(
                f"expense_classification must be one of {list(EXPENSE_CLASSIFICATIONS)}"
            )


class CreateReview(BaseModel):
    review_text: Optional[str] = None
    rating_value: float
    review_anonymous: str


class GetDisputeType(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: Optional[str]=None


class CreateDispute(BaseModel):
    dispute_type_id: int

class BookingDisputeResponse(BaseModel):
    dispute_id: int
    conversation_id: Optional[int] = None
    conversation_status: Optional[str] = 'not_started'
    created_at: Optional[datetime] = None
    booking_id: Optional[int] = None
    sender_id: Optional[int] = None
    order_number: str
    dispute_reason: str
    participants: Optional[List] = []
    messages: Optional[List[ChatMessageResponse]] = []


class GetDisputedBookings(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    booking_id: int
    order_number: str
    dispute_type_id: int
    dispute_reason: str
    dispute_status: str
    created_by: int