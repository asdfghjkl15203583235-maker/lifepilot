"""Thin async wrapper around the Tencent Meeting REST API using httpx.

Tencent Meeting requires an app credential (appId + secretId + secretKey) and a
stable user ID for most endpoints. The wrapper is intentionally provider-agnostic:
callers pass configured credentials through so this service can be swapped for the
MCP server later without touching agent tools.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from config import Settings

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 30


class TencentMeetingApiError(Exception):
    """Raised when Tencent Meeting returns a non-2xx response or a business error."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Tencent Meeting API {status_code}: {detail}")


class TencentMeetingConfigError(Exception):
    """Raised when Tencent Meeting credentials are not configured."""


class TencentMeetingClient:
    """Minimal client; secrets are held by the caller from Settings."""

    def __init__(self, app_id: str, secret_id: str, secret_key: str, user_id: str, base_url: str) -> None:
        self._app_id = app_id
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._user_id = user_id
        self._base_url = base_url.rstrip("/")

    def _sign(self, method: str, path: str, params: dict[str, Any], body: dict[str, Any]) -> str:
        allowed = "&".join(
            f"{k}={v}" for k, v in sorted(params.items()) if v is not None and k != "nonce"
        )
        body_str = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        string_to_sign = (
            f"{method.upper()}\n{path}\n{allowed}\n{body_str}\n"
            f"X-TC-Action=DescribeMeetings\nX-TC-Key={self._secret_id}\nX-TC-Timestamp={int(time.time())}"
        )
        digest = hmac.new(self._secret_key.encode(), string_to_sign.encode(), hashlib.sha1).digest()
        return digest.hex()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        payload = body or {}
        headers = {
            "X-TC-Action": "DescribeMeetings",
            "X-TC-Key": self._secret_id,
            "X-TC-Timestamp": str(int(time.time())),
            "X-TC-Signature": self._sign(method, path, params, payload),
            "X-TC-AppId": self._app_id,
            "X-TC-Registered": "1",
            "X-TC-UserID": self._user_id,
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.request(method, url, headers=headers, params=params, json=payload)
        if response.status_code != 200:
            raise TencentMeetingApiError(response.status_code, response.text[:1000])
        data = response.json()
        # Tencent Meeting uses `error_code` in the response body for business errors.
        error_code = data.get("error_code")
        if error_code not in (None, 0):
            raise TencentMeetingApiError(0, f"{error_code}: {data.get('error_info', '')}")
        return data

    async def list_meetings(self, **filters: Any) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/meetings",
            params={"userid": self._user_id, **filters},
        )

    async def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/meetings/{meeting_id}", params={"userid": self._user_id})

    async def create_meeting(
        self,
        *,
        title: str,
        start_time: int | None = None,
        end_time: int | None = None,
        participants: list[str] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "subject": title,
            "userid": self._user_id,
        }
        if start_time is not None:
            body["start_time"] = start_time
        if end_time is not None:
            body["end_time"] = end_time
        if participants:
            body["participants"] = participants
        if settings:
            body.update(settings)
        return await self._request("POST", "/meetings", body=body)

    async def update_meeting(
        self,
        meeting_id: str,
        *,
        title: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"meeting_id": meeting_id, "userid": self._user_id}
        if title is not None:
            body["subject"] = title
        if start_time is not None:
            body["start_time"] = start_time
        if end_time is not None:
            body["end_time"] = end_time
        return await self._request("PUT", f"/meetings/{meeting_id}", body=body)


def build_tencent_meeting_client(settings: Settings) -> TencentMeetingClient:
    if not (settings.tencent_meeting_app_id and settings.tencent_meeting_secret_id and settings.tencent_meeting_secret_key):
        raise TencentMeetingConfigError(
            "Tencent Meeting credentials are not configured "
            "(TENCENT_MEETING_APP_ID / SECRET_ID / SECRET_KEY).",
        )
    return TencentMeetingClient(
        app_id=settings.tencent_meeting_app_id,
        secret_id=settings.tencent_meeting_secret_id,
        secret_key=settings.tencent_meeting_secret_key,
        user_id=settings.tencent_meeting_user_id or str(UUID(int=0)),
        base_url=settings.tencent_meeting_base_url,
    )