# Standard library
import io
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Third-party
import stripe
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# FastAPI
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer

# SQLAlchemy
from sqlalchemy import asc, cast, Date, desc, func, or_
from sqlalchemy.orm import Session, joinedload

# App
from app import models
from app.controller.base_controller import BaseController
from app.controller.pagination_controller import PaginationResponse
from app.database import get_db
from app.helpers.messages import messages
from app.schemas import bookings as booking_model
from app.utils.auth import user_required
from app.utils.email_utils import send_order_email, get_email_template_content, send_received_order_email, send_new_order_email
from app.utils.encryption import decrypt_data
from app.utils.bookings import resolve_caller_plan_type
from app.utils.helper import extract_payment_intent, convert_date_time_with_number, convert_date_with_number
from app.utils.jwt_token import verify_access_token
from app.utils.logging import setup_logger
from app.utils.stripe_client import (
    StripeCountryNotSupported,
    map_stripe_requirements_to_status,
    stripe_api_key_for,
    stripe_country_for_platform,
    stripe_platform_for_supplier,
)
from app.utils.notification_utils import send_push_notification, get_notification_data

logger = setup_logger()


router = APIRouter(
    prefix="",
    tags=["Bookings"]
)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


_DEFAULT_REVIEW_WINDOW_DAYS = 7
_DEFAULT_MEIAPPLI_PARTICIPATION_FEE_RATE = Decimal("0.08")


def _get_review_window_days(db: Session) -> int:
    """SCT review-window length (post supplier_completed → auto-release), also
    stamped onto ``booking_transactions.payment_holding_days``. Sourced from
    the ``online_payment_holding_period`` Setting so ops can tune it without
    a deploy; falls back to 7 when the row is missing or unparseable."""
    row = (
        db.query(models.Setting)
        .filter(models.Setting.key_name == 'online_payment_holding_period')
        .first()
    )
    if row and row.value:
        try:
            days = int(row.value)
            if days > 0:
                return days
        except (TypeError, ValueError):
            pass
    return _DEFAULT_REVIEW_WINDOW_DAYS


def _get_participation_fee_rate(db: Session) -> Decimal:
    """MEIAPPLI participation-fee rate (fraction of non-refunded amount).
    Sourced from the ``meiappli_participation_fee_rate`` Setting; falls
    back to 0.08 (8%) when the row is missing or unparseable."""
    row = (
        db.query(models.Setting)
        .filter(models.Setting.key_name == 'meiappli_participation_fee_rate')
        .first()
    )
    if row and row.value:
        try:
            raw = Decimal(str(row.value))
            rate = raw if raw < Decimal("1") else raw / Decimal("100")
            if Decimal("0") <= rate <= Decimal("1"):
                return rate
        except Exception:
            pass
    return _DEFAULT_MEIAPPLI_PARTICIPATION_FEE_RATE


def _decimal(value) -> Decimal:
    """Coerce Float/Decimal/None columns into a stable Decimal for math.
    Missing / None becomes 0. Prevents float drift in fee/refund math."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def compute_release_amounts(db: Session, txn) -> tuple[Decimal, Decimal]:
    """Return (participation_fee, net_to_supplier) for the current state
    of a booking_transaction. MEIAPPLI participation fee (rate from
    settings, defaults to 8%) applies to the amount that is ultimately
    NOT refunded. Stripe processing fee is deducted from the
    entrepreneur's side (their responsibility per business rule)."""
    remaining = _decimal(txn.amount) - _decimal(txn.refunded_amount)
    if remaining < 0:
        remaining = Decimal("0")
    rate = _get_participation_fee_rate(db)
    participation_fee = (remaining * rate).quantize(Decimal("0.01"))
    stripe_fee = _decimal(txn.stripe_fee_amount)
    net = (remaining - participation_fee - stripe_fee).quantize(Decimal("0.01"))
    return participation_fee, net


def _open_review_window(db: Session, booking, txn, *, actor_id: Optional[int]) -> None:
    """Called when the supplier marks the order as supplier_completed. Sets
    the dispute deadline; auto-release cron fires the transfer
    when the deadline passes and no dispute is open."""
    if txn.review_window_ends_at is not None:
        return
    days = _get_review_window_days(db)
    now = datetime.now()
    txn.payment_holding_days = days
    txn.review_window_ends_at = now + timedelta(days=days)
    if txn.transfer_status == 'pending':
        txn.transfer_status = 'held'
    db.add(models.BookingAuditLog(
        booking_id=booking.id,
        actor_id=actor_id,
        event='review_window_opened',
        payload={
            "window_ends_at": txn.review_window_ends_at.isoformat(),
            "days": days,
        },
    ))
    db.commit()


_SUPPLIER_COMPLETED_STATUSES = ('supplier_completed', 'user_completed', 'complete')

# Terminal statuses — money already released, refunded, or never taken.
_TERMINAL_STATUSES = ('cancelled', 'rejected', 'complete_closed')


def _create_dispute_conversation(db: Session, booking) -> Optional[int]:
    """Create a fresh dispute conversation and participants for the booking."""
    supplier_user_id = booking.supplier.user_id if booking.supplier else None
    if not booking.user_id or not supplier_user_id:
        return None
    conversation = models.Conversations(
        chat_type='dispute',
        status='active',
        booking_id=booking.id,
    )
    db.add(conversation)
    db.flush()
    db.bulk_save_objects([
        models.ConversationParticipants(
            conversation_id=conversation.id,
            participant_id=booking.user_id,
            status='active',
        ),
        models.ConversationParticipants(
            conversation_id=conversation.id,
            participant_id=supplier_user_id,
            status='active',
        ),
    ])
    return conversation.id


def _send_cancel_dispute_push(db: Session, booking) -> None:
    """Notify both sides that a customer-cancel escalated to a dispute."""
    customer = booking.user1
    if customer is not None and customer.fcm_token and customer.send_push_notification == "0":
        payload = get_notification_data("customer_cancel_dispute", customer.language_id, db)
        if payload:
            payload["order_id"] = str(booking.id)
            payload["type"] = "placed_order"
            send_push_notification(
                token=customer.fcm_token,
                title=payload["title"],
                body=payload["body"],
                platform=decrypt_data(customer.device_type),
                data=payload,
            )
    supplier_user = booking.supplier.user if booking.supplier else None
    if supplier_user is not None and supplier_user.fcm_token and supplier_user.send_push_notification == "0":
        payload = get_notification_data("supplier_cancel_dispute", supplier_user.language_id, db)
        if payload:
            payload["order_id"] = str(booking.id)
            payload["type"] = "received_order"
            send_push_notification(
                token=supplier_user.fcm_token,
                title=payload["title"],
                body=payload["body"],
                platform=decrypt_data(supplier_user.device_type),
                data=payload,
            )


def _maybe_send_review_prompt_push(db: Session, booking) -> None:
    """After a customer-side transition to user_completed, request an
    additional review at the 1st and 3rd completed-order milestones."""
    from app.utils.review_prompt import (
        TRIGGER_COMPLETED_ORDER_1, TRIGGER_COMPLETED_ORDER_3,
        record_prompt, should_request_followup,
    )
    customer = booking.user1
    if customer is None:
        return
    completed_count = (
        db.query(models.Booking)
        .filter(
            models.Booking.user_id == customer.id,
            models.Booking.status.in_(_SUPPLIER_COMPLETED_STATUSES),
        )
        .count()
    )
    if completed_count == 1:
        trigger = TRIGGER_COMPLETED_ORDER_1
        expected_prior = 1
    elif completed_count == 3:
        trigger = TRIGGER_COMPLETED_ORDER_3
        expected_prior = 2
    else:
        return
    if not should_request_followup(db, customer.id, expected_prior):
        return
    record_prompt(db, customer.id, trigger, None)
    db.commit()
    if customer.fcm_token and customer.send_push_notification == "0":
        notification_obj = get_notification_data("review_request", customer.language_id, db)
        if notification_obj:
            send_push_notification(
                token=customer.fcm_token,
                title=notification_obj["title"],
                body=notification_obj["body"],
                platform=decrypt_data(customer.device_type),
                data=notification_obj,
            )


def _cancel_requires_dispute(db: Session, booking) -> tuple[bool, list[int]]:
    """Decide whether a customer action must go through admin review."""
    started_item_ids = [
        bi.id for bi in (
            db.query(models.BookingItem)
            .filter(
                models.BookingItem.booking_id == booking.id,
                models.BookingItem.item_type == '1',
                models.BookingItem.service_started_at.isnot(None),
            )
            .all()
        )
    ]
    requires_dispute = bool(started_item_ids) or booking.status in _SUPPLIER_COMPLETED_STATUSES
    return requires_dispute, started_item_ids


