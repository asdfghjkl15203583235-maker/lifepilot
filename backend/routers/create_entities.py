from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_claims
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.user import UserCreateRequest, UserResponse
from services import user_service

create_entities_router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(verify_database_ready), Depends(get_current_user_claims)],
)


@create_entities_router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_only(
    body: UserCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> UserResponse:
    try:
        user = await user_service.create_user(session, body)
        return UserResponse.model_validate(user)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已存在使用该邮箱的用户",
        ) from None
