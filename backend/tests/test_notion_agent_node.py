"""Tests for Notion agent parameter handling."""

from unittest.mock import AsyncMock, patch

from agents.tools.notion_agent_tools import make_notion_tools
from services.notion_client import normalize_notion_uuid


def test_normalize_notion_uuid_keeps_dashed_uuid() -> None:
    value = "a1b2c3d4-e5f6-7890-1234-5678901234ab"
    assert normalize_notion_uuid(value) == value


def test_normalize_notion_uuid_dashes_compact_id() -> None:
    compact = "a1b2c3d4e5f6789012345678901234ab"
    assert normalize_notion_uuid(compact) == "a1b2c3d4-e5f6-7890-1234-5678901234ab"


def test_make_notion_tools_exposes_expected_tools() -> None:
    tools = make_notion_tools("token")
    names = {tool.name for tool in tools}
    assert {"search_workspace", "get_page", "query_database", "create_page"}.issubset(names)


def test_search_workspace_forwards_query_and_filter() -> None:
    import asyncio

    async def invoke() -> str:
        tools = make_notion_tools("token")
        search_tool = next(tool for tool in tools if tool.name == "search_workspace")
        with patch("agents.tools.notion_agent_tools._search_workspace", new=AsyncMock(return_value={"results": []})):
            return await search_tool.ainvoke(
                {"query": "tasks", "filter_type": "database", "page_size": 3}
            )

    result = asyncio.run(invoke())
    assert result is not None


async def _bypass_cache(_key, _ttl, fetch_fn):
    """跳过 Redis，直接调用真实取 token 逻辑。"""
    return await fetch_fn()


def test_notion_api_key_used_when_oauth_not_connected() -> None:
    """静态内部集成 token 必须能顶替 OAuth，否则 notion_agent 直接报"未连接"。"""
    import asyncio
    import uuid
    from unittest.mock import MagicMock, patch

    from config import Settings
    from services.agent_runner import get_all_tokens

    async def run() -> tuple:
        settings = MagicMock(spec=Settings)
        settings.notion_api_key = "ntn_static_key"
        settings.google_client_id = None
        settings.google_client_secret = None
        with patch(
            "services.agent_runner._get_cached_token",
            new=_bypass_cache,
        ), patch(
            "services.agent_runner._fetch_gmail_access_token",
            new=AsyncMock(return_value=None),
        ), patch(
            "services.agent_runner._fetch_calendar_access_token",
            new=AsyncMock(return_value=None),
        ), patch(
            "services.agent_runner._fetch_notion_access_token",
            new=AsyncMock(return_value=None),
        ):
            return await get_all_tokens(MagicMock(), settings, uuid.uuid4())

    _, _, notion_token = asyncio.run(run())
    assert notion_token == "ntn_static_key"


def test_oauth_token_wins_over_static_api_key() -> None:
    import asyncio
    import uuid
    from unittest.mock import MagicMock, patch

    from config import Settings
    from services.agent_runner import get_all_tokens

    async def run() -> tuple:
        settings = MagicMock(spec=Settings)
        settings.notion_api_key = "ntn_static_key"
        with patch(
            "services.agent_runner._get_cached_token",
            new=_bypass_cache,
        ), patch(
            "services.agent_runner._fetch_gmail_access_token",
            new=AsyncMock(return_value=None),
        ), patch(
            "services.agent_runner._fetch_calendar_access_token",
            new=AsyncMock(return_value=None),
        ), patch(
            "services.agent_runner._fetch_notion_access_token",
            new=AsyncMock(return_value="oauth_token"),
        ):
            return await get_all_tokens(MagicMock(), settings, uuid.uuid4())

    _, _, notion_token = asyncio.run(run())
    assert notion_token == "oauth_token"