def _refund_transaction_full(
    db: Session, txn, *, api_key: str, actor_id: Optional[int], reason: str,
) -> Optional[models.BookingRefund]:
    """Issue a full refund of the remaining (non-refunded) amount on this
    SCT transaction. Writes a booking_refunds row + audit log. Returns
    the local refund row (None if Stripe call failed).
    """
    remaining = _decimal(txn.amount) - _decimal(txn.refunded_amount)
    if remaining <= 0:
        return None
    refund_row = models.BookingRefund(
        booking_transaction_id=txn.id,
        amount=remaining,
        reason=reason,
        refunded_by=actor_id,
        status='pending',
    )
    db.add(refund_row)
    db.flush()
    try:
        booking = txn.booking
        stripe_refund = stripe.Refund.create(
            payment_intent=txn.payment_intent,
            amount=int(remaining * 100),
            api_key=api_key,
            metadata={
                "meiappli_order_number": booking.order_number if booking else "",
                "booking_id": str(txn.booking_id or ""),
                "refund_type": "full",
                "refund_reason": reason or "",
            },
        )
        refund_row.stripe_refund_id = stripe_refund.get('id')
        refund_row.status = 'succeeded'
        refund_row.stripe_response = stripe_refund
        txn.refunded_amount = _decimal(txn.refunded_amount) + remaining
        txn.refunded_at = datetime.now()
        txn.transfer_status = 'refunded'
        txn.participation_fee = Decimal("0")
        txn.net_amount = Decimal("0")
        db.add(models.BookingAuditLog(
            booking_id=txn.booking_id,
            actor_id=actor_id,
            event='refund_issued',
            payload={
                "amount": str(remaining),
                "reason": reason,
                "stripe_refund_id": refund_row.stripe_refund_id,
            },
        ))
        db.commit()
    except Exception as exc:
        logger.error("Refund failed for booking_transaction %s: %s", txn.id, exc)
        refund_row.status = 'failed'
        db.commit()
    return refund_row


def _execute_release(
    db: Session, booking, txn, *, api_key: str, actor_id: Optional[int], event_name: str,
) -> Optional[str]:
    """Fire the Stripe transfer that pays the supplier the net amount
    (after MEIAPPLI 8% on the non-refunded portion and after Stripe's
    processing fee). Records transfer_id + status, writes audit rows.
    Returns the transfer id on success, None on failure."""
    if txn.transfer_status == 'released':
        return txn.transfer_id
    supplier = booking.supplier
    if not supplier or not supplier.stripe_account_id:
        logger.error("Cannot release booking_transaction %s: supplier or stripe_account_id missing", txn.id)
        return None
    participation_fee, net = compute_release_amounts(db, txn)
    if net <= 0:
        db.add(models.BookingAuditLog(
            booking_id=booking.id,
            actor_id=actor_id,
            event='admin_decision',
            payload={
                "reason": "net_to_supplier_non_positive",
                "participation_fee": str(participation_fee),
                "net_amount": str(net),
            },
        ))
        txn.participation_fee = participation_fee
        txn.net_amount = net
        db.commit()
        return None
    if not txn.stripe_charge_id and txn.payment_intent:
        try:
            pi = stripe.PaymentIntent.retrieve(txn.payment_intent, api_key=api_key)
            latest_charge = pi.get('latest_charge')
            if latest_charge:
                txn.stripe_charge_id = latest_charge
                db.commit()
        except Exception as exc:
            logger.error("PaymentIntent retrieve for charge backfill failed on booking_transaction %s: %s", txn.id, exc)

    if not txn.stripe_charge_id:
        logger.error("Cannot release booking_transaction %s: stripe_charge_id missing (source_transaction is mandatory for BR)", txn.id)
        txn.transfer_status = 'failed'
        db.add(models.BookingAuditLog(
            booking_id=booking.id,
            actor_id=actor_id,
            event='transfer_blocked',
            payload={"reason": "missing_source_transaction"},
        ))
        db.commit()
        return None

    try:
        transfer = stripe.Transfer.create(
            amount=int(net * 100),
            currency=booking.currency_code if booking.currency_code else "BRL",
            destination=supplier.stripe_account_id,
            source_transaction=txn.stripe_charge_id,
            api_key=api_key,
            metadata={
                "meiappli_order_number": booking.order_number or "",
                "booking_id": str(booking.id),
                "participation_fee": f"{participation_fee:.2f}",
            },
        )
    except Exception as exc:
        logger.error("Transfer failed for booking_transaction %s: %s", txn.id, exc)
        txn.transfer_status = 'failed'
        db.add(models.BookingAuditLog(
            booking_id=booking.id,
            actor_id=actor_id,
            event='transfer_blocked',
            payload={"reason": "transfer_call_failed", "error": str(exc)},
        ))
        db.commit()
        return None
    txn.transfer_id = transfer.get('id')
    txn.transfer_status = 'released'
    txn.participation_fee = participation_fee
    txn.net_amount = net
    txn.released_at = datetime.now()
    if booking.status not in ('cancelled', 'rejected'):
        booking.status = 'complete_closed'
    db.add(models.BookingAuditLog(
        booking_id=booking.id,
        actor_id=actor_id,
        event=event_name,
        payload={
            "transfer_id": txn.transfer_id,
            "net_amount": str(net),
            "participation_fee": str(participation_fee),
            "stripe_fee_amount": str(_decimal(txn.stripe_fee_amount)),
        },
    ))
    db.commit()
    return txn.transfer_id


def _derive_order_type(booking_items) -> str:
    """Derive an order-level type from the item_type snapshot on
    each booking_item. Returns '0' when every item is a product, '1' when
    every item is a service, '2' when the order is mixed. Empty order
    falls back to '0' (product) as the safest neutral default."""
    types = {getattr(bi, 'item_type', '0') for bi in booking_items}
    if not types:
        return '0'
    if len(types) == 1:
        return next(iter(types))
    return '2'

_DAY_SLUGS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_booking_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 or 'dd/mm/yy HH:MM' booking date/time. Returns None when empty."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return convert_date_time_with_number(value)


def _is_supplier_available_at(db: Session, supplier_id: int, booking_date_obj: datetime) -> bool:
    """True when the supplier has an active schedule window covering this datetime."""
    day_slug = _DAY_SLUGS[booking_date_obj.weekday()]
    week_day = db.query(models.WeekDay).filter_by(slug=day_slug).first()
    if not week_day:
        return False
    window = (
        db.query(models.SupplierItemSchedule)
        .filter(
            models.SupplierItemSchedule.supplier_id == supplier_id,
            models.SupplierItemSchedule.day_id == week_day.id,
            models.SupplierItemSchedule.availability == '1',
            models.SupplierItemSchedule.start_time <= booking_date_obj.time(),
            models.SupplierItemSchedule.end_time >= booking_date_obj.time(),
        )
        .first()
    )
    return window is not None


