import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user_notion_integration import NOTION_PROVIDER, UserNotionIntegration


async def get_notion_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> UserNotionIntegration | None:
    result = await session.execute(
        select(UserNotionIntegration).where(UserNotionIntegration.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def upsert_notion_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
    access_token: str,
    workspace_id: str | None,
    workspace_name: str | None,
    bot_id: str | None,
    provider_account_id: str,
) -> UserNotionIntegration:
    integration = await get_notion_integration(session, user_id)

    if integration is None:
        integration = UserNotionIntegration(
            user_id=user_id,
            provider=NOTION_PROVIDER,
        )
        session.add(integration)

    integration.access_token = access_token
    integration.workspace_id = workspace_id
    integration.workspace_name = workspace_name
    integration.bot_id = bot_id
    integration.provider_account_id = provider_account_id
    integration.is_connected = True
    integration.connected_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(integration)
    return integration


async def disconnect_notion_integration(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> bool:
    """Clear Notion OAuth state. Returns True if a row was updated."""
    integration = await get_notion_integration(session, user_id)
    if integration is None or not integration.is_connected:
        return False

    integration.access_token = None
    integration.workspace_id = None
    integration.workspace_name = None
    integration.bot_id = None
    integration.provider_account_id = None
    integration.is_connected = False
    integration.connected_at = None
    await session.flush()
    return True
