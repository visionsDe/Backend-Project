from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator
import re


def validate_password_strength(password: str) -> str:
    # Raise translation KEYS, not English strings. The validation exception
    # handler in app.main resolves the key against the selected_language cookie
    # before returning the response. An unknown key falls back to the raw
    # string, so this stays compatible with any other ValueError raised by
    # Pydantic validators in the project.
    if len(password) < 8:
        raise ValueError('password_too_short')
    if not re.search(r'[A-Z]', password):
        raise ValueError('password_missing_uppercase')
    if not re.search(r'[a-z]', password):
        raise ValueError('password_missing_lowercase')
    if not re.search(r'[0-9]', password):
        raise ValueError('password_missing_digit')
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        raise ValueError('password_missing_special_character')
    return password


class OAuth2CustomRequestFormRequest(BaseModel):
    username: EmailStr
    password: str
    time_zone: str
    fcm_token: str
    device_type: str
    language_id: int
    ip_address: str


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    time_zone: str
    fcm_token: str
    device_type: str
    language_id: int
    ip_address: str
    referred_by: Optional[str] = None

    @field_validator('password')
    @classmethod
    def password_strength(cls, v):
        return validate_password_strength(v)

class TokenData(BaseModel):
    email: EmailStr
    status: str
    sid: Optional[str] = None

class PasswordResetRequest(BaseModel):
    email: EmailStr
    language_id: int

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator('new_password')
    @classmethod
    def password_strength(cls, v):
        return validate_password_strength(v)