# ──────────────────────────────────────────────
# Bookings CRUD
# ──────────────────────────────────────────────
@router.get("/check-supplier-availability", dependencies=[Depends(user_required)])
def check_supplier_availability(
    supplier_id: int = Query(..., description="Supplier ID to check"),
    booking_date_time: str = Query(
        ...,
        description="Booking date/time (ISO 8601 e.g. '2026-01-15T14:30:00Z', or 'dd/mm/yy HH:MM').",
    ),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Check whether a supplier has an active schedule window covering the given date/time.
    Mirrors the availability guard in create-bookings so clients can pre-validate."""
    supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not supplier:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    booking_date_obj = _parse_booking_datetime(booking_date_time)
    if booking_date_obj is None:
        return BaseController.errorGeneral(
            messages[selected_language]['supplier_not_available_for_booking'], 400,
        )
    is_available = _is_supplier_available_at(db, supplier_id, booking_date_obj)
    return BaseController.success({"is_available": is_available})


@router.post("/create-bookings", dependencies=[Depends(user_required)])
def create_bookings(
    item: booking_model.CreateBooking,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    supplier = (db.query(models.Supplier)
                .filter(models.Supplier.id == item.supplier_id)
                .options(joinedload(models.Supplier.user).joinedload(models.User.currency))
                .first())
    if not supplier:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    booking_date_obj = _parse_booking_datetime(item.booking_date_time)

    if booking_date_obj is not None:
        if not _is_supplier_available_at(db, item.supplier_id, booking_date_obj):
            return BaseController.errorGeneral(
                messages[selected_language]['supplier_not_available_for_booking'], 400,
            )
    order_deadline_days = 30
    setting_obj = db.query(models.Setting).filter(models.Setting.key_name == "offline_order_deadline").first()
    if setting_obj:
        try:
            order_deadline_days = int(setting_obj.value)
        except Exception as e:
            logger.error("Deadline days error: %s", e)
            order_deadline_days = 30
    country_id = None
    if item.address_country:
            country = (
                db.query(models.Country)
                .join(models.CountryTranslation)
                .filter(
                    models.CountryTranslation.country_name == item.address_country
                )
                .first()
            )
            if not country:
                return BaseController.errorGeneral(messages[selected_language]['country_not_found'], 400)
            country_id = country.id

    new_booking = models.Booking(
        user_id = user.id,
        supplier_id = item.supplier_id,
        booking_date_time = booking_date_obj,
        is_delivery = "1" if item.is_delivery else "0",
        customer_name = f"{decrypt_data(user.first_name)} {decrypt_data(user.last_name)}",
        customer_company_name = decrypt_data(user.company_name) if user.company_name else None,
        booking_address = item.booking_address,
        booking_address_additional_direction = item.booking_address_additional_direction,
        supplier_name = f"{decrypt_data(supplier.user.first_name)} {decrypt_data(supplier.user.last_name)}",
        supplier_company_name = decrypt_data(supplier.user.company_name) if supplier.user.company_name else None,
        additional_fee = item.additional_fee,
        discount = item.discount,
        total_price = item.total_price,
        additional_fee_reason = item.additional_fee_reason,
        booking_additional_information = item.booking_additional_information,
        customer_email = user.email,
        payment_mode = item.payment_mode,
        latitude = item.latitude,
        longitude = item.longitude,
        city = item.city,
        apartment_zone = item.apartment_zone,
        order_date_time = datetime.now(),
        currency_code = supplier.user.currency.code if supplier.user.currency else 'BRL',
        currency_symbol = supplier.user.currency.symbol if supplier.user.currency else None,
        order_deadline_days = order_deadline_days,
        state = item.state,
        district = item.district,
        country_id = country_id,
        booking_address_response = item.booking_address_response,
        expense_classification = item.expense_classification or "2",
    )
    db.add(new_booking)
    db.flush()
    new_booking.order_number = f"#{new_booking.supplier_id:03d}-{new_booking.id:03d}"
    for booking_item in item.booking_items:
        supplier_item = db.query(models.SupplierItem).filter(models.SupplierItem.id == booking_item.supplier_item_id).first()
        if not supplier_item:
            return BaseController.errorGeneral(messages[selected_language]['supplier_item_not_found'], status.HTTP_404_NOT_FOUND)
        new_booking_item = models.BookingItem(
            booking_id=new_booking.id,
            supplier_item_id=booking_item.supplier_item_id,
            item_name=supplier_item.item_name,
            item_price=booking_item.item_price,
            item_unit=booking_item.item_unit,
            item_type=supplier_item.item_type,
            quantity=booking_item.quantity,
            hours=booking_item.hours,
            minutes=booking_item.minutes,
            item_image=supplier_item.item_image
        )
        db.add(new_booking_item)
        db.flush()
        gallery = (
            db.query(models.ItemImage)
            .filter(
                models.ItemImage.supplier_item_id == supplier_item.id,
                models.ItemImage.deleted_at.is_(None),
            )
            .order_by(models.ItemImage.sort_order.asc(), models.ItemImage.id.asc())
            .all()
        )
        for img in gallery:
            db.add(models.BookingItemImage(
                booking_item_id=new_booking_item.id,
                image_url=img.image_url,
                sort_order=img.sort_order,
            ))
    db.commit()
    if user.fcm_token and user.send_push_notification == "0":
        customer_notification_obj = get_notification_data("placed_order", user.language_id, db)
        if customer_notification_obj:
            customer_notification_obj["order_id"] = str(new_booking.id)
            send_push_notification(
                token=user.fcm_token,
                title=customer_notification_obj["title"],
                body=customer_notification_obj["body"],
                platform=decrypt_data(user.device_type),
                data=customer_notification_obj
            )
    if supplier.user.fcm_token and supplier.user.send_push_notification == "0":
        supplier_notification_obj = get_notification_data("received_order", supplier.user.language_id, db)
        if supplier_notification_obj:
            supplier_notification_obj["order_id"] = str(new_booking.id)
            send_push_notification(
                token=supplier.user.fcm_token,
                title=supplier_notification_obj["title"],
                body=supplier_notification_obj["body"],
                platform=decrypt_data(supplier.user.device_type),
                data=supplier_notification_obj
            )
    customer_template_content = get_email_template_content(db, user.language_id, 'new-order')
    supplier_template_content = get_email_template_content(db, supplier.user.language_id, 'received-order')
    if customer_template_content and user.send_mail_notification == "0":
        background_tasks.add_task(
            send_new_order_email,
            email=user.email,
            order_number=new_booking.order_number,
            subject=customer_template_content.subject,
            body=customer_template_content.body,
            first_name=decrypt_data(user.first_name),
            last_name=decrypt_data(user.last_name),
            booking_id=new_booking.order_number,
            customer_name=new_booking.customer_name,
            service_provider_name=new_booking.supplier_name,
            price=f"{new_booking.supplier.user.currency.symbol}{str(new_booking.total_price)}",
            booking_date=(new_booking.order_date_time).strftime("%d/%m/%Y"),
            booking_time=(new_booking.order_date_time).strftime("%H:%M"),
            status=messages[user.language.code][new_booking.status],
            payment_mode=messages[supplier.user.language.code]["offline_booking"],
            selected_language=user.language.code
        )
    if supplier_template_content and supplier.user.send_mail_notification == "0":
        background_tasks.add_task(
            send_received_order_email,
            email=supplier.user.email,
            order_number=new_booking.order_number,
            subject=supplier_template_content.subject,
            body=supplier_template_content.body,
            service_provider_fname=decrypt_data(supplier.user.first_name),
            service_provider_lname=decrypt_data(supplier.user.last_name),
            booking_id=new_booking.order_number,
            customer_name=new_booking.customer_name,
            service_provider_name=new_booking.supplier_name,
            price=f"{new_booking.supplier.user.currency.symbol}{str(new_booking.total_price)}",
            booking_date=(new_booking.order_date_time).strftime("%d/%m/%Y"),
            booking_time=(new_booking.order_date_time).strftime("%H:%M"),
            status=messages[supplier.user.language.code][new_booking.status],
            payment_mode=messages[supplier.user.language.code]["offline_booking"],
            selected_language=supplier.user.language.code
        )
    from app.utils.review_prompt import should_request_first_attempt

    should_request_review = should_request_first_attempt(db, user.id)
    return BaseController.success(
        [booking_model.NewBookingResponse.model_validate({
            "booking_id": new_booking.id,
            "order_number": new_booking.order_number,
            "should_request_review": should_request_review,
        })],
        messages[selected_language]['booking_created'],
    )


@router.get("/get-bookings")
def get_supplier_bookings(
    filters: booking_model.BookingsFilter = Depends(),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(user_required),
    selected_language: Optional[str] = Cookie(default='en'),
    booking_type: str = Query(default="received", description="Type of booking"),
    page: int = Query(default=0, ge=0, description="Page number for pagination"),
    limit: int = Query(default=10, ge=1, le=100, description="Number of bookings to return per page"),
):
    if booking_type not in ('received', 'placed'):
        return BaseController.errorGeneral(
            messages[selected_language]['wrong_booking_type'],
            status.HTTP_404_NOT_FOUND,
        )

    supplier = db.query(models.Supplier).filter(models.Supplier.user_id == current_user.id).first()
    if not supplier and booking_type == "received":
        return PaginationResponse(
            total=0,
            page=page,
            per_page=limit,
            data=[],
            message=messages[selected_language]['user_bookings'],
        )

    if booking_type == "received":
        bookings_query = db.query(models.Booking).filter(
            models.Booking.supplier_id == supplier.id,
            models.Booking.user_id.isnot(None),
            models.Booking.supplier_id.isnot(None),
        )
    else:
        bookings_query = db.query(models.Booking).filter(
            models.Booking.user_id == current_user.id,
            models.Booking.user_id.isnot(None),
            models.Booking.supplier_id.isnot(None),
        )

    if filters.order_status and filters.order_status.lower() != "all":
        status_filter = (
            ["complete", "user_completed"]
            if filters.order_status.lower() == "complete"
            else [filters.order_status.lower()]
        )
        bookings_query = bookings_query.filter(models.Booking.status.in_(status_filter))

    if filters.order_number:
        bookings_query = bookings_query.filter(
            models.Booking.order_number.ilike(f"%{filters.order_number}%")
        )

    if filters.order_date:
        bookings_query = bookings_query.filter(
            cast(models.Booking.order_date_time, Date) == convert_date_with_number(filters.order_date)
        )

    if filters.booking_date:
        bookings_query = bookings_query.filter(
            cast(models.Booking.booking_date_time, Date) == convert_date_with_number(filters.booking_date)
        )

    if filters.search:
        filters.search = filters.search.strip()
    if filters.search:
        search_term = f"%{filters.search}%"
        bookings_query = bookings_query.filter(
            or_(
                models.Booking.customer_name.ilike(search_term),
                models.Booking.supplier_name.ilike(search_term),
                models.Booking.order_number.ilike(search_term),
                models.Booking.status.ilike(search_term.lower()),
            )
        )

    total_bookings = bookings_query.count()

    sort_columns = {
        "order_number": models.Booking.order_number,
        "status": models.Booking.status,
        "order_date": models.Booking.order_date_time,
        "booking_date": models.Booking.booking_date_time,
        "name": models.Booking.customer_name,
        "amount": models.Booking.total_price,
    }
    if filters.sort_by in sort_columns:
        sort_column = sort_columns[filters.sort_by]
        bookings_query = bookings_query.order_by(
            asc(sort_column) if filters.sort_order == 'asc' else desc(sort_column)
        )

    bookings = bookings_query.offset(page * limit).limit(limit).all()

    response_data = [
        booking_model.GetBookings(
            id=booking.id,
            order_number=booking.order_number,
            booking_date_time=booking.booking_date_time,
            customer_name=booking.customer_name,
            customer_company_name=booking.customer_company_name,
            total_price=f"{round((booking.total_price + booking.additional_fee - booking.discount), 2):.2f}",
            order_date_time=booking.order_date_time,
            supplier_name=booking.supplier_name,
            supplier_company_name=booking.supplier_company_name,
            status=booking.status,
            currency_code=booking.currency_code,
            currency_symbol=booking.currency_symbol,
            profile_img=booking.user1.profile_img if booking_type == "received" else booking.supplier.user.profile_img,
            agreement_notes=booking.agreement_notes,
        )
        for booking in bookings
    ]

    return PaginationResponse(
        total=total_bookings,
        page=page,
        per_page=limit,
        data=response_data,
        message=messages[selected_language]['user_bookings'],
    )


@router.get("/get-bookings/export-pdf", dependencies=[Depends(user_required)])
def export_supplier_bookings_pdf(
    filters: booking_model.BookingsFilterV2 = Depends(),
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
    booking_type: str = Query(default="received", description="Type of booking: 'received' or 'placed'"),
    user_timezone: Optional[str] = Query(
        default=None,
        description="IANA timezone (e.g. America/Sao_Paulo) — all dates in the PDF are rendered in this zone. Server time (UTC) when omitted.",
    ),
):
    """Same filters as /get-bookings, but returns a PDF of ALL matching rows (no pagination)."""
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    # Gate: PDF export is available on Gold and Diamond tiers.
    if resolve_caller_plan_type(db, user) not in (1, 3):
        return BaseController.errorGeneral(
            messages[selected_language]['gold_or_diamond_subscription_required'],
            status.HTTP_403_FORBIDDEN,
        )
    if booking_type not in ['received', 'placed']:
        return BaseController.errorGeneral(messages[selected_language]['wrong_booking_type'], status.HTTP_404_NOT_FOUND)

    tz = None
    if user_timezone:
        try:
            tz = ZoneInfo(user_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return BaseController.errorGeneral(
                messages[selected_language]['invalid_timezone_report'], status.HTTP_400_BAD_REQUEST,
            )

    supplier = db.query(models.Supplier).filter(models.Supplier.user_id == user.id).first()
    if not supplier and booking_type == "received":
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)

    if booking_type == "received":
        bookings_query = db.query(models.Booking).filter(
            models.Booking.supplier_id == supplier.id,
            models.Booking.user_id.isnot(None),
            models.Booking.supplier_id.isnot(None),
        )
    else:
        bookings_query = db.query(models.Booking).filter(
            models.Booking.user_id == user.id,
            models.Booking.user_id.isnot(None),
            models.Booking.supplier_id.isnot(None),
        )

    # Same filter logic as get_supplier_bookings
    if filters.order_status and filters.order_status.lower() != "all":
        filter_status = (
            ["complete", "user_completed"]
            if filters.order_status.lower() == "complete"
            else [filters.order_status.lower()]
        )
        bookings_query = bookings_query.filter(models.Booking.status.in_(filter_status))

    if filters.order_number:
        bookings_query = bookings_query.filter(
            models.Booking.order_number.ilike(f"%{filters.order_number}%")
        )

    if filters.order_from_date or filters.order_to_date:
        order_filters = []
        if filters.order_from_date:
            order_filters.append(cast(models.Booking.order_date_time, Date) >= convert_date_with_number(filters.order_from_date))
        if filters.order_to_date:
            order_filters.append(cast(models.Booking.order_date_time, Date) <= convert_date_with_number(filters.order_to_date))
        elif filters.order_from_date:
            order_filters.append(cast(models.Booking.order_date_time, Date) <= datetime.now().date())
        bookings_query = bookings_query.filter(*order_filters)

    if filters.booking_from_date or filters.booking_to_date:
        booking_filters = []
        if filters.booking_from_date:
            booking_filters.append(cast(models.Booking.booking_date_time, Date) >= convert_date_with_number(filters.booking_from_date))
        if filters.booking_to_date:
            booking_filters.append(cast(models.Booking.booking_date_time, Date) <= convert_date_with_number(filters.booking_to_date))
        elif filters.booking_from_date:
            booking_filters.append(cast(models.Booking.booking_date_time, Date) <= datetime.now().date())
        bookings_query = bookings_query.filter(*booking_filters)

    if filters.search:
        search_term = f"%{filters.search}%"
        bookings_query = bookings_query.filter(
            or_(
                models.Booking.customer_name.ilike(search_term),
                models.Booking.customer_company_name.ilike(search_term),
                models.Booking.supplier_name.ilike(search_term),
                models.Booking.supplier_company_name.ilike(search_term),
                models.Booking.order_number.ilike(search_term),
                models.Booking.status.ilike(search_term.lower()),
            )
        )

    sort_columns = {
        "order_number": models.Booking.order_number,
        "status": models.Booking.status,
        "order_date": models.Booking.order_date_time,
        "booking_date": models.Booking.booking_date_time,
        "name": models.Booking.customer_name,
        "amount": models.Booking.total_price,
    }
    if filters.sort_by in sort_columns:
        sort_column = sort_columns[filters.sort_by]
        bookings_query = bookings_query.order_by(
            asc(sort_column) if filters.sort_order == 'asc' else desc(sort_column)
        )

    bookings = bookings_query.all()

    pdf_buffer = _build_bookings_pdf(bookings, booking_type, user, selected_language, tz=tz)

    filename_stamp = _now_in_tz(tz).strftime("%Y%m%d_%H%M%S")
    headers = {
        "Content-Disposition": f'attachment; filename="bookings_{booking_type}_{filename_stamp}.pdf"'
    }
    return StreamingResponse(pdf_buffer, media_type="application/pdf", headers=headers)


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PDF_LOGO_PATH = os.path.join(_BASE_DIR, "static_files", "email_logo.png")


def _to_tz(dt, tz):
    """Convert a naive UTC datetime to ``tz`` (naive local). Passthrough when
    ``tz`` is None or ``dt`` is falsy."""
    if dt is None or tz is None:
        return dt
    aware = dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)
    return aware.astimezone(tz).replace(tzinfo=None)


def _now_in_tz(tz):
    """`datetime.now()` in the caller's tz (naive local); UTC when tz is None."""
    if tz is None:
        return datetime.now()
    return datetime.now(dt_timezone.utc).astimezone(tz).replace(tzinfo=None)


def _build_bookings_pdf(bookings, booking_type: str, user, selected_language: str = 'en', tz=None) -> io.BytesIO:
    """Generate a PDF report of bookings and return a ready-to-stream buffer."""
    buffer = io.BytesIO()

    lang_messages = messages.get(selected_language) or messages['en']

    page_size = A4  # portrait
    page_width, _ = page_size
    left_margin = right_margin = 0.4 * inch
    usable_width = page_width - left_margin - right_margin

    def _footer(canvas, doc):
        """Drawn on every page — shows the generation timestamp (left) and page number (right)."""
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.grey)
        footer_text = f"{lang_messages['generated_on']} {_now_in_tz(tz).strftime('%d %b %Y %H:%M')}"
        canvas.drawString(left_margin, 0.25 * inch, footer_text)
        canvas.drawRightString(page_width - right_margin, 0.25 * inch, f"{lang_messages['page']} {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=0.4 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    subtitle_style = ParagraphStyle(
        "SubtitleStyle",
        parent=styles["Normal"],
        fontSize=11,
        textColor=colors.HexColor("#444444"),
        alignment=0,  # left
        spaceAfter=0,
    )
    elements = []

    # Header: centered logo only
    if os.path.exists(_PDF_LOGO_PATH):
        logo = Image(_PDF_LOGO_PATH, width=1.0 * inch, height=1.0 * inch, kind='proportional')
        logo.hAlign = "CENTER"
        elements.append(logo)

    elements.append(Spacer(1, 10))

    order_label = lang_messages['received_orders'] if booking_type == "received" else lang_messages['placed_orders']
    subtitle_para = Paragraph(
        f"{order_label} &middot; {lang_messages['total']}: {len(bookings)} {lang_messages['bookings']}",
        subtitle_style,
    )
    elements.append(subtitle_para)
    elements.append(Spacer(1, 12))

    # Cell styles — Paragraph wraps text within fixed-width columns
    cell_style = ParagraphStyle(
        "CellStyle",
        parent=styles["Normal"],
        fontSize=9,
        leading=11,
        wordWrap="CJK",  # ensures long unbroken strings still wrap
    )
    cell_style_right = ParagraphStyle(
        "CellStyleRight",
        parent=cell_style,
        alignment=2,  # right
    )
    header_cell_style = ParagraphStyle(
        "HeaderCellStyle",
        parent=styles["Normal"],
        fontSize=10,
        leading=12,
        textColor=colors.whitesmoke,
        fontName="Helvetica-Bold",
    )

    def _cell(text, right=False):
        text = "" if text is None else str(text)
        # Escape HTML-significant chars so user data doesn't break Paragraph parsing.
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(text, cell_style_right if right else cell_style)

    header_labels = [
        lang_messages['order_number'],
        lang_messages['order_status'],
        lang_messages['customer_name'],
        lang_messages['supplier_name'],
        lang_messages['order_date'],
        lang_messages['booking_date'],
        lang_messages['amount'],
    ]
    header_row = [Paragraph(label, header_cell_style) for label in header_labels]
    rows = [header_row]
    def _customer_display(b) -> str:
        name = b.customer_name or ""
        company = b.customer_company_name or ""
        if name and company:
            return f"{name} — {company}"
        return name or company or "-"

    def _supplier_display(b) -> str:
        return b.supplier_company_name or b.supplier_name or "-"

    note_cell_style = ParagraphStyle(
        "NoteCellStyle",
        parent=cell_style,
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#555555"),
        leftIndent=4,
    )
    booking_row_pairs = []

    for idx, b in enumerate(bookings):
        amount = (b.total_price or 0) + (b.additional_fee or 0) - (b.discount or 0)
        code = b.currency_code or ""
        amount_str = f"{code} {round(float(amount), 2):.2f}".strip()
        status_key = b.status or ""
        status_label = lang_messages.get(status_key) or status_key.replace("_", " ").title()
        order_dt = _to_tz(b.order_date_time, tz)
        booking_dt = _to_tz(b.booking_date_time, tz)
        main_row_idx = len(rows)
        rows.append([
            _cell(b.order_number or "-"),
            _cell(status_label or "-"),
            _cell(_customer_display(b)),
            _cell(_supplier_display(b)),
            _cell(order_dt.strftime("%d %b %Y") if order_dt else "-"),
            _cell(booking_dt.strftime("%d %b %Y %H:%M") if booking_dt else "-"),
            _cell(amount_str, right=True),
        ])
        note_row_idx = None
        note_text = (b.agreement_notes or "").strip()
        if note_text and (b.status or "").lower() != "pending":
            safe_text = note_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            label = lang_messages.get("agreement_notes_label", "Agreement Notes")
            note_para = Paragraph(f"<b>{label}:</b> {safe_text}", note_cell_style)
            note_row_idx = len(rows)
            rows.append([note_para, "", "", "", "", "", ""])
        booking_row_pairs.append((idx, main_row_idx, note_row_idx))

    col_proportions = [0.12, 0.12, 0.17, 0.17, 0.12, 0.16, 0.14]
    col_widths = [p * usable_width for p in col_proportions]
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ff0000")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
    ]
    zebra_colors = [colors.whitesmoke, colors.white]
    for idx, main_row_idx, note_row_idx in booking_row_pairs:
        bg = zebra_colors[idx % 2]
        style_cmds.append(("BACKGROUND", (0, main_row_idx), (-1, main_row_idx), bg))
        if note_row_idx is not None:
            style_cmds.append(("SPAN", (0, note_row_idx), (-1, note_row_idx)))
            style_cmds.append(("BACKGROUND", (0, note_row_idx), (-1, note_row_idx), bg))
            style_cmds.append(("TOPPADDING", (0, note_row_idx), (-1, note_row_idx), 2))
            style_cmds.append(("BOTTOMPADDING", (0, note_row_idx), (-1, note_row_idx), 6))
            style_cmds.append(("LINEABOVE", (0, note_row_idx), (-1, note_row_idx), 0, colors.white))
            last_row = note_row_idx
        else:
            last_row = main_row_idx
        style_cmds.append(("LINEBELOW", (0, last_row), (-1, last_row), 0.5, colors.grey))
    for col in range(len(col_proportions) - 1):
        style_cmds.append(("LINEAFTER", (col, 1), (col, -1), 0.25, colors.lightgrey))
    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    doc.build(elements, onFirstPage=_footer, onLaterPages=_footer)
    buffer.seek(0)
    return buffer


