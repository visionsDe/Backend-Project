from sqlalchemy import Column, ForeignKey, Index, TIMESTAMP, text, Boolean
from sqlalchemy.dialects.mysql import BIGINT, ENUM, TEXT, VARCHAR
from sqlalchemy.orm import relationship
from .base import Base

class Conversations(Base):
    __tablename__ = 'conversations'
    __table_args__ = (
        Index('ix_conversations_status', 'status'),
        Index('ix_conversations_created_at', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the conversation')
    booking_id = Column(ForeignKey('bookings.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=True, index=True, comment='Reference to the booking')
    chat_type = Column(ENUM('normal', 'dispute'), nullable=False, server_default=text("'normal'"))
    status = Column(ENUM('active', 'inactive', 'blocked'), nullable=False, server_default=text("'active'"), comment="Status of conversation")
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the chat was added.')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the chat was updated.')

    booking  = relationship('Booking')

class ConversationParticipants(Base):
    __tablename__ = 'conversation_participants'

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the conversation participants')
    conversation_id = Column(ForeignKey('conversations.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True, comment='Reference to the conversation')
    participant_id = Column(ForeignKey('users.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True, comment='Reference to the participants')
    status = Column(ENUM('active', 'inactive'), nullable=False, server_default=text("'active'"), comment="Status of participants")
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the participants were added.')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the participants were updated.')

    conversation = relationship('Conversations')
    participant = relationship('User')



class Messages(Base):
    __tablename__ = 'messages'
    __table_args__ = (
        Index('ix_messages_status', 'status'),
        Index('ix_messages_read', 'read'),
        Index('ix_messages_conversation_created', 'conversation_id', 'created_at'),
    )

    id = Column(BIGINT, primary_key=True, comment='Unique identifier for the chat')
    conversation_id = Column(ForeignKey('conversations.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True, comment='Reference to the conversation')
    sender_id = Column(ForeignKey('users.id', ondelete='CASCADE', onupdate='RESTRICT'), nullable=False, index=True, comment='Reference to the sender')
    body = Column(TEXT, comment='Content of the text message')
    attach_doc = Column(VARCHAR(255), comment='share file')
    read = Column(Boolean, server_default=text("false"))
    status = Column(ENUM('active', 'inactive'), nullable=False, server_default=text("'active'"), comment="Status of message")
    created_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the chat was added.')
    updated_at = Column(TIMESTAMP, nullable=False, server_default=text("CURRENT_TIMESTAMP"), comment='Timestamp indicating when the chat was updated.')

    conversation = relationship('Conversations')
    sender = relationship('User')
