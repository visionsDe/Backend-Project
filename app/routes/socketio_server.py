# Standard library
from typing import Dict, List

# Third-party
import socketio

# SQLAlchemy
from sqlalchemy.orm import Session

# App
from app import models
from app.database import get_db
from app.schemas import chat as chat_model
from app.utils.encryption import decrypt_data

# Socket.IO server
sio = socketio.AsyncServer(cors_allowed_origins="*", async_mode='asgi')
app = socketio.ASGIApp(sio)


# ──────────────────────────────────────────────
# Connection manager
# ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, str] = {}

    async def connect(self, user_id: int, sid: str):
        self.active_connections[user_id] = sid

    def disconnect(self, sid: str):
        for user_id, session_id in self.active_connections.items():
            if session_id == sid:
                del self.active_connections[user_id]
                break

    async def send_message(self, message: dict, sid: str):
        await sio.emit('new_message', message, room=sid)

    async def send_message_to_user(self, message: dict, user_id: int):
        if user_id in self.active_connections:
            sid = self.active_connections[user_id]
            await self.send_message(message, sid)

    async def broadcast(self, message: dict):
        await sio.emit('chat_message', message)


manager = ConnectionManager()


# ──────────────────────────────────────────────
# Socket.IO events
# ──────────────────────────────────────────────
@sio.event
async def connect(sid, environ):
    pass


@sio.event
async def disconnect(sid):
    manager.disconnect(sid)


@sio.on('join')
async def join(sid, data):
    user_id = data['user_id']
    await manager.connect(int(user_id), sid)


@sio.on('send_message')
async def handle_message(sid, data):
    db: Session = next(get_db())
    try:
        message_data = chat_model.ChatMessage(**data)

        await save_message(db, message_data.conversation_id, message_data)
        await notify_participants(db, message_data, message_data.conversation_id)
    finally:
        db.close()


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
async def save_message(db: Session, conversation_id: int, message_data: chat_model.ChatMessage):
    new_message = models.Messages(
        conversation_id=conversation_id,
        sender_id=message_data.sender_id,
        body=message_data.body,
        attach_doc=message_data.attach_doc,
        read=False,
        status='active',
    )
    db.add(new_message)
    db.commit()


async def _get_conversation_participants(db: Session, conversation_id: int) -> List[int]:
    participants = (
        db.query(models.ConversationParticipants)
        .filter(
            models.ConversationParticipants.conversation_id == conversation_id,
            models.ConversationParticipants.status == 'active',
        )
        .all()
    )
    return [participant.participant_id for participant in participants]


async def notify_participants(
    db: Session,
    message_data: chat_model.ChatMessage,
    conversation_id: int,
):
    participants = await _get_conversation_participants(db, conversation_id)

    base_payload = {
        "sender_id": message_data.sender_id,
        "body": message_data.body,
        "attach_doc": message_data.attach_doc,
        "conversation_id": conversation_id,
        "participants_list": participants,
        "sender_name": message_data.sender_name,
    }

    admin_ids: set = set()
    if participants:
        admin_role_rows = (
            db.query(models.User.id)
            .join(models.Role, models.Role.id == models.User.role_id)
            .filter(models.User.id.in_(participants), models.Role.guard_name == 'admin')
            .all()
        )
        admin_ids = {row[0] for row in admin_role_rows}

    sender_is_admin = message_data.sender_id in admin_ids
    admin_display_name = message_data.sender_name
    if admin_ids and not sender_is_admin:
        sender = (
            db.query(models.User)
            .filter(models.User.id == message_data.sender_id)
            .first()
        )
        if sender:
            first = decrypt_data(sender.first_name) if sender.first_name else ""
            last = decrypt_data(sender.last_name) if sender.last_name else ""
            personal_name = f"{first} {last}".strip()
            company_name = decrypt_data(sender.company_name) if sender.company_name else ""
            if personal_name and company_name:
                admin_display_name = f"{personal_name} — {company_name}"
            elif personal_name:
                admin_display_name = personal_name

    for participant in participants:
        payload = dict(base_payload)
        if sender_is_admin:
            payload["sender_name"] = "Admin"
        elif participant in admin_ids:
            payload["sender_name"] = admin_display_name
        await manager.send_message_to_user(payload, participant)
