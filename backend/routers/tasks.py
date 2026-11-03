import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_user_id
from db.dependencies import verify_database_ready
from db.session import get_session
from schemas.task import TaskCreateRequest, TaskResponse, TaskUpdateRequest
from services import task_planner_service, task_service

tasks_router = APIRouter(
    prefix="/tasks",
    tags=["tasks"],
    dependencies=[Depends(verify_database_ready)],
)


@tasks_router.get("", response_model=list[TaskResponse])
async def list_tasks(
    project: str | None = Query(default=None),
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> list[TaskResponse]:
    tasks = await task_service.list_tasks_for_user(session, user_id=user_id, project=project)
    return [TaskResponse.model_validate(t) for t in tasks]


@tasks_router.get("/scan-plan")
async def scan_task_plan(
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await task_planner_service.scan_user_task_plan(session, user_id=user_id)


@tasks_router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    """Single-task lookup for detail deep links.

    The board's detail drawer used to depend on the already-fetched list, so a
    link to a task outside the current filter/cache silently showed nothing.
    """
    task = await task_service.get_task_for_user(session, task_id=task_id, user_id=user_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到任务")
    return TaskResponse.model_validate(task)


@tasks_router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    task = await task_service.create_task_for_user(session, user_id=user_id, body=body)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到用户")
    return TaskResponse.model_validate(task)


@tasks_router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> TaskResponse:
    task = await task_service.update_task(session, task_id=task_id, user_id=user_id, body=body)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到任务")
    return TaskResponse.model_validate(task)


@tasks_router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> None:
    deleted = await task_service.delete_task(session, task_id=task_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到任务")
