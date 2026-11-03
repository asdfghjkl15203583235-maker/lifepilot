"""Thin async wrapper around the Gmail REST API using httpx.

All methods accept an access_token and call the Gmail API directly.
Token refresh is handled by `get_valid_access_token` before calling any method.
"""

import base64
import time
from email.mime.text import MIMEText
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from models.user_gmail_integration import UserGmailIntegration

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

TOKEN_EXPIRY_BUFFER_SECONDS = 300

# https://developers.google.com/gmail/api/reference/rest/v1/users.messages/list
GMAIL_API_MAX_RESULTS_PER_REQUEST = 500

HTTP_TIMEOUT_SECONDS = 30


class GmailApiError(Exception):
    """Raised when the Gmail API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Gmail API {status_code}: {detail}")


async def get_valid_access_token(
    session: AsyncSession,
    integration: UserGmailIntegration,
    client_id: str,
    client_secret: str,
) -> str:
    """Return a valid access token, refreshing if expired."""
    is_expired = (
        integration.token_expiry is not None
        and integration.token_expiry < int(time.time()) + TOKEN_EXPIRY_BUFFER_SECONDS
    )

    if not is_expired and integration.access_token:
        return integration.access_token

    if not integration.refresh_token:
        raise GmailApiError(401, "没有可用的刷新令牌，用户需要重新授权")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": integration.refresh_token,
                "grant_type": "refresh_token",
            },
        )

    if response.status_code != 200:
        raise GmailApiError(response.status_code, "令牌刷新失败")

    token_data = response.json()
    integration.access_token = token_data["access_token"]
    expires_in: int | None = token_data.get("expires_in")
    if expires_in:
        integration.token_expiry = int(time.time()) + expires_in

    await session.flush()
    return integration.access_token


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


async def _gmail_request(
    method: str,
    path: str,
    access_token: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{GMAIL_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.request(
            method,
            url,
            headers=_auth_headers(access_token),
            params=params,
            json=json_body,
        )

    if not response.is_success:
        raise GmailApiError(response.status_code, response.text)

    if response.status_code == 204:
        return {}
    return response.json()


# ── Read / Get email ─────────────────────────────────────────────────────

async def get_email(access_token: str, message_id: str) -> dict[str, Any]:
    return await _gmail_request("GET", f"/messages/{message_id}", access_token, params={"format": "full"})


# ── List / Search emails ─────────────────────────────────────────────────

async def list_emails(
    access_token: str,
    *,
    query: str = "",
    max_results: int | None = None,
    page_token: str | None = None,
) -> dict[str, Any]:
    resolved = (
        max_results
        if max_results is not None
        else get_settings().gmail_list_max_results
    )
    capped = min(resolved, GMAIL_API_MAX_RESULTS_PER_REQUEST)
    params: dict[str, Any] = {"maxResults": capped}
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
    return await _gmail_request("GET", "/messages", access_token, params=params)


# ── Send email ────────────────────────────────────────────────────────────

def _build_raw_message(to: str, subject: str, body: str) -> str:
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return raw


async def send_email(access_token: str, to: str, subject: str, body: str) -> dict[str, Any]:
    raw = _build_raw_message(to, subject, body)
    return await _gmail_request("POST", "/messages/send", access_token, json_body={"raw": raw})


# ── Reply to email ────────────────────────────────────────────────────────

async def reply_to_email(
    access_token: str,
    thread_id: str,
    message_id: str,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    raw = _build_raw_message(to, subject, body)
    return await _gmail_request(
        "POST",
        "/messages/send",
        access_token,
        json_body={"raw": raw, "threadId": thread_id},
    )


# ── Create draft ──────────────────────────────────────────────────────────

async def create_draft(access_token: str, to: str, subject: str, body: str) -> dict[str, Any]:
    raw = _build_raw_message(to, subject, body)
    return await _gmail_request(
        "POST",
        "/drafts",
        access_token,
        json_body={"message": {"raw": raw}},
    )


# ── Get attachments ───────────────────────────────────────────────────────

async def get_attachment(access_token: str, message_id: str, attachment_id: str) -> dict[str, Any]:
    return await _gmail_request(
        "GET",
        f"/messages/{message_id}/attachments/{attachment_id}",
        access_token,
    )


# ── Mark as read / unread ─────────────────────────────────────────────────

async def mark_as_read(access_token: str, message_id: str) -> dict[str, Any]:
    return await _gmail_request(
        "POST",
        f"/messages/{message_id}/modify",
        access_token,
        json_body={"removeLabelIds": ["UNREAD"]},
    )


async def mark_as_unread(access_token: str, message_id: str) -> dict[str, Any]:
    return await _gmail_request(
        "POST",
        f"/messages/{message_id}/modify",
        access_token,
        json_body={"addLabelIds": ["UNREAD"]},
    )


# ── Archive / Trash ───────────────────────────────────────────────────────

async def archive_email(access_token: str, message_id: str) -> dict[str, Any]:
    return await _gmail_request(
        "POST",
        f"/messages/{message_id}/modify",
        access_token,
        json_body={"removeLabelIds": ["INBOX"]},
    )


async def trash_email(access_token: str, message_id: str) -> dict[str, Any]:
    return await _gmail_request("POST", f"/messages/{message_id}/trash", access_token)


# ── List labels ───────────────────────────────────────────────────────────

async def list_labels(access_token: str) -> dict[str, Any]:
    return await _gmail_request("GET", "/labels", access_token)


# ── Get user profile ─────────────────────────────────────────────────────

async def get_user_profile(access_token: str) -> dict[str, Any]:
    return await _gmail_request("GET", "/profile", access_token)
