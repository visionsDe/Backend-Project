import uuid
from fastapi import Depends, HTTPException, status, Cookie
from jose import JWTError
from sqlalchemy.orm import Session
from app.models import User, Role
from app.database import get_db
from app.utils.jwt_token import verify_access_token
from fastapi.security import OAuth2PasswordBearer
from typing import Optional
from app.helpers.messages import messages

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db), selected_language: Optional[str] = Cookie(default='en')):
    try:
        token_data = verify_access_token(token)
        if not token_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=messages[selected_language]['invalid_token'],
            )
        user = db.query(User).filter(User.email == token_data.email).join(Role).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=messages[selected_language]['user_not_found'],
            )
        if user.status in ('Deleted', 'Rejected'):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=messages[selected_language]['account_not_active'],
            )
        # Single-device session enforcement: the JWT's sid must match the user's current
        # session_token_id. A login on a new device rotates session_token_id, which kills
        # any previously-issued tokens. Users with NULL session_token_id (legacy rows
        # that haven't logged in since the feature shipped) keep working until their next
        # login — at that point they'll be on the new scheme.
        # Admin role is exempt: admins are allowed concurrent sessions across devices
        # (multiple ops team members may share an account). The rotation still happens
        # on login; we just don't enforce the mismatch when role.guard_name == 'admin'.
        if (
            user.role.guard_name != "admin"
            and user.session_token_id
            and token_data.sid != user.session_token_id
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=messages[selected_language]['invalid_token'],
            )
        return user
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=messages[selected_language]['invalid_token'],
        )

def admin_required(user = Depends(get_current_user), selected_language: Optional[str] = Cookie(default='en')):
    if user.role.guard_name != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=messages[selected_language]['action_not_allowed']
        )
    return user

def user_required(user = Depends(get_current_user), selected_language: Optional[str] = Cookie(default='en')):
    if user.role.guard_name != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=messages[selected_language]['action_not_allowed']
        )
    return user


def user_or_admin_required(
    user = Depends(get_current_user),
    selected_language: Optional[str] = Cookie(default='en'),
):
    """Allow either a regular ``user`` or an ``admin`` role."""
    if user.role.guard_name not in ("user", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=messages[selected_language]['action_not_allowed'],
        )
    return user

def generate_unique_referral_code(db: Session, length: int = 8) -> str:
    while True:
        referral_code = uuid.uuid4().hex[:length].upper()
        existing_user = db.query(User).filter(User.referral_code == referral_code).first()
        if not existing_user:
            return referral_code