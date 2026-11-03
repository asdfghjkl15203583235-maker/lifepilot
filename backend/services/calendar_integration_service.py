import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user_calendar_integration import CALENDAR_PROVIDER, CALENDAR_SCOPES, UserCalendarIntegration


async def get_calendar_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> UserCalendarIntegration | None:
    result = await session.execute(
        select(UserCalendarIntegration).where(UserCalendarIntegration.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def upsert_calendar_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
    access_token: str,
    refresh_token: str | None,
    token_expiry: int | None,
    provider_account_id: str,
) -> UserCalendarIntegration:
    integration = await get_calendar_integration(session, user_id)

    if integration is None:
        integration = UserCalendarIntegration(
            user_id=user_id,
            provider=CALENDAR_PROVIDER,
            scope=CALENDAR_SCOPES,
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


async def disconnect_calendar_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> bool:
    """Clear OAuth state for Google Calendar. Returns True if a row was updated."""
    from services.google_token_revocation import revoke_google_oauth_token

    integration = await get_calendar_integration(session, user_id)
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
