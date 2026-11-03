import uuid
from typing import Annotated

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status

from auth.constants import AUTH_COOKIE_NAME, HTTP_DETAIL_INVALID_TOKEN, HTTP_DETAIL_NOT_AUTHENTICATED
from auth.jwt import decode_access_token
from config import Settings, get_settings


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    stripped = parts[1].strip()
    return stripped if stripped else None


def get_current_user_claims(
    settings: Settings = Depends(get_settings),
    session_token: str | None = Cookie(None, alias=AUTH_COOKIE_NAME),
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    token = session_token or _bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=HTTP_DETAIL_NOT_AUTHENTICATED)

    if not settings.jwt_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="JWT_SECRET 尚未配置")

    try:
        return decode_access_token(token, settings.jwt_secret)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=HTTP_DETAIL_INVALID_TOKEN) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=HTTP_DETAIL_INVALID_TOKEN) from exc


def get_current_user_id(claims: dict = Depends(get_current_user_claims)) -> uuid.UUID:
    return uuid.UUID(claims["sub"])