@router.get("/get-booking/{booking_id}", dependencies=[Depends(user_required)])
def get_single_supplier_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    booking = db.query(models.Booking).filter(
            models.Booking.id == booking_id
        ).options(
            joinedload(models.Booking.supplier)
            .joinedload(models.Supplier.user)
            .joinedload(models.User.currency),
            joinedload(models.Booking.supplier)
            .joinedload(models.Supplier.user)
            .joinedload(models.User.country),
        ).first()

    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)
    stripe_country_matched = False
    if booking.supplier.stripe_account_id:
        try:
            account = stripe.Account.retrieve(
                booking.supplier.stripe_account_id,
                api_key=stripe_api_key_for(booking.supplier),
            )
            # Refresh verification status if it drifted.
            if booking.supplier.stripe_verification_status in ['pending', 'unverified']:
                booking_supplier = db.query(models.Supplier).filter(models.Supplier.id == booking.supplier_id).first()
                new_status = map_stripe_requirements_to_status(account.get("requirements"))
                if booking_supplier.stripe_verification_status != new_status:
                    booking_supplier.stripe_verification_status = new_status
                    booking_supplier.updated_at = datetime.now()
                    db.commit()
                    db.refresh(booking_supplier)
            # Country match — resolve the platform the supplier belongs to
            stripe_account_country = (account.get("country") or "").upper()
            resolved_platform = stripe_platform_for_supplier(booking.supplier)
            if resolved_platform:
                expected_stripe_country = stripe_country_for_platform(resolved_platform)
                stripe_country_matched = (stripe_account_country == expected_stripe_country)
        except StripeCountryNotSupported:
            # Supplier isn't in a serviceable country (BR / CH / EU)
            logger.warning(
                "Skipping Stripe country check — supplier country not supported "
                "for booking=%s supplier=%s", booking.id, booking.supplier_id,
            )
            stripe_country_matched = False
        except stripe.error.InvalidRequestError as e:
            logger.error("Stripe error while fetching supplier account: %s", e)
            # Retrieve failed — safer to report mismatch so the mobile app
            # doesn't offer online payment on data we can't verify.
            stripe_country_matched = False
        except Exception as e:
            logger.error("Unexpected error fetching supplier Stripe account: %s", e)
            stripe_country_matched = False

    if booking.user_id==user.id:
        average_rating = db.query(func.avg(models.t_booking_reviews.c.rating_value)).filter(
            models.t_booking_reviews.c.receiver_id == booking.supplier.user_id, models.t_booking_reviews.c.reviewer_type == "1"
        ).scalar()
    else:
        average_rating = db.query(func.avg(models.t_booking_reviews.c.rating_value)).filter(
            models.t_booking_reviews.c.receiver_id == booking.user_id, models.t_booking_reviews.c.reviewer_type == "0"
        ).scalar()

    booking_review = db.query(models.t_booking_reviews).filter(
        models.t_booking_reviews.c.booking_id == booking.id,
        models.t_booking_reviews.c.sender_id == user.id
        ).first()
    
    is_customer_review_posted = db.query(models.t_booking_reviews).filter(
        models.t_booking_reviews.c.booking_id == booking.id,
        models.t_booking_reviews.c.sender_id == booking.user_id
        ).first()

    booking_items = db.query(models.BookingItem).filter(models.BookingItem.booking_id == booking.id).all()

    # Batch-fetch the snapshotted gallery for every booking item in one query.
    booking_item_ids = [bi.id for bi in booking_items]
    images_by_booking_item = {}
    if booking_item_ids:
        all_images = (
            db.query(models.BookingItemImage)
            .filter(models.BookingItemImage.booking_item_id.in_(booking_item_ids))
            .order_by(models.BookingItemImage.sort_order.asc(), models.BookingItemImage.id.asc())
            .all()
        )
        for img in all_images:
            images_by_booking_item.setdefault(img.booking_item_id, []).append(img)

    payment_intent = ""
    discounted_price = booking.discount if booking.discount else 0.0
    total_amount = round((booking.total_price + booking.additional_fee - booking.discount), 2)
    if booking.status=="payment_pending" and booking.user_id==user.id and booking.payment_mode=="online" and booking.supplier.stripe_account_id and booking.supplier.stripe_verification_status == 'verified' and stripe_country_matched:
        api_key = stripe_api_key_for(booking.supplier)
        existing = (
            db.query(models.BookingTransaction)
            .filter(models.BookingTransaction.booking_id == booking.id)
            .first()
        )
        # Reuse the existing PaymentIntent when it is still usable
        reusable_pi = None
        if existing and existing.payment_intent:
            try:
                stripe_pi = stripe.PaymentIntent.retrieve(existing.payment_intent, api_key=api_key)
                pi_status = stripe_pi.get('status')
                if pi_status in (
                    'requires_payment_method',
                    'requires_confirmation',
                    'requires_action',
                    'processing',
                    'requires_capture',
                    'succeeded',
                ):
                    reusable_pi = stripe_pi
            except Exception as e:
                logger.error("PaymentIntent retrieve error: %s", e)

        if reusable_pi is not None:
            payment_intent = reusable_pi.get('client_secret') or ""
        else:
            db.query(models.BookingTransaction).filter(models.BookingTransaction.booking_id == booking.id).delete()
            db.commit()
            try:
                estimated_fee = (Decimal(str(total_amount)) * _get_participation_fee_rate(db)).quantize(Decimal("0.01"))
                new_pi = stripe.PaymentIntent.create(
                    amount=int(total_amount * 100),
                    currency=booking.currency_code if booking.currency_code else "BRL",
                    payment_method_types=["card"],
                    api_key=api_key,
                    metadata={
                        "meiappli_order_number": booking.order_number or "",
                        "booking_id": str(booking.id),
                        "participation_fee": f"{estimated_fee:.2f}",
                    },
                )
                payment_intent = new_pi.client_secret
                extracted_intent = extract_payment_intent(payment_intent)
                booking_transaction = models.BookingTransaction(
                    booking_id = booking.id,
                    payment_intent = extracted_intent,
                    amount = total_amount,
                    trx_status = '2',
                    is_sct = True,
                    transfer_status = 'pending',
                )
                db.add(booking_transaction)
                db.commit()
            except Exception as e:
                logger.error("Payment intent error: %s", e)
    is_cancel_active = True
    transaction_date = None
    booking_transaction = db.query(models.BookingTransaction).filter(models.BookingTransaction.booking_id==booking_id).first()
    if booking_transaction:
        transaction_date = booking_transaction.created_at.strftime("%d/%m/%Y")
    if booking_transaction and booking.user_id == user.id:
        if booking.status in _TERMINAL_STATUSES:
            is_cancel_active = False
        elif booking_transaction.is_sct and booking_transaction.transfer_status in ('released', 'refunded'):
            is_cancel_active = False

    cancel_requires_dispute = False
    if is_cancel_active:
        cancel_requires_dispute, _ = _cancel_requires_dispute(db, booking)

    current_selected_plan_type = resolve_caller_plan_type(db, user)

    can_update_classification = False
    if booking.user_id == user.id:
        can_update_classification = bool(
            db.query(models.Supplier.id)
            .join(models.SupplierSubscription)
            .filter(
                models.Supplier.user_id == user.id,
                models.Supplier.status == "Approved",
                models.SupplierSubscription.status == "active",
            )
            .first()
        )

    booking_response = booking_model.GetBookingDetail(
        id=booking.id,
        order_number=booking.order_number,
        user_id=booking.user_id,
        supplier_id=booking.supplier_id,
        booking_date_time=booking.booking_date_time,
        is_delivery=True if booking.is_delivery == "1" else False,
        customer_name=booking.customer_name,
        customer_company_name=booking.customer_company_name,
        booking_address=booking.booking_address,
        booking_address_additional_direction=booking.booking_address_additional_direction,
        supplier_name=booking.supplier_name,
        supplier_company_name=booking.supplier_company_name,
        additional_fee=f"{booking.additional_fee if booking.additional_fee else 0.0:.2f}",
        discount=f"{booking.discount if booking.discount else 0.0:.2f}",
        total_price=f"{total_amount:.2f}",
        additional_fee_reason=booking.additional_fee_reason,
        booking_additional_information=booking.booking_additional_information,
        customer_email=booking.customer_email,
        order_date_time=booking.order_date_time,
        payment_mode=booking.payment_mode,
        order_deadline_days=booking.order_deadline_days,
        status=booking.status,
        cancelled_reason=booking.cancelled_reason,
        cancelled_by=booking.cancelled_by,
        rating=round(average_rating, 2) if average_rating else 0.0,
        latitude=booking.latitude,
        longitude=booking.longitude,
        city=booking.city,
        apartment_zone=booking.apartment_zone,
        bank_details_verified=booking.supplier.stripe_verification_status,
        supplier_email=booking.supplier.user.email,
        is_review_posted=True if booking_review else False,
        is_customer_review_posted=True if is_customer_review_posted else False,
        supplier_user_id=booking.supplier.user_id,
        supplier_country_name=booking.supplier.user.country.country_name if booking.supplier.user.country else None,
        supplier_country_code=booking.supplier.user.country.country_code if booking.supplier.user.country else None,
        stripe_country_matched=stripe_country_matched,
        payment_intent=payment_intent,
        discounted_price=f"{discounted_price:.2f}",
        currency_code = booking.currency_code,
        currency_symbol = booking.currency_symbol,
        is_bank_detail_added = True if booking.supplier.stripe_verification_status == "verified" else False,
        is_cancel_active=is_cancel_active,
        cancel_requires_dispute=cancel_requires_dispute,
        transaction_date=transaction_date,
        current_selected_plan_type=current_selected_plan_type,
        expense_classification=booking.expense_classification or "2",
        can_update_classification=can_update_classification,
        order_type=_derive_order_type(booking_items),
        is_sct=bool(booking_transaction.is_sct) if booking_transaction else False,
        transfer_status=booking_transaction.transfer_status if booking_transaction else "pending",
        participation_fee=(f"{booking_transaction.participation_fee:.2f}"
                           if booking_transaction and booking_transaction.participation_fee is not None else None),
        net_amount=(f"{booking_transaction.net_amount:.2f}"
                    if booking_transaction and booking_transaction.net_amount is not None else None),
        refunded_amount=(f"{booking_transaction.refunded_amount:.2f}"
                         if booking_transaction and booking_transaction.refunded_amount is not None else None),
        payment_protection_ends_at=(booking_transaction.review_window_ends_at
                                    if booking_transaction else None),
        released_at=(booking_transaction.released_at if booking_transaction else None),
        refunded_at=(booking_transaction.refunded_at if booking_transaction else None),
        agreement_notes=booking.agreement_notes,
        booking_items=[
            booking_model.GetBookingItems(
                id=item.id,
                supplier_item_id=item.supplier_item_id,
                item_name=item.item_name,
                item_price=f"{item.item_price:.2f}",
                item_unit=item.item_unit,
                item_type=item.item_type,
                service_started=bool(item.service_started),
                service_started_at=item.service_started_at,
                service_ended=bool(item.service_ended),
                service_ended_at=item.service_ended_at,
                quantity=item.quantity,
                hours=item.hours,
                minutes=item.minutes,
                currency_code=booking.currency_code,
                currency_symbol=booking.currency_symbol,
                item_image=item.item_image,
                item_images=[
                    booking_model.BookingItemImage(
                        id=img.id,
                        image_url=img.image_url,
                        sort_order=img.sort_order,
                    )
                    for img in images_by_booking_item.get(item.id, [])
                ],
            ) for item in booking_items
        ]
    )

    return BaseController.success(jsonable_encoder(booking_response), messages[selected_language]['user_booking'])


