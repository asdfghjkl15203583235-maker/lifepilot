import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from auth.constants import AUTH_COOKIE_NAME
from auth.dependencies import get_current_user_claims
from schemas.user import UserResponse

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.get("/me", response_model=UserResponse)
async def get_current_user(claims: dict = Depends(get_current_user_claims)) -> UserResponse:
    return UserResponse(
        id=uuid.UUID(claims["sub"]),
        email=claims["email"],
        name=claims["email"].split("@")[0],
    )


@auth_router.post("/logout")
async def logout() -> JSONResponse:
    """Clear session cookie so the browser is signed out (safe to call without a cookie)."""
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response
