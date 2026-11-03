"""Notion agent tools — thin wrappers around services.notion_client.

Each standalone ``tool_*`` function accepts an access_token and delegates to the Notion
client. ``make_notion_tools(access_token)`` returns a list of LangChain tools with the token
closed over so it never appears in the LLM's tool schema.
"""

import json
from typing import Any

from langchain_core.tools import tool

from services.notion_client import (
    append_blocks as _append_blocks,
    archive_page as _archive_page,
    create_comment as _create_comment,
    create_database as _create_database,
    create_page as _create_page,
    delete_block as _delete_block,
    delete_database as _delete_database,
    get_blocks as _get_blocks,
    get_comments as _get_comments,
    get_current_user as _get_current_user,
    get_database as _get_database,
    get_page as _get_page,
    get_page_properties as _get_page_properties,
    list_users as _list_users,
    query_database as _query_database,
    search_workspace as _search_workspace,
    update_block as _update_block,
    update_database as _update_database,
    update_page as _update_page,
)

DEFAULT_PAGE_SIZE = 100


# ── Standalone tool functions (kept for backward compat) ─────────────────


async def tool_create_page(
    access_token: str,
    parent: dict[str, Any],
    properties: dict[str, Any],
    *,
    children: list[dict[str, Any]] | None = None,
    icon: dict[str, Any] | None = None,
    cover: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new page in a database or as a child of another page."""
    return await _create_page(
        access_token, parent, properties,
        children=children, icon=icon, cover=cover,
    )


async def tool_get_page(access_token: str, page_id: str) -> dict[str, Any]:
    """Retrieve a page by its ID."""
    return await _get_page(access_token, page_id)


async def tool_update_page(
    access_token: str,
    page_id: str,
    *,
    properties: dict[str, Any] | None = None,
    archived: bool | None = None,
    icon: dict[str, Any] | None = None,
    cover: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update a page's properties, icon, cover, or archived status."""
    return await _update_page(
        access_token, page_id,
        properties=properties, archived=archived, icon=icon, cover=cover,
    )


async def tool_archive_page(access_token: str, page_id: str) -> dict[str, Any]:
    """Archive (soft-delete) a page."""
    return await _archive_page(access_token, page_id)


async def tool_create_database(
    access_token: str,
    parent: dict[str, Any],
    title: list[dict[str, Any]],
    properties: dict[str, Any],
) -> dict[str, Any]:
    """Create a new database as a child of a page."""
    return await _create_database(access_token, parent, title, properties)


async def tool_get_database(access_token: str, database_id: str) -> dict[str, Any]:
    """Retrieve a database schema by its ID."""
    return await _get_database(access_token, database_id)


async def tool_update_database(
    access_token: str,
    database_id: str,
    *,
    title: list[dict[str, Any]] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update a database's title or property schema."""
    return await _update_database(
        access_token, database_id, title=title, properties=properties,
    )


async def tool_query_database(
    access_token: str,
    database_id: str,
    *,
    filter_obj: dict[str, Any] | None = None,
    sorts: list[dict[str, Any]] | None = None,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Query a database with optional filter, sorts, and pagination."""
    return await _query_database(
        access_token, database_id,
        filter_obj=filter_obj, sorts=sorts,
        start_cursor=start_cursor, page_size=page_size,
    )


async def tool_delete_database(access_token: str, database_id: str) -> dict[str, Any]:
    """Archive a database (Notion does not support hard deletes)."""
    return await _delete_database(access_token, database_id)


async def tool_append_blocks(
    access_token: str,
    block_id: str,
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    """Append child blocks to a parent block (page or block)."""
    return await _append_blocks(access_token, block_id, children)


async def tool_get_blocks(
    access_token: str,
    block_id: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Retrieve children blocks of a block or page."""
    return await _get_blocks(
        access_token, block_id,
        start_cursor=start_cursor, page_size=page_size,
    )


async def tool_update_block(
    access_token: str,
    block_id: str,
    block_data: dict[str, Any],
) -> dict[str, Any]:
    """Update an existing block's content or type-specific data."""
    return await _update_block(access_token, block_id, block_data)


async def tool_delete_block(access_token: str, block_id: str) -> dict[str, Any]:
    """Delete a block by its ID."""
    return await _delete_block(access_token, block_id)


async def tool_get_page_properties(
    access_token: str,
    page_id: str,
    property_id: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Retrieve a specific property value from a page (for paginated props like rollups/relations)."""
    return await _get_page_properties(
        access_token, page_id, property_id,
        start_cursor=start_cursor, page_size=page_size,
    )


async def tool_create_comment(
    access_token: str,
    parent: dict[str, Any],
    rich_text: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a comment on a page or discussion thread."""
    return await _create_comment(access_token, parent, rich_text)


async def tool_get_comments(
    access_token: str,
    block_id: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Retrieve comments for a block or page."""
    return await _get_comments(
        access_token, block_id,
        start_cursor=start_cursor, page_size=page_size,
    )


async def tool_search_workspace(
    access_token: str,
    *,
    query: str = "",
    filter_obj: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Search the entire Notion workspace for pages and databases."""
    return await _search_workspace(
        access_token,
        query=query, filter_obj=filter_obj, sort=sort,
        start_cursor=start_cursor, page_size=page_size,
    )


async def tool_list_users(
    access_token: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """List all users in the workspace."""
    return await _list_users(
        access_token,
        start_cursor=start_cursor, page_size=page_size,
    )


async def tool_get_current_user(access_token: str) -> dict[str, Any]:
    """Get the bot user associated with the current integration token."""
    return await _get_current_user(access_token)


# ── LangChain tool factory ────────────────────────────────────────────────


def make_notion_tools(access_token: str) -> list:
    """Return LangChain tools for Notion with the access token closed over."""

    @tool
    async def search_workspace(
        query: str = "",
        filter_type: str = "",
        page_size: int = 20,
    ) -> str:
        """Search the Notion workspace for pages and databases by name.
        filter_type can be 'page' or 'database' to narrow results. Always use this first when you don't have an ID."""
        filter_obj = None
        if filter_type in ("page", "database"):
            filter_obj = {"value": filter_type, "property": "object"}
        return json.dumps(
            await _search_workspace(
                access_token, query=query, filter_obj=filter_obj, page_size=page_size
            ),
            default=str,
        )

    @tool
    async def get_page(page_id: str) -> str:
        """Retrieve a Notion page by its ID."""
        return json.dumps(await _get_page(access_token, page_id), default=str)

    @tool
    async def create_page(
        parent: dict,
        properties: dict,
        children: list | None = None,
    ) -> str:
        """Create a new page. parent must be {"database_id": "<id>"} or {"page_id": "<id>"}."""
        return json.dumps(
            await _create_page(access_token, parent, properties, children=children),
            default=str,
        )

    @tool
    async def update_page(
        page_id: str,
        properties: dict | None = None,
        archived: bool | None = None,
    ) -> str:
        """Update a page's properties or archived status."""
        return json.dumps(
            await _update_page(access_token, page_id, properties=properties, archived=archived),
            default=str,
        )

    @tool
    async def archive_page(page_id: str) -> str:
        """Archive (soft-delete) a Notion page."""
        return json.dumps(await _archive_page(access_token, page_id), default=str)

    @tool
    async def get_database(database_id: str) -> str:
        """Retrieve a Notion database schema by its ID."""
        return json.dumps(await _get_database(access_token, database_id), default=str)

    @tool
    async def query_database(
        database_id: str,
        filter_obj: dict | None = None,
        sorts: list | None = None,
        page_size: int = 100,
    ) -> str:
        """Query a Notion database with optional filters and sorts."""
        return json.dumps(
            await _query_database(
                access_token, database_id,
                filter_obj=filter_obj, sorts=sorts, page_size=page_size,
            ),
            default=str,
        )

    @tool
    async def create_database(
        parent: dict,
        title: list,
        properties: dict,
    ) -> str:
        """Create a new Notion database. parent must be {"page_id": "<id>"}."""
        return json.dumps(
            await _create_database(access_token, parent, title, properties), default=str
        )

    @tool
    async def update_database(
        database_id: str,
        title: list | None = None,
        properties: dict | None = None,
    ) -> str:
        """Update a Notion database title or property schema."""
        return json.dumps(
            await _update_database(
                access_token, database_id, title=title, properties=properties
            ),
            default=str,
        )

    @tool
    async def append_blocks(block_id: str, children: list) -> str:
        """Append child blocks to a Notion page or block."""
        return json.dumps(
            await _append_blocks(access_token, block_id, children), default=str
        )

    @tool
    async def get_blocks(block_id: str, page_size: int = 100) -> str:
        """Retrieve child blocks of a Notion page or block."""
        return json.dumps(
            await _get_blocks(access_token, block_id, page_size=page_size), default=str
        )

    @tool
    async def update_block(block_id: str, block_data: dict) -> str:
        """Update an existing Notion block's content."""
        return json.dumps(
            await _update_block(access_token, block_id, block_data), default=str
        )

    @tool
    async def delete_block(block_id: str) -> str:
        """Delete a Notion block."""
        return json.dumps(await _delete_block(access_token, block_id), default=str)

    @tool
    async def create_comment(parent: dict, rich_text: list) -> str:
        """Create a comment on a Notion page. parent must be {"page_id": "<id>"}."""
        return json.dumps(
            await _create_comment(access_token, parent, rich_text), default=str
        )

    @tool
    async def get_comments(block_id: str) -> str:
        """Retrieve comments for a Notion page or block."""
        return json.dumps(await _get_comments(access_token, block_id), default=str)

    @tool
    async def list_users() -> str:
        """List all users in the Notion workspace."""
        return json.dumps(await _list_users(access_token), default=str)

    @tool
    async def get_current_user() -> str:
        """Get the bot user for the current Notion integration."""
        return json.dumps(await _get_current_user(access_token), default=str)

    return [
        search_workspace,
        get_page,
        create_page,
        update_page,
        archive_page,
        get_database,
        query_database,
        create_database,
        update_database,
        append_blocks,
        get_blocks,
        update_block,
        delete_block,
        create_comment,
        get_comments,
        list_users,
        get_current_user,
    ]
