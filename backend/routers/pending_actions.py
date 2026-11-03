import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from config import Settings, get_settings
from db.dependencies import verify_database_ready
from db.session import get_session
from lib.enums import PendingActionStatus
from schemas.pending_action import PendingActionResponse
from services.mail163_client import Mail163Error
from services.pending_action_service import (
    PendingActionNotFoundError,
    PendingActionNotApprovableError,
    approve_and_execute,
    list_pending_actions,
    reject_pending_action,
)

pending_actions_router = APIRouter(
    prefix="/pending-actions",
    tags=["pending-actions"],
    dependencies=[Depends(verify_database_ready)],
)


@pending_actions_router.get("", response_model=list[PendingActionResponse])
async def list_actions(
    status_filter: PendingActionStatus | None = Query(default=None, alias="status"),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[PendingActionResponse]:
    actions = await list_pending_actions(
        session, user_id=user_id, status=status_filter,
    )
    return [PendingActionResponse.model_validate(a) for a in actions]


@pending_actions_router.post("/{action_id}/approve")
async def approve_action(
    action_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        action, result = await approve_and_execute(
            session, user_id, action_id, settings=settings,
        )
    except PendingActionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except PendingActionNotApprovableError as exc:
        # Persist the terminal FAILED/EXPIRED state written by approve_and_execute
        # rather than rolling it back, so the draft's lifecycle is always recorded.
        await session.commit()
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc))
    except Mail163Error as exc:
        # 邮箱侧临时故障（限频/网络）：把 PENDING 状态和错误原因落库后返回 502，
        # 前端提示用户再点一次，而不是抛 500。
        await session.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    await session.commit()
    return {"status": action.status.value, "result": result}


@pending_actions_router.post("/{action_id}/reject")
async def reject_action(
    action_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        action = await reject_pending_action(session, user_id, action_id)
    except PendingActionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    await session.commit()
    return {"status": action.status.value}