@router.patch("/update-expense-classification/{booking_id}", dependencies=[Depends(user_required)])
def update_booking_expense_classification(
    booking_id: int,
    payload: booking_model.UpdateExpenseClassification,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    booking = db.query(models.Booking).filter(models.Booking.id == booking_id).first()
    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)

    if booking.user_id != user.id:
        return BaseController.errorGeneral(messages[selected_language]['action_not_allowed'], status.HTTP_403_FORBIDDEN)

    supplier = (
        db.query(models.Supplier)
        .join(models.SupplierSubscription)
        .filter(models.SupplierSubscription.status == "active")
        .filter(models.Supplier.user_id == user.id)
        .first()
    )
    if not supplier:
        return BaseController.errorGeneral(messages[selected_language]['action_not_allowed'], status.HTTP_403_FORBIDDEN)

    booking.expense_classification = payload.expense_classification
    db.commit()

    return BaseController.success(
        {"id": booking.id, "expense_classification": booking.expense_classification},
        messages[selected_language]['expense_classification_updated'],
    )


@router.put("/update-booking/{booking_id}", dependencies=[Depends(user_required)])
def update_booking(
    item: booking_model.UpdateBooking,
    booking_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    booking = db.query(models.Booking).filter(
        models.Booking.id == booking_id
        ).options(
            joinedload(models.Booking.supplier).joinedload(models.Supplier.user).joinedload(models.User.language)
        ).options(
            joinedload(models.Booking.user1).joinedload(models.User.language)
        ).first()

    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)
    status = item.status
    order_deadline_days = None
    if booking.status == "pending" and item.status == "accepted":
        if item.payment_mode == "offline":
            setting_key = "offline_order_deadline"
            default_days = 30
            status = item.status
        else:
            setting_key = "online_order_deadline"
            default_days = 7
            status = "payment_pending"
        order_deadline_days = default_days
        setting_obj = db.query(models.Setting).filter(models.Setting.key_name == setting_key).first()
        if setting_obj:
            try:
                order_deadline_days = int(setting_obj.value)
            except Exception as e:
                logger.error("Deadline days error: %s", e)
                order_deadline_days = default_days
        booking.order_deadline_days = order_deadline_days
    elif item.status == "cancelled" and booking.payment_mode == "online":
        booking_transaction = db.query(models.BookingTransaction).filter(models.BookingTransaction.booking_id == booking.id).first()
        if booking_transaction:
            api_key = stripe_api_key_for(booking.supplier)
            if booking_transaction.is_sct:
                _refund_transaction_full(
                    db, booking_transaction,
                    api_key=api_key,
                    actor_id=user.id,
                    reason='customer_cancelled',
                )
            else:
                payment_intent_info = stripe.PaymentIntent.retrieve(booking_transaction.payment_intent, api_key=api_key)
                if payment_intent_info.status == "requires_capture":
                    stripe.PaymentIntent.cancel(booking_transaction.payment_intent, api_key=api_key)
                    booking_transaction.trx_status = "4"
                    db.commit()
    elif item.status == "supplier_completed" and booking.payment_mode == "online":
        booking_transaction = db.query(models.BookingTransaction).filter(models.BookingTransaction.booking_id == booking.id).first()
        if booking_transaction and booking_transaction.is_sct:
            _open_review_window(db, booking, booking_transaction, actor_id=user.id)
    elif item.status == "user_completed" and booking.payment_mode == "online":
        booking_transaction = db.query(models.BookingTransaction).filter(models.BookingTransaction.booking_id == booking.id).first()
        if booking_transaction:
            api_key = stripe_api_key_for(booking.supplier)
            if booking_transaction.is_sct:
                _execute_release(
                    db, booking, booking_transaction,
                    api_key=api_key,
                    actor_id=user.id,
                    event_name='customer_confirmed',
                )
            else:
                payment_intent_info = stripe.PaymentIntent.retrieve(booking_transaction.payment_intent, api_key=api_key)
                if payment_intent_info.status == "requires_capture":
                    stripe.PaymentIntent.capture(booking_transaction.payment_intent, api_key=api_key)
                    booking_transaction.trx_status = "1"
                    db.commit()
        _maybe_send_review_prompt_push(db, booking)
    if item.status == "accepted":
        if item.additional_fee is not None:
            booking.additional_fee = item.additional_fee
        if item.discount is not None:
            booking.discount = item.discount
        if item.additional_fee_reason is not None:
            booking.additional_fee_reason = item.additional_fee_reason
    booking.status = status
    booking.payment_mode = item.payment_mode
    db.commit()
    db.refresh(booking)
    if booking.user1.fcm_token and booking.user1.send_push_notification == "0":
        notification_obj = get_notification_data("order_updated", booking.user1.language_id, db)
        if notification_obj:
            notification_body = f'{notification_obj["body"].replace("XXX", booking.order_number)} : {messages[booking.user1.language.code][booking.status]}'
            notification_obj["order_id"] = str(booking.id)
            notification_obj["body"] = notification_body
            notification_obj["type"] = "placed_order"
            send_push_notification(
                token=booking.user1.fcm_token,
                title=notification_obj["title"],
                body=notification_body,
                platform=decrypt_data(booking.user1.device_type),
                data=notification_obj
            )
    if booking.supplier.user.fcm_token and booking.supplier.user.send_push_notification == "0":
        notification_obj = get_notification_data("order_updated", booking.supplier.user.language_id, db)
        if notification_obj:
            notification_body = f'{notification_obj["body"].replace("XXX", booking.order_number)} : {messages[booking.supplier.user.language.code][booking.status]}'
            notification_obj["order_id"] = str(booking.id)
            notification_obj["body"] = notification_body
            notification_obj["type"] = "received_order"
            send_push_notification(
                token=booking.supplier.user.fcm_token,
                title=notification_obj["title"],
                body=notification_body,
                platform=decrypt_data(booking.supplier.user.device_type),
                data=notification_obj
            )
    customer_template_content = get_email_template_content(db, booking.user1.language_id, 'order-updated')
    supplier_template_content = get_email_template_content(db, booking.supplier.user.language_id, 'order-updated')
    customer_payment_status = messages[booking.user1.language.code]["offline_booking"]
    supplier_payment_status = messages[booking.supplier.user.language.code]["offline_booking"]
    if booking.payment_mode == "online":
        customer_payment_status = messages[booking.user1.language.code]["online_booking"]
        supplier_payment_status = messages[booking.supplier.user.language.code]["online_booking"]
    if customer_template_content and booking.user1.send_mail_notification == "0":
        background_tasks.add_task(
            send_order_email,
            email=booking.user1.email,
            order_number=booking.order_number,
            subject=customer_template_content.subject,
            body=customer_template_content.body,
            first_name=decrypt_data(booking.user1.first_name),
            last_name=decrypt_data(booking.user1.last_name),
            status=messages[booking.user1.language.code][booking.status],
            booking_id=booking.order_number,
            customer_name=booking.customer_name,
            service_provider_name=booking.supplier_name,
            price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
            booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
            booking_time=(booking.order_date_time).strftime("%H:%M"),
            payment_mode=customer_payment_status,
            selected_language=booking.user1.language.code
        )
    if supplier_template_content and booking.supplier.user.send_mail_notification == "0":
        background_tasks.add_task(
            send_order_email,
            email=booking.supplier.user.email,
            order_number=booking.order_number,
            subject=supplier_template_content.subject,
            body=supplier_template_content.body,
            first_name=decrypt_data(booking.supplier.user.first_name),
            last_name=decrypt_data(booking.supplier.user.last_name),
            status=messages[booking.supplier.user.language.code][booking.status],
            booking_id=booking.order_number,
            customer_name=booking.customer_name,
            service_provider_name=booking.supplier_name,
            price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
            booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
            booking_time=(booking.order_date_time).strftime("%H:%M"),
            payment_mode=supplier_payment_status,
            selected_language=booking.supplier.user.language.code
        )

    return BaseController.success([], messages[selected_language]['booking_updated'])


