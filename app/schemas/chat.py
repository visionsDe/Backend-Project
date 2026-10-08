from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

class ChatMessage(BaseModel):
    sender_id: int
    body: Optional[str]
    attach_doc: Optional[str] = None
    participants_list: List[int] = []
    conversation_id: Optional[int] = None
    sender_name: str


class ChatMessageResponse(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    body: Optional[str]
    message_status: Optional[str]
    attach_doc: Optional[str] = None
    sender_name: Optional[str] = None
    sender_company_name: Optional[str] = None
    created_at: datetime

class ChatParticipants(BaseModel):
    id: int
    status: str

class ChatResponse(BaseModel):
    conversation_id: Optional[int] = None
    conversation_status: Optional[str] = 'not_started'
    created_at: Optional[datetime] = None
    user_name: Optional[str] = None
    user_company_name: Optional[str] = None
    user_profile_image: Optional[str] = None
    booking_id: Optional[int] = None
    sender_id: Optional[int] = None
    sender_status: Optional[str]
    participants: Optional[List] = []
    messages: Optional[List[ChatMessageResponse]] = []

class ChatListResponse(BaseModel):
    conversation_id: int
    status: str
    created_at: datetime
    user_name: str
    user_company_name: Optional[str] = None
    profile_img: Optional[str] = None
    user_id: int
    last_message: Optional[ChatMessageResponse] = None

class DisputeChatResponse(BaseModel):
    conversation_id: Optional[int] = None
    conversation_status: Optional[str] = 'not_started'
    created_at: Optional[datetime] = None
    booking_id: Optional[int] = None
    sender_id: Optional[int] = None
    sender_status: Optional[str]
    order_number: str
    reason: str
    dispute_status: str
    reaction_reason: Optional[str] =None
    payment_transaction_status: Optional[str] = None
    participants: Optional[List] = []
    messages: Optional[List[ChatMessageResponse]] = []

class AddChatMessage(BaseModel):
    body: Optional[str]
    attach_doc: Optional[str] = None

class ChatMessageAdminResponse(BaseModel):
    id: int
    conversation_id: int
    sender_id: int
    sender_first_name: Optional[str] = None
    sender_last_name: Optional[str] = None
    sender_company_name: Optional[str] = None
    sender_profile_img: Optional[str] = None
    body: Optional[str]
    message_status: Optional[str]
    attach_doc: Optional[str] = None
    sender_name: Optional[str] = None
    created_at: datetime

class ChatAdminResponse(BaseModel):
    conversation_id: Optional[int] = None
    conversation_status: Optional[str] = 'not_started'
    created_at: Optional[datetime] = None
    user_name: Optional[str] = None
    user_company_name: Optional[str] = None
    user_profile_image: Optional[str] = None
    booking_id: Optional[int] = None
    sender_id: Optional[int] = None
    sender_status: Optional[str]
    participants: Optional[List] = []
    messages: Optional[List[ChatMessageAdminResponse]] = []    