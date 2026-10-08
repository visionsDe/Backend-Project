from datetime import datetime, timedelta
from jose import JWTError, jwt, ExpiredSignatureError
from app.config import settings
from app.schemas.auth import TokenData
from typing import Optional
from fastapi import HTTPException, status

SECRET_KEY = settings.SECRET_KEY
ALGORITHM = settings.ALGORITHM
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
VERIFICATION_TOKEN_EXPIRE_MINUTES = settings.VERIFICATION_TOKEN_EXPIRE_MINUTES
RESET_PASS_TOKEN_EXPIRE_MINUTES = settings.RESET_PASS_TOKEN_EXPIRE_MINUTES
REMEMBER_ME_EXPIRE_DAYS = settings.REMEMBER_ME_EXPIRE_DAYS


def create_access_token(data: dict, remember_me: bool = False):
    to_encode = data.copy()
    if remember_me:
        expire = datetime.now() + timedelta(days=REMEMBER_ME_EXPIRE_DAYS)
    else:
        expire = datetime.now() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("email")
        status: str = payload.get("status")
        sid: str = payload.get("sid")
        token_data = TokenData(email=email, status=status, sid=sid)
        return token_data
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has been expired")

def create_verification_token(user_id: str):
    expire = datetime.now() + timedelta(minutes=VERIFICATION_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": str(user_id), "exp": expire}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def email_token_verification(token:str):
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    user_id: str = payload.get("sub")
    return user_id

def create_reset_token(email: str, expires_delta: Optional[timedelta] = None):
    to_encode = {"sub": email}
    if expires_delta:
        expire = datetime.now() + expires_delta
    else:
        expire = datetime.now() + timedelta(minutes=RESET_PASS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# Function to verify password reset token
def verify_reset_token(token: str):
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    email: str = payload.get("sub")
    return email