@router.put("/bookings/{booking_id}/agreement-notes", dependencies=[Depends(user_required)])
def update_agreement_notes(
    payload: booking_model.UpdateAgreementNotes,
    booking_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Entrepreneur-only write endpoint for the pre-acceptance agreement note.

    Allowed only when the booking is still `pending`; refused with 409 for any
    other status. Fires a push notification to the customer whenever the stored
    value changes to a non-empty value (clearing the note is silent).
    """
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == booking_id)
        .options(
            joinedload(models.Booking.supplier).joinedload(models.Supplier.user).joinedload(models.User.language),
            joinedload(models.Booking.user1).joinedload(models.User.language),
        )
        .first()
    )
    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)

    if not booking.supplier or booking.supplier.user_id != user.id:
        return BaseController.errorGeneral(
            messages[selected_language]['action_not_allowed'], status.HTTP_403_FORBIDDEN,
        )

    if booking.status != 'pending':
        return BaseController.errorGeneral(
            messages[selected_language]['agreement_notes_locked'], status.HTTP_409_CONFLICT,
        )

    new_value = (payload.agreement_notes or "").strip() or None
    previous_value = booking.agreement_notes
    booking.agreement_notes = new_value
    db.commit()

    if new_value and new_value != previous_value:
        customer = booking.user1
        if customer and customer.fcm_token and customer.send_push_notification == "0":
            notif = get_notification_data('agreement_notes_updated', customer.language_id, db)
            if notif:
                body = (notif.get("body") or "").replace("[order_number]", booking.order_number or "")
                send_push_notification(
                    token=customer.fcm_token,
                    title=notif.get("title"),
                    body=body,
                    platform=decrypt_data(customer.device_type),
                    data=notif,
                )

    return BaseController.success(
        {"agreement_notes": booking.agreement_notes},
        messages[selected_language]['agreement_notes_updated'],
    )


@router.delete("/bookings/{booking_id}/agreement-notes", dependencies=[Depends(user_required)])
def delete_agreement_notes(
    booking_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Clear the agreement note on a still-pending order.

    Entrepreneur-only. Allowed only while the booking is `pending`; refused
    with 409 for any other status. No notification is sent on delete.
    """
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    booking = (
        db.query(models.Booking)
        .filter(models.Booking.id == booking_id)
        .options(joinedload(models.Booking.supplier))
        .first()
    )
    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)

    if not booking.supplier or booking.supplier.user_id != user.id:
        return BaseController.errorGeneral(
            messages[selected_language]['action_not_allowed'], status.HTTP_403_FORBIDDEN,
        )

    if booking.status != 'pending':
        return BaseController.errorGeneral(
            messages[selected_language]['agreement_notes_locked'], status.HTTP_409_CONFLICT,
        )

    booking.agreement_notes = None
    db.commit()
    return BaseController.success(
        {"agreement_notes": None},
        messages[selected_language]['agreement_notes_deleted'],
    )


