import time
import urllib.parse
import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from auth.jwt import create_access_token
from auth.oauth_redirect import oauth_success_redirect
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.gmail import GmailAuthUrlResponse, GmailStatusResponse
from services import gmail_integration_service, user_service

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

GMAIL_SCOPES = " ".join([
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
])

gmail_router = APIRouter(prefix="/gmail", tags=["gmail"])


def _require_google_credentials(settings: Settings) -> tuple[str, str]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth 凭据尚未配置",
        )
    return settings.google_client_id, settings.google_client_secret


@gmail_router.get("/auth-url", response_model=GmailAuthUrlResponse)
async def get_gmail_auth_url(settings: Settings = Depends(get_settings)) -> GmailAuthUrlResponse:
    client_id, _ = _require_google_credentials(settings)

    params = {
        "client_id": client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return GmailAuthUrlResponse(auth_url=auth_url)


@gmail_router.get("/callback", dependencies=[Depends(verify_database_ready)])
async def gmail_oauth_callback(
    code: Annotated[str, Query()],
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    client_id, client_secret = _require_google_credentials(settings)

    if not settings.jwt_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="JWT_SECRET 尚未配置")

    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_response.status_code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="与 Google 交换授权码失败")

    token_data: dict[str, Any] = token_response.json()
    access_token: str = token_data["access_token"]
    refresh_token: str | None = token_data.get("refresh_token")
    expires_in: int | None = token_data.get("expires_in")
    token_expiry = int(time.time()) + expires_in if expires_in else None

    async with httpx.AsyncClient() as client:
        userinfo_response = await client.get(
            GOOGLE_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_response.status_code != 200:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="无法从 Google 获取用户信息")

    email: str = userinfo_response.json()["email"]

    user = await user_service.get_or_create_user_by_email(session, email)

    await gmail_integration_service.upsert_gmail_integration(
        session,
        user_id=user.id,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=token_expiry,
        provider_account_id=email,
    )

    jwt_token = create_access_token(user.id, email, settings.jwt_secret)

    return oauth_success_redirect(settings.frontend_url, jwt_token)


@gmail_router.get(
    "/status",
    response_model=GmailStatusResponse,
    dependencies=[Depends(verify_database_ready)],
)
async def get_gmail_status(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> GmailStatusResponse:
    integration = await gmail_integration_service.get_gmail_integration(session, user_id)
    if integration is None:
        return GmailStatusResponse(is_connected=False)

    return GmailStatusResponse(
        is_connected=integration.is_connected,
        provider_account_id=integration.provider_account_id,
    )


@gmail_router.post(
    "/disconnect",
    dependencies=[Depends(verify_database_ready)],
)
async def post_disconnect_gmail(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """Clear stored Gmail OAuth tokens and mark the integration disconnected."""
    from services.agent_runner import invalidate_token_cache
    cleared = await gmail_integration_service.disconnect_gmail_integration(session, user_id)
    await invalidate_token_cache(user_id, "gmail")
    return {"disconnected": cleared}
