"""Best-effort revocation of Google OAuth tokens (Gmail / Calendar disconnect)."""

import logging

import httpx

logger = logging.getLogger(__name__)

GOOGLE_OAUTH_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"


async def revoke_google_oauth_token(token: str | None) -> None:
    """POST to Google's revoke endpoint; logs and ignores failures so local state can still clear."""
    if not token:
        return
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                GOOGLE_OAUTH_REVOKE_ENDPOINT,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )
    except httpx.HTTPError as exc:
        logger.warning("Google token revoke request failed: %s", exc)
        return
    if response.status_code not in (200, 400):
        logger.warning(
            "Google token revoke returned HTTP %s: %s",
            response.status_code,
            response.text[:200],
        )