# ──────────────────────────────────────────────
# Reviews
# ──────────────────────────────────────────────
@router.post("/create-reviews/{booking_id}", dependencies=[Depends(user_required)])
async def create_reviews(
    review: booking_model.CreateReview,
    booking_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    booking = db.query(models.Booking).filter(
        models.Booking.id == booking_id
    ).options(joinedload(models.Booking.supplier)).first()
    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)
    if booking.supplier.user_id == user.id:
        reviewer_type = "0"
    elif booking.user_id == user.id:
        reviewer_type = "1"
    else:
        return BaseController.errorGeneral(messages[selected_language]['action_not_allowed'], status.HTTP_404_NOT_FOUND)
    added_review = (
        db.query(models.t_booking_reviews)
        .filter(
            models.t_booking_reviews.c.booking_id == booking.id,
            models.t_booking_reviews.c.reviewer_type == reviewer_type
        )
        .first()
    )
    if added_review:
        return BaseController.errorGeneral(messages[selected_language]['review_already_added'], status.HTTP_400_BAD_REQUEST)
    new_review = models.t_booking_reviews.insert().values(
        sender_id=booking.user_id if reviewer_type == '1' else booking.supplier.user_id,
        receiver_id=booking.supplier.user_id if reviewer_type == '1' else booking.user_id,
        booking_id=booking_id,
        review_text=review.review_text,
        rating_value=review.rating_value,
        review_anonymous=review.review_anonymous,
        reviewer_type=reviewer_type
    )
    db.execute(new_review)
    db.commit()
    return BaseController.success([], messages[selected_language]['review_created'])


