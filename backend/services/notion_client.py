"""Thin async wrapper around the Notion REST API using httpx.

All methods accept an access_token (obtained via Notion OAuth) and call the
Notion API directly. Notion tokens do not expire, so there is no refresh flow.
"""

from typing import Any

import httpx

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"


def normalize_notion_uuid(value: str) -> str:
    """Notion accepts UUIDs with or without hyphens; normalize to dashed form.

    Copy-pasted Notion URLs often use 32-character hex ids without separators.
    """
    stripped = value.strip()
    compact = stripped.replace("-", "")
    if len(compact) != 32:
        return stripped
    hex_lower = compact.lower()
    allowed = frozenset("0123456789abcdef")
    if any(ch not in allowed for ch in hex_lower):
        return stripped
    return (
        f"{hex_lower[:8]}-{hex_lower[8:12]}-{hex_lower[12:16]}-"
        f"{hex_lower[16:20]}-{hex_lower[20:]}"
    )

HTTP_TIMEOUT_SECONDS = 30
DEFAULT_PAGE_SIZE = 100


class NotionApiError(Exception):
    """Raised when the Notion API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Notion API {status_code}: {detail}")


def _auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


async def _notion_request(
    method: str,
    path: str,
    access_token: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{NOTION_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.request(
            method,
            url,
            headers=_auth_headers(access_token),
            params=params,
            json=json_body,
        )

    if not response.is_success:
        raise NotionApiError(response.status_code, response.text)

    if response.status_code == 204:
        return {}
    return response.json()


# ── Pages ─────────────────────────────────────────────────────────────────


async def create_page(
    access_token: str,
    parent: dict[str, Any],
    properties: dict[str, Any],
    *,
    children: list[dict[str, Any]] | None = None,
    icon: dict[str, Any] | None = None,
    cover: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"parent": parent, "properties": properties}
    if children:
        body["children"] = children
    if icon:
        body["icon"] = icon
    if cover:
        body["cover"] = cover
    return await _notion_request("POST", "/pages", access_token, json_body=body)


async def get_page(access_token: str, page_id: str) -> dict[str, Any]:
    return await _notion_request("GET", f"/pages/{page_id}", access_token)


async def update_page(
    access_token: str,
    page_id: str,
    *,
    properties: dict[str, Any] | None = None,
    archived: bool | None = None,
    icon: dict[str, Any] | None = None,
    cover: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if properties is not None:
        body["properties"] = properties
    if archived is not None:
        body["archived"] = archived
    if icon is not None:
        body["icon"] = icon
    if cover is not None:
        body["cover"] = cover
    return await _notion_request("PATCH", f"/pages/{page_id}", access_token, json_body=body)


async def archive_page(access_token: str, page_id: str) -> dict[str, Any]:
    return await update_page(access_token, page_id, archived=True)


# ── Databases ─────────────────────────────────────────────────────────────


async def create_database(
    access_token: str,
    parent: dict[str, Any],
    title: list[dict[str, Any]],
    properties: dict[str, Any],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "parent": parent,
        "title": title,
        "properties": properties,
    }
    return await _notion_request("POST", "/databases", access_token, json_body=body)


async def get_database(access_token: str, database_id: str) -> dict[str, Any]:
    return await _notion_request("GET", f"/databases/{database_id}", access_token)


async def update_database(
    access_token: str,
    database_id: str,
    *,
    title: list[dict[str, Any]] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if properties is not None:
        body["properties"] = properties
    return await _notion_request("PATCH", f"/databases/{database_id}", access_token, json_body=body)


async def query_database(
    access_token: str,
    database_id: str,
    *,
    filter_obj: dict[str, Any] | None = None,
    sorts: list[dict[str, Any]] | None = None,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    body: dict[str, Any] = {"page_size": page_size}
    if filter_obj is not None:
        body["filter"] = filter_obj
    if sorts is not None:
        body["sorts"] = sorts
    if start_cursor is not None:
        body["start_cursor"] = start_cursor
    return await _notion_request(
        "POST", f"/databases/{database_id}/query", access_token, json_body=body,
    )


async def delete_database(access_token: str, database_id: str) -> dict[str, Any]:
    """Archive a database (Notion API does not have hard delete for databases)."""
    return await _notion_request(
        "PATCH", f"/databases/{database_id}", access_token,
        json_body={"archived": True},
    )


# ── Blocks ────────────────────────────────────────────────────────────────


async def append_blocks(
    access_token: str,
    block_id: str,
    children: list[dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {"children": children}
    return await _notion_request(
        "PATCH", f"/blocks/{block_id}/children", access_token, json_body=body,
    )


async def get_blocks(
    access_token: str,
    block_id: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    params: dict[str, Any] = {"page_size": page_size}
    if start_cursor:
        params["start_cursor"] = start_cursor
    return await _notion_request(
        "GET", f"/blocks/{block_id}/children", access_token, params=params,
    )


async def update_block(
    access_token: str,
    block_id: str,
    block_data: dict[str, Any],
) -> dict[str, Any]:
    return await _notion_request(
        "PATCH", f"/blocks/{block_id}", access_token, json_body=block_data,
    )


async def delete_block(access_token: str, block_id: str) -> dict[str, Any]:
    return await _notion_request("DELETE", f"/blocks/{block_id}", access_token)


# ── Page Properties ───────────────────────────────────────────────────────


async def get_page_properties(
    access_token: str,
    page_id: str,
    property_id: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    params: dict[str, Any] = {"page_size": page_size}
    if start_cursor:
        params["start_cursor"] = start_cursor
    return await _notion_request(
        "GET", f"/pages/{page_id}/properties/{property_id}", access_token, params=params,
    )


# ── Comments ──────────────────────────────────────────────────────────────


async def create_comment(
    access_token: str,
    parent: dict[str, Any],
    rich_text: list[dict[str, Any]],
) -> dict[str, Any]:
    body: dict[str, Any] = {"parent": parent, "rich_text": rich_text}
    return await _notion_request("POST", "/comments", access_token, json_body=body)


async def get_comments(
    access_token: str,
    block_id: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    params: dict[str, Any] = {"block_id": block_id, "page_size": page_size}
    if start_cursor:
        params["start_cursor"] = start_cursor
    return await _notion_request("GET", "/comments", access_token, params=params)


# ── Search ────────────────────────────────────────────────────────────────


async def search_workspace(
    access_token: str,
    *,
    query: str = "",
    filter_obj: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    body: dict[str, Any] = {"page_size": page_size}
    if query:
        body["query"] = query
    if filter_obj is not None:
        body["filter"] = filter_obj
    if sort is not None:
        body["sort"] = sort
    if start_cursor is not None:
        body["start_cursor"] = start_cursor
    return await _notion_request("POST", "/search", access_token, json_body=body)


# ── Users ─────────────────────────────────────────────────────────────────


async def list_users(
    access_token: str,
    *,
    start_cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    params: dict[str, Any] = {"page_size": page_size}
    if start_cursor:
        params["start_cursor"] = start_cursor
    return await _notion_request("GET", "/users", access_token, params=params)


async def get_current_user(access_token: str) -> dict[str, Any]:
    return await _notion_request("GET", "/users/me", access_token)
