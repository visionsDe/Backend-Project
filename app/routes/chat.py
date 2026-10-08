# Standard library
import os
import uuid
from typing import Optional

# FastAPI
from fastapi import APIRouter, Cookie, Depends, File, Query, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer

# SQLAlchemy
from sqlalchemy import and_, distinct, func, literal, or_
from sqlalchemy.orm import Session, aliased, joinedload

# App
from app import models
from app.config import settings
from app.controller.base_controller import BaseController
from app.database import get_db
from app.helpers.messages import messages
from app.schemas import chat as chat_model
from app.utils.auth import user_or_admin_required, user_required
from app.utils.bookings import resolve_caller_plan_type
from app.utils.encryption import decrypt_data, hash_sort_string
from app.utils.helper import allowed_file
from app.utils.jwt_token import verify_access_token

router = APIRouter(
    prefix="",
    tags=["Booking Chats"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

CHATS_UPLOAD_DIR = "static/chats"
ALLOWED_CONTENT_TYPES = [
    "image/",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
]


# ──────────────────────────────────────────────
# Chat history (1-to-1)
# ──────────────────────────────────────────────
@router.get("/chats/{participant_id}", dependencies=[Depends(user_required)])
async def get_chat_history(
    participant_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    receiver_user = (
        db.query(models.User)
        .join(models.Role, models.Role.id == models.User.role_id)
        .filter(models.User.id == participant_id, models.Role.guard_name != 'admin')
        .first()
    )
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)
    if not receiver_user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    participants_id_list = [user.id, participant_id]
    participants_list = []
    sender_status = 'active'

    # Find an existing normal conversation between the two participants
    conversation = (
        db.query(models.Conversations)
        .join(models.ConversationParticipants, models.Conversations.id == models.ConversationParticipants.conversation_id)
        .filter(models.Conversations.chat_type == 'normal')
        .filter(models.Conversations.id.in_(
            db.query(models.ConversationParticipants.conversation_id)
            .filter(models.ConversationParticipants.participant_id.in_(participants_id_list))
            .group_by(models.ConversationParticipants.conversation_id)
            .having(
                func.count(distinct(models.ConversationParticipants.participant_id)) == len(participants_id_list)
            )
        ))
        .first()
    )

    if not conversation:
        # No prior conversation — create a new one with both participants
        conversation = models.Conversations(chat_type='normal', status='active')
        db.add(conversation)
        db.flush()
        participants = [
            models.ConversationParticipants(conversation_id=conversation.id, participant_id=pid, status='active')
            for pid in participants_id_list
        ]
        db.bulk_save_objects(participants)
        db.commit()
        for participant in participants:
            participants_list.append(participant.participant_id)
            if participant.participant_id == user.id:
                sender_status = participant.status
        chat_history = []
    else:
        chats = (
            db.query(models.Messages).join(models.User, models.User.id == models.Messages.sender_id)
            .filter(models.Messages.conversation_id == conversation.id)
            .order_by(models.Messages.created_at.asc())
            .options(joinedload(models.Messages.sender).joinedload(models.User.role))
            .all()
        )
        participants = db.query(models.ConversationParticipants).filter_by(conversation_id=conversation.id).all()
        for participant in participants:
            participants_list.append(participant.participant_id)
            if participant.participant_id == user.id:
                sender_status = participant.status
        chat_history = [
            chat_model.ChatMessageResponse(
                id=chat.id,
                conversation_id=chat.conversation_id,
                sender_id=chat.sender_id,
                body=chat.body,
                message_status=chat.status,
                attach_doc=chat.attach_doc,
                created_at=chat.created_at,
                sender_name="Admin" if chat.sender.role.guard_name == "admin" else f"{decrypt_data(chat.sender.first_name)} {decrypt_data(chat.sender.last_name)}",
                sender_company_name=None if chat.sender.role.guard_name == "admin" else decrypt_data(chat.sender.company_name),
            ) for chat in chats
        ]

    chat_response = chat_model.ChatResponse(
        conversation_id=conversation.id,
        conversation_status=conversation.status,
        created_at=conversation.created_at,
        user_name=f"{decrypt_data(receiver_user.first_name)} {decrypt_data(receiver_user.last_name)}",
        user_company_name=decrypt_data(receiver_user.company_name),
        user_profile_image=receiver_user.profile_img,
        booking_id=conversation.booking_id,
        sender_id=user.id,
        participants=participants_list,
        sender_status=sender_status,
        messages=chat_history,
    )
    return BaseController.success(jsonable_encoder(chat_response), messages[selected_language]['chat_retrieved'])


# ──────────────────────────────────────────────
# File upload
# ──────────────────────────────────────────────
@router.post("/chat/upload-file", dependencies=[Depends(user_or_admin_required)])
async def upload_file(
    chat_file: UploadFile = File(...),
    selected_language: Optional[str] = Cookie(default='en'),
):
    if not any(chat_file.content_type.startswith(ct) for ct in ALLOWED_CONTENT_TYPES):
        return BaseController.errorGeneral(messages[selected_language]['invalid_file_format'], 400)
    if not allowed_file(chat_file.filename):
        return BaseController.errorGeneral(messages[selected_language]['invalid_extension'], 400)

    if not os.path.exists(os.path.join(settings.FILE_DIR_PATH, CHATS_UPLOAD_DIR)):
        os.makedirs(os.path.join(settings.FILE_DIR_PATH, CHATS_UPLOAD_DIR))
    file_extension = chat_file.filename.split('.')[-1]
    new_filename = f"chat_file_{uuid.uuid4()}.{file_extension}"
    file_path = os.path.join(CHATS_UPLOAD_DIR, new_filename)
    with open(os.path.join(settings.FILE_DIR_PATH, file_path), "wb") as f:
        f.write(await chat_file.read())

    return BaseController.success({"path": file_path}, messages[selected_language]['chat_file_uploaded'])


# ──────────────────────────────────────────────
# Chat list
# ──────────────────────────────────────────────
def _build_search_filters(search_term: str):
    """Build the OR filter clause for searching users by encrypted name hashes."""
    term_hash = hash_sort_string(search_term)
    full_name_hash_strings = []
    keyword_list = search_term.split()
    for i in range(1, len(keyword_list)):
        first_name_str = " ".join(keyword_list[:i])
        last_name_str = " ".join(keyword_list[i:])
        full_name_hash_strings.append(f"{hash_sort_string(first_name_str)} {hash_sort_string(last_name_str)}")
    personal_name_match = or_(
        models.User.first_name_hash == term_hash,
        models.User.last_name_hash == term_hash,
        func.concat(models.User.first_name_hash, literal(" "), models.User.last_name_hash).in_(full_name_hash_strings),
    )
    return or_(
        models.User.company_name_hash == term_hash,
        and_(
            or_(models.User.company_name_hash.is_(None), models.User.company_name_hash == ""),
            personal_name_match,
        ),
    )


def _build_broadcast_list_items(
    db: Session,
    user: models.User,
    selected_language: str,
    search_term: Optional[str] = None,
) -> list:
    """Broadcast history entries for the caller's chat feed (supplier only)."""
    from app.routes.broadcasts import _localized_audience_label

    supplier = db.query(models.Supplier).filter_by(user_id=user.id).first()
    if not supplier:
        return []

    campaigns = (
        db.query(models.BroadcastCampaign)
        .options(
            joinedload(models.BroadcastCampaign.audience_config)
            .joinedload(models.BroadcastAudienceConfig.translations)
        )
        .filter(models.BroadcastCampaign.supplier_id == supplier.id)
        .order_by(models.BroadcastCampaign.sent_at.desc())
        .all()
    )
    if not campaigns:
        return []

    language_id = (
        db.query(models.Language.id)
        .filter(models.Language.code == selected_language)
        .scalar()
    )

    sender_name = f"{decrypt_data(user.first_name)} {decrypt_data(user.last_name)}"
    sender_company_name = decrypt_data(user.company_name)

    needle = search_term.strip().lower() if search_term else None
    items = []
    for row in campaigns:
        audience_label = (
            _localized_audience_label(row.audience_config, language_id)
            if row.audience_config else row.audience_key
        )
        if needle:
            haystack = f"{audience_label or ''} {row.message_text or ''}".lower()
            if needle not in haystack:
                continue
        items.append({
            "type": "broadcast",
            "conversation_id": row.id,
            "status": "active",
            "created_at": row.sent_at,
            "user_name": audience_label,
            "profile_img": row.audience_config.icon if row.audience_config else None,
            "user_id": None,
            "last_message": {
                "id": row.id,
                "conversation_id": row.id,
                "sender_id": user.id,
                "body": row.message_text,
                "attach_doc": row.image_url,
                "created_at": row.sent_at,
                "message_status": "active",
                "sender_name": sender_name,
                "sender_company_name": sender_company_name,
            },
            "total_recipients": row.total_recipients,
        })
    return jsonable_encoder(items)


@router.get("/chats", dependencies=[Depends(user_required)])
async def get_user_chats(
    search_term: Optional[str] = Query(None, description="Search for user name by keyword"),
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    from app.routes.broadcasts import (
        _TIER_BY_CODE,
        _active_subscription_for,
        _campaigns_this_period,
        _monthly_limit_for_tier,
    )

    supplier_row = (
        db.query(models.Supplier)
        .join(models.SupplierSubscription)
        .filter(models.SupplierSubscription.status == "active")
        .filter(models.Supplier.user_id == user.id)
        .first()
    )
    is_supplier_account = bool(supplier_row)
    can_broadcast = is_supplier_account
    tier_code = resolve_caller_plan_type(db, user) if is_supplier_account else None
    active_sub = _active_subscription_for(db, supplier_row) if is_supplier_account else None
    sent_this_month = _campaigns_this_period(db, supplier_row.id, active_sub) if supplier_row else 0
    monthly_limit = (
        _monthly_limit_for_tier(db, _TIER_BY_CODE.get(tier_code))
        if is_supplier_account and tier_code is not None
        else None
    )
    campaigns_remaining = None if monthly_limit is None else max(0, monthly_limit - sent_this_month)
    quota_fields = {
        "can_broadcast": can_broadcast,
        "current_subscription_plan": tier_code,
        "campaigns_sent_this_month": sent_this_month,
        "campaigns_remaining": campaigns_remaining,
        "monthly_limit": monthly_limit,
    }

    # Fetch all conversations for the current user, ordered by most recent message
    latest_message = aliased(models.Messages)
    conversations = (
        db.query(models.Conversations)
        .join(models.ConversationParticipants, models.Conversations.id == models.ConversationParticipants.conversation_id)
        .outerjoin(latest_message, latest_message.conversation_id == models.Conversations.id)
        .filter(models.ConversationParticipants.participant_id == user.id)
        .filter(models.Conversations.chat_type == 'normal')
        .group_by(models.Conversations.id)
        .order_by(func.max(latest_message.created_at).desc())
        .all()
    )
    if not conversations:
        return JSONResponse(
            content={
                "data": _build_broadcast_list_items(db, user, selected_language, search_term),
                **quota_fields,
                "status": True,
                "statusCode": 200,
                "message": messages[selected_language]['chat_retrieved'],
            },
            status_code=200,
        )

    conversation_ids = [c.id for c in conversations]

    # Batch-fetch the "other" participant for every conversation in a single query
    participant_query = (
        db.query(models.ConversationParticipants.conversation_id, models.User)
        .join(models.User, models.User.id == models.ConversationParticipants.participant_id)
        .join(models.Role, models.Role.id == models.User.role_id)
        .filter(models.ConversationParticipants.conversation_id.in_(conversation_ids))
        .filter(models.User.id != user.id)
        .filter(models.Role.guard_name != 'admin')
    )
    if search_term:
        participant_query = participant_query.filter(_build_search_filters(search_term))
    participants_by_conversation = {conv_id: participant for conv_id, participant in participant_query.all()}

    # Only keep conversations that still have a matching participant after filtering
    visible_conversation_ids = [cid for cid in conversation_ids if cid in participants_by_conversation]
    if not visible_conversation_ids:
        return JSONResponse(
            content={
                "data": _build_broadcast_list_items(db, user, selected_language, search_term),
                **quota_fields,
                "status": True,
                "statusCode": 200,
                "message": messages[selected_language]['chat_retrieved'],
            },
            status_code=200,
        )

    # Batch-fetch the last message per conversation in a single query using a correlated subquery
    last_message_subq = (
        db.query(
            models.Messages.conversation_id,
            func.max(models.Messages.created_at).label("max_created_at"),
        )
        .filter(models.Messages.conversation_id.in_(visible_conversation_ids))
        .group_by(models.Messages.conversation_id)
        .subquery()
    )
    last_messages = (
        db.query(models.Messages)
        .join(
            last_message_subq,
            (models.Messages.conversation_id == last_message_subq.c.conversation_id)
            & (models.Messages.created_at == last_message_subq.c.max_created_at),
        )
        .options(joinedload(models.Messages.sender).joinedload(models.User.role))
        .all()
    )
    last_messages_by_conversation = {msg.conversation_id: msg for msg in last_messages}

    # Build the response from pre-fetched data
    response_data = []
    for conversation in conversations:
        participant = participants_by_conversation.get(conversation.id)
        if not participant:
            continue

        last_message = last_messages_by_conversation.get(conversation.id)
        last_message_data = None
        if last_message:
            is_admin_sender = last_message.sender.role.guard_name == "admin"
            sender_name = (
                "Admin" if is_admin_sender
                else f"{decrypt_data(last_message.sender.first_name)} {decrypt_data(last_message.sender.last_name)}"
            )
            last_message_data = {
                "id": last_message.id,
                "conversation_id": last_message.conversation_id,
                "sender_id": last_message.sender_id,
                "body": last_message.body,
                "attach_doc": last_message.attach_doc,
                "created_at": last_message.created_at,
                "message_status": last_message.status,
                "sender_name": sender_name,
                "sender_company_name": None if is_admin_sender else decrypt_data(last_message.sender.company_name),
            }

        response_data.append(chat_model.ChatListResponse(
            conversation_id=conversation.id,
            status=conversation.status,
            created_at=conversation.created_at,
            user_name=f"{decrypt_data(participant.first_name)} {decrypt_data(participant.last_name)}",
            user_company_name=decrypt_data(participant.company_name),
            profile_img=participant.profile_img,
            user_id=participant.id,
            last_message=chat_model.ChatMessageResponse.model_validate(last_message_data) if last_message_data else None,
        ))

    chat_items = [{**item, "type": "chat"} for item in jsonable_encoder(response_data)]
    broadcast_items = _build_broadcast_list_items(db, user, selected_language, search_term)

    return JSONResponse(
        content={
            "data": broadcast_items + chat_items,
            **quota_fields,
            "status": True,
            "statusCode": 200,
            "message": messages[selected_language]['chat_retrieved'],
        },
        status_code=200,
    )


# ──────────────────────────────────────────────
# Dispute chat history
# ──────────────────────────────────────────────
@router.get("/chats/booking-dispute/{dispute_id}", dependencies=[Depends(user_required)])
async def get_chat_dispute_history(
    dispute_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    booking_dispute = (
        db.query(models.BookingDispute)
        .join(models.Booking, models.Booking.id == models.BookingDispute.booking_id)
        .filter(models.BookingDispute.id == dispute_id)
        .first()
    )
    if not booking_dispute:
        return BaseController.errorGeneral(messages[selected_language]['booking_not_found'], status.HTTP_404_NOT_FOUND)

    language = db.query(models.Language).filter(models.Language.code == selected_language).first()
    if not language:
        return BaseController.errorGeneral("Selected language not supported", 400)

    translation = (
        db.query(models.DisputeTypeTranslation)
        .filter(
            models.DisputeTypeTranslation.dispute_type_id == booking_dispute.dispute_type_id,
            models.DisputeTypeTranslation.language_id == language.id,
        )
        .first()
    )

    participants_list = []
    sender_status = 'active'
    conversation = (
        db.query(models.Conversations)
        .join(models.ConversationParticipants, models.Conversations.id == models.ConversationParticipants.conversation_id)
        .filter(models.Conversations.chat_type == 'dispute', models.Conversations.booking_id == booking_dispute.booking_id)
        .first()
    )

    if not conversation:
        return BaseController.errorGeneral(messages[selected_language]['conversation_not_started'], status.HTTP_404_NOT_FOUND)

    chats = (
        db.query(models.Messages).join(models.User, models.User.id == models.Messages.sender_id)
        .filter(models.Messages.conversation_id == conversation.id)
        .order_by(models.Messages.created_at.asc())
        .options(joinedload(models.Messages.sender).joinedload(models.User.role))
        .all()
    )
    participants = db.query(models.ConversationParticipants).filter_by(conversation_id=conversation.id).all()
    for participant in participants:
        participants_list.append(participant.participant_id)
        if participant.participant_id == user.id:
            sender_status = participant.status
    if user.id not in participants_list:
        return BaseController.errorGeneral(messages[selected_language]['action_not_allowed'], status.HTTP_404_NOT_FOUND)

    payment_transaction_status = None
    payment_transaction = db.query(models.BookingTransaction).filter(models.BookingTransaction.booking_id == booking_dispute.booking_id).first()
    if payment_transaction:
        payment_transaction_status = payment_transaction.trx_status

    chat_history = [
        chat_model.ChatMessageResponse(
            id=chat.id,
            conversation_id=chat.conversation_id,
            sender_id=chat.sender_id,
            body=chat.body,
            message_status=chat.status,
            attach_doc=chat.attach_doc,
            sender_name="Admin" if chat.sender.role.guard_name == "admin" else f"{decrypt_data(chat.sender.first_name)} {decrypt_data(chat.sender.last_name)}",
            sender_company_name=None if chat.sender.role.guard_name == "admin" else decrypt_data(chat.sender.company_name),
            created_at=chat.created_at,
        ) for chat in chats
    ]
    chat_response = chat_model.DisputeChatResponse(
        conversation_id=conversation.id,
        conversation_status=conversation.status,
        created_at=conversation.created_at,
        booking_id=conversation.booking_id,
        sender_id=user.id,
        order_number=booking_dispute.booking.order_number,
        reason=translation.dispute_title if translation else None,
        participants=participants_list,
        sender_status=sender_status,
        dispute_status=booking_dispute.dispute_status,
        reaction_reason=booking_dispute.reaction_reason,
        payment_transaction_status=payment_transaction_status,
        messages=chat_history,
    )
    return BaseController.success(jsonable_encoder(chat_response), messages[selected_language]['chat_retrieved'])


# ──────────────────────────────────────────────
# Send message
# ──────────────────────────────────────────────
@router.post("/send-message/{conversation_id}", dependencies=[Depends(user_required)])
def add_chat_message(
    conversation_id: int,
    message: chat_model.AddChatMessage,
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
    selected_language: Optional[str] = Cookie(default='en'),
):
    token_data = verify_access_token(token)
    user = db.query(models.User).filter(models.User.email == token_data.email).first()
    if not user:
        return BaseController.errorGeneral(messages[selected_language]['user_not_found'], status.HTTP_404_NOT_FOUND)

    participants = db.query(models.ConversationParticipants).filter_by(
        conversation_id=conversation_id, participant_id=user.id
    ).all()
    if not participants:
        return BaseController.errorGeneral(messages[selected_language]['action_not_allowed'], status.HTTP_404_NOT_FOUND)

    new_item = models.Messages(
        conversation_id=conversation_id,
        sender_id=user.id,
        body=message.body,
        attach_doc=message.attach_doc,
        read=False,
        status='active',
    )
    db.add(new_item)
    db.commit()

    return BaseController.success([], messages[selected_language]['message_created'])
