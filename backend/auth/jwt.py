import time
import uuid

import jwt

from auth.constants import JWT_ALGORITHM, JWT_TOKEN_EXPIRY_SECONDS


def create_access_token(user_id: uuid.UUID, email: str, secret: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict:
    return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
