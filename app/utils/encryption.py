from passlib.context import CryptContext
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings
import hashlib

SECRET_KEY = settings.ENC_SECURE_KEY  # Replace with your generated key
cipher_suite = Fernet(SECRET_KEY)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password: str, password: str):
    return pwd_context.verify(plain_password, password)

def encrypt_data(data: str) -> str:
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(data: str) -> str:
    if isinstance(data, str):
        try:
            decrypted_string = cipher_suite.decrypt(data.encode()).decode()
        except InvalidToken:
            decrypted_string = data
        return decrypted_string
    else:
        return data

def hash_string(string: str) -> str:
    if not string or not string.strip():
        return ""
    string_without_spaces = string.lower().strip()
    return hashlib.sha256(string_without_spaces.encode()).hexdigest()

def hash_sort_string(string: str) -> str:
    if not string or not string.strip():
        return ""
    string_without_spaces = string.lower().strip()
    first_char = string_without_spaces[0]
    remaining_string = string_without_spaces[1:]
    hashed_remaining = hashlib.sha256(remaining_string.encode()).hexdigest()
    return f"{first_char}{hashed_remaining}"