import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user_gmail_integration import GMAIL_PROVIDER, GMAIL_SCOPES, UserGmailIntegration


async def get_gmail_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> UserGmailIntegration | None:
    result = await session.execute(
        select(UserGmailIntegration).where(UserGmailIntegration.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def upsert_gmail_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
    access_token: str,
    refresh_token: str | None,
    token_expiry: int | None,
    provider_account_id: str,
) -> UserGmailIntegration:
    integration = await get_gmail_integration(session, user_id)

    if integration is None:
        integration = UserGmailIntegration(
            user_id=user_id,
            provider=GMAIL_PROVIDER,
            scope=GMAIL_SCOPES,
        )
        session.add(integration)

    integration.access_token = access_token
    integration.refresh_token = refresh_token
    integration.token_expiry = token_expiry
    integration.provider_account_id = provider_account_id
    integration.is_connected = True
    integration.connected_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(integration)
    return integration


async def disconnect_gmail_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> bool:
    """Clear OAuth state for Gmail. Returns True if a row was updated."""
    from services.google_token_revocation import revoke_google_oauth_token

    integration = await get_gmail_integration(session, user_id)
    if integration is None or not integration.is_connected:
        return False

    await revoke_google_oauth_token(integration.access_token)
    await revoke_google_oauth_token(integration.refresh_token)

    integration.access_token = None
    integration.refresh_token = None
    integration.token_expiry = None
    integration.provider_account_id = None
    integration.is_connected = False
    integration.connected_at = None
    await session.flush()
    return True