# ──────────────────────────────────────────────
# Disputes
# ──────────────────────────────────────────────
@router.get("/booking-dispute-types", dependencies=[Depends(user_required)])
def get_booking_dispute_type(
    type: str = Query(
        default='dispute',
        description="Filter by category: 'dispute' (default) or 'cancellation'",
    ),
    db: Session = Depends(get_db),
    selected_language: Optional[str] = Cookie(default='en'),
):
    language = db.query(models.Language).filter(models.Language.code == selected_language).first()

    if not language:
        return BaseController.errorGeneral("Selected language not supported", 400)

    language_id = language.id

    category = type if type in ('dispute', 'cancellation') else 'dispute'

    dispute_type_list = (
        db.query(models.DisputeType)
        .join(models.DisputeTypeTranslation)
        .filter(
            models.DisputeTypeTranslation.language_id == language_id,
            models.DisputeType.status == "1",
            models.DisputeType.category == category,
        )
        .all()
    )
    
    if dispute_type_list:
        dispute_type_response = [map_dispute_to_pydantic(dispute_type, language_id, db) for dispute_type in dispute_type_list]
        return BaseController.success(dispute_type_response, messages[selected_language]['dispute_type_listing'])
    else:
        return BaseController.success([], messages[selected_language]['dispute_type_listing'])


def map_dispute_to_pydantic(dispute_type: models.DisputeType, language_id: int, db: Session) -> booking_model.GetDisputeType:
    translation = (
        db.query(models.DisputeTypeTranslation)
        .filter(models.DisputeTypeTranslation.dispute_type_id == dispute_type.id, models.DisputeTypeTranslation.language_id == language_id)
        .first()
    )
    
    if not translation:
        raise ValueError(f"No translation found for dispute type {dispute_type.id} and language_id {language_id}")
    return booking_model.GetDisputeType(
        id=dispute_type.id,
        name=translation.dispute_title,
    )


@router.post("/raise-dispute/{booking_id}", dependencies=[Depends(user_required)])
async def raise_dispute(
    booking_id: int,
    dispute_type: booking_model.CreateDispute,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en')
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    language = db.query(models.Language).filter(models.Language.code == selected_language).first()
    if not language:
        return BaseController.errorGeneral("Selected language not supported", 400)
    translation = (
        db.query(models.DisputeTypeTranslation)
        .filter(models.DisputeTypeTranslation.dispute_type_id == dispute_type.dispute_type_id, models.DisputeTypeTranslation.language_id == language.id)
        .first()
    )
    booking = (db.query(models.Booking)
               .filter(models.Booking.id == booking_id)
               .options(joinedload(models.Booking.supplier).joinedload(models.Supplier.user))
               .options(joinedload(models.Booking.user)).first())
    if not booking:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)
    participants_id_list = [booking.user_id, booking.supplier.user_id]
    if not user.id in participants_id_list:
        return BaseController.errorGeneral(messages[selected_language]['action_not_allowed'], status.HTTP_404_NOT_FOUND)
    dispute = models.BookingDispute(
        booking_id=booking_id,
        dispute_type_id=dispute_type.dispute_type_id,
        created_by=user.id
    )
    db.add(dispute)
    conversation = models.Conversations(
        chat_type='dispute',
        status='active',
        booking_id=booking_id
    )
    db.add(conversation)
    db.flush()
    participants = [
        models.ConversationParticipants(conversation_id=conversation.id, participant_id=participant, status='active')
        for participant in participants_id_list
    ]
    db.bulk_save_objects(participants)
    booking.status="dispute"

    booking_transaction = (
        db.query(models.BookingTransaction)
        .filter(models.BookingTransaction.booking_id == booking.id)
        .first()
    )
    if booking_transaction and booking_transaction.is_sct:
        booking_transaction.transfer_status = 'blocked'
        db.add(models.BookingAuditLog(
            booking_id=booking.id,
            actor_id=user.id,
            event='transfer_blocked',
            payload={
                "reason": "dispute_raised",
                "dispute_type_id": dispute_type.dispute_type_id,
            },
        ))
    db.commit()
    if booking.user1.fcm_token and booking.user1.send_push_notification == "0":
        customer_notification_obj = get_notification_data("customer_dispute", booking.user1.language_id, db)
        if customer_notification_obj:
            send_push_notification(
                token=booking.user1.fcm_token,
                title=customer_notification_obj["title"],
                body=customer_notification_obj["body"],
                platform=decrypt_data(booking.user1.device_type),
                data=customer_notification_obj
            )
    if booking.supplier.user.fcm_token and booking.supplier.user.send_push_notification == "0":
        supplier_notification_obj = get_notification_data("supplier_dispute", booking.supplier.user.language_id, db)
        if supplier_notification_obj:
            send_push_notification(
                token=booking.supplier.user.fcm_token,
                title=supplier_notification_obj["title"],
                body=supplier_notification_obj["body"],
                platform=decrypt_data(booking.supplier.user.device_type),
                data=supplier_notification_obj
            )
    customer_template_content = get_email_template_content(db, booking.user1.language_id, 'order-updated')
    supplier_template_content = get_email_template_content(db, booking.supplier.user.language_id, 'order-updated')
    customer_payment_status = messages[booking.user1.language.code]["offline_booking"]
    supplier_payment_status = messages[booking.supplier.user.language.code]["offline_booking"]
    if booking.payment_mode == "online":
        customer_payment_status = messages[booking.user1.language.code]["online_booking"]
        supplier_payment_status = messages[booking.supplier.user.language.code]["online_booking"]
    if customer_template_content and booking.user1.send_mail_notification == "0":
        background_tasks.add_task(
            send_order_email,
            email=booking.user1.email,
            order_number=booking.order_number,
            subject=customer_template_content.subject,
            body=customer_template_content.body,
            first_name=decrypt_data(booking.user1.first_name),
            last_name=decrypt_data(booking.user1.last_name),
            status=messages[booking.user1.language.code][booking.status],
            booking_id=booking.order_number,
            customer_name=booking.customer_name,
            service_provider_name=booking.supplier_name,
            price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
            booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
            booking_time=(booking.order_date_time).strftime("%H:%M"),
            payment_mode=customer_payment_status,
            selected_language=booking.user1.language.code
        )
    if supplier_template_content and booking.supplier.user.send_mail_notification == "0":
        background_tasks.add_task(
            send_order_email,
            email=booking.supplier.user.email,
            order_number=booking.order_number,
            subject=supplier_template_content.subject,
            body=supplier_template_content.body,
            first_name=decrypt_data(booking.supplier.user.first_name),
            last_name=decrypt_data(booking.supplier.user.last_name),
            status=messages[booking.supplier.user.language.code][booking.status],
            booking_id=booking.order_number,
            customer_name=booking.customer_name,
            service_provider_name=booking.supplier_name,
            price=f"{booking.supplier.user.currency.symbol}{str(booking.total_price)}",
            booking_date=(booking.order_date_time).strftime("%d/%m/%Y"),
            booking_time=(booking.order_date_time).strftime("%H:%M"),
            payment_mode=supplier_payment_status,
            selected_language=booking.supplier.user.language.code
        )
    chat_history = []
    chat_response = (
        booking_model.BookingDisputeResponse(
            dispute_id=dispute.id,
            conversation_id=conversation.id,
            conversation_status=conversation.status,
            created_at=conversation.created_at,
            booking_id=conversation.booking_id,
            sender_id=user.id,
            order_number=booking.order_number,
            dispute_reason=translation.dispute_title,
            participants=participants_id_list,
            messages=chat_history
        )
    )
    return BaseController.success(jsonable_encoder(chat_response), messages[selected_language]['dispute_raised'])


@router.get("/get-disputed_bookings", dependencies=[Depends(user_required)])
def get_disputed_bookings(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
    page: int = Query(default=0, ge=0, description="Page number for pagination"),
    limit: int = Query(default=10, ge=1, le=100, description="Number of bookings to return per page")
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    
    language = db.query(models.Language).filter(models.Language.code == selected_language).first()
    if not language:
        return BaseController.errorGeneral("Selected language not supported", 400)
    
    bookings_query = (
        db.query(models.BookingDispute)
        .join(models.Booking, models.Booking.id == models.BookingDispute.booking_id)
        .join(models.Supplier, models.Supplier.id == models.Booking.supplier_id)
        .join(models.User, models.User.id == models.Supplier.user_id)
        .filter((models.Booking.user_id == user.id) | (models.Supplier.user_id == user.id))
        .distinct()
    )

    total_bookings = bookings_query.count()
    bookings = (
        bookings_query
        .order_by(desc(models.BookingDispute.created_at), desc(models.BookingDispute.id))
        .offset(page * limit)
        .limit(limit)
        .all()
    )

    dispute_type_ids = [booking.dispute_type_id for booking in bookings]
    translations = db.query(models.DisputeTypeTranslation).filter(
        models.DisputeTypeTranslation.dispute_type_id.in_(dispute_type_ids),
        models.DisputeTypeTranslation.language_id == language.id
    ).all()
    
    translation_map = {translation.dispute_type_id: translation.dispute_title for translation in translations}
    
    response_data = [
        booking_model.GetDisputedBookings(
            id=booking.id,
            booking_id=booking.booking_id,
            order_number=booking.booking.order_number,
            dispute_type_id=booking.dispute_type_id,
            dispute_reason=translation_map.get(booking.dispute_type_id, ""),
            dispute_status=booking.dispute_status,
            created_by=booking.created_by
        )
        for booking in bookings
    ]

    return PaginationResponse(
        total=total_bookings,
        page=page,
        per_page=limit,
        data=response_data,
        message=messages[selected_language]['user_bookings']
    )
