"""163 邮箱授权码登录与状态路由。"""

import html
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from auth.jwt import create_access_token
from auth.oauth_redirect import oauth_success_redirect
from config import Settings, get_settings, has_mail163_credentials
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.mail163 import Mail163StatusResponse
from services import user_service
from services.mail163_client import Mail163Config, Mail163Error, verify_login

mail163_router = APIRouter(prefix="/mail163", tags=["mail163"])


def _login_error_page(title: str, message: str, return_url: str | None = None) -> HTMLResponse:
    target = html.escape(return_url or "/")
    body = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>{html.escape(title)}</title>
      <style>
        body {{ margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
                background: #0f172a; color: #e2e8f0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }}
        main {{ max-width: 42rem; margin: 2rem; padding: 2rem; background: #1e293b; border: 1px solid #334155; border-radius: 12px; }}
        h1 {{ margin: 0 0 .75rem; font-size: 1.35rem; color: #f8fafc; }}
        p {{ margin: 0 0 1.25rem; line-height: 1.7; color: #cbd5e1; }}
        a {{ color: #93c5fd; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
      </style>
    </head>
    <body>
      <main>
        <h1>{html.escape(title)}</h1>
        <p>{html.escape(message)}</p>
        <p><a href="{target}">返回登录页</a></p>
      </main>
    </body>
    </html>
    """
    return HTMLResponse(content=body, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


def _require_mail163_credentials(settings: Settings) -> Mail163Config:
    if not has_mail163_credentials(settings):
        raise Mail163Error("请在 backend/.env 中配置 MAIL163_EMAIL 和 MAIL163_EMAIL_AUTH_CODE", 503)
    return Mail163Config(
        email=settings.mail163_email or "",
        auth_code=settings.mail163_email_auth_code or "",
        smtp_host=settings.mail163_smtp_host,
        smtp_port=settings.mail163_smtp_port,
        imap_host=settings.mail163_imap_host,
        imap_port=settings.mail163_imap_port,
        timeout_seconds=settings.mail163_email_timeout_seconds,
    )


@mail163_router.get(
    "/login",
    dependencies=[Depends(verify_database_ready)],
)
async def mail163_login(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """校验 .env 中的 163 授权码并签发应用登录 JWT。"""
    if not settings.jwt_secret:
        return _login_error_page("JWT_SECRET 尚未配置", "请在 backend/.env 中设置 JWT_SECRET 后重启后端。", settings.frontend_url)

    try:
        config = _require_mail163_credentials(settings)
        email = await verify_login(config)
    except Mail163Error as exc:
        return _login_error_page("163 邮箱登录失败", exc.message, settings.frontend_url)

    user = await user_service.get_or_create_user_by_email(session, email)
    jwt_token = create_access_token(user.id, email, settings.jwt_secret)
    return oauth_success_redirect(settings.frontend_url, jwt_token)


@mail163_router.get(
    "/status",
    response_model=Mail163StatusResponse,
    dependencies=[Depends(verify_database_ready)],
)
async def get_mail163_status(
    user_id: uuid.UUID = Depends(get_current_user_id),
    settings: Settings = Depends(get_settings),
) -> Mail163StatusResponse:
    """返回系统级 163 邮箱配置是否存在；不执行实时 IMAP 登录。"""
    _ = user_id
    configured = has_mail163_credentials(settings)
    return Mail163StatusResponse(
        is_connected=configured,
        provider_account_id=settings.mail163_email if configured else None,
    )


@mail163_router.post(
    "/disconnect",
    dependencies=[Depends(verify_database_ready)],
)
async def post_disconnect_mail163(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    """163 邮箱没有服务端撤销机制；这里保留端点并返回本地断开。"""
    _ = session
    from services.agent_runner import invalidate_token_cache

    await invalidate_token_cache(user_id, "mail163")
    return {"disconnected": True}