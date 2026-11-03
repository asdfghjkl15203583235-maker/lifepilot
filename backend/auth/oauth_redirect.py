"""OAuth callback redirect: SPA reads JWT from URL fragment when cookies are not cross-site."""

from urllib.parse import quote, urlparse, urlunparse

from fastapi.responses import RedirectResponse
from starlette import status

from auth.constants import AUTH_COOKIE_NAME


def build_frontend_oauth_redirect_url(frontend_url: str, jwt_token: str) -> str:
    parsed = urlparse(frontend_url)
    path = parsed.path if parsed.path else "/"
    fragment = f"session={quote(jwt_token, safe='')}"
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, fragment))


def oauth_success_redirect(frontend_url: str, jwt_token: str) -> RedirectResponse:
    """Redirect to SPA with JWT in fragment (cross-origin) plus HttpOnly cookie (same-origin)."""
    url = build_frontend_oauth_redirect_url(frontend_url, jwt_token)
    response = RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=jwt_token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
        path="/",
    )
    return response
