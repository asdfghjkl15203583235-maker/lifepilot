import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import TaskStatus
from models.task import Task
from schemas.task import TaskCreateRequest, TaskUpdateRequest
from services.idempotency_service import (
    acquire_task_creation_lock,
    normalize_task_title,
)
from services.task_project_classifier import classify_task_project
from services.user_service import get_user_by_id

logger = logging.getLogger(__name__)


def _task_signature_statement(*, user_id: uuid.UUID, title: str, due_at: datetime | None):
    """Select the user-visible task matching `user_id + title + due_at`.

    与 `tasks` 唯一索引（`user_id, title, coalesce(due_at, epoch)`）保持同一口径：
    `source='system'` 的内部任务不参与去重。
    """
    stmt = select(Task).where(
        Task.user_id == user_id,
        Task.title == title,
        Task.source != "system",
    )
    if due_at is None:
        stmt = stmt.where(Task.due_at.is_(None))
    else:
        stmt = stmt.where(Task.due_at == due_at)
    return stmt.order_by(Task.created_at.asc()).limit(1)


async def find_task_by_signature(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    title: str,
    due_at: datetime | None,
) -> Task | None:
    """Return the already-created task for this signature, if any."""
    result = await session.execute(
        _task_signature_statement(user_id=user_id, title=title, due_at=due_at)
    )
    return result.scalar_one_or_none()


async def _insert_task(session: AsyncSession, task: Task) -> None:
    """Insert with a savepoint so an IntegrityError never poisons the caller's txn.

    Planner steps share one outer transaction; a failed unique-index insert must
    only roll back this row, not the whole plan. Non-`AsyncSession` handles (test
    doubles without savepoint support) fall back to a plain insert.
    """
    if not isinstance(session, AsyncSession):
        session.add(task)
        await session.flush()
        return
    async with session.begin_nested():
        session.add(task)
        await session.flush()


async def create_task_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    body: TaskCreateRequest,
) -> Task | None:
    """Create a task, never a duplicate of an identical one.

    双层防御：
      1. Redis 锁（`user_id + title + due_at`，TTL 300s）拦住并发/重复创建；
      2. 数据库唯一索引兜底，插入冲突时返回已存在的任务而不是抛错。
    """
    owner = await get_user_by_id(session, user_id)
    if owner is None:
        return None

    title = normalize_task_title(body.title)
    due_at = body.due_at

    lock_state = await acquire_task_creation_lock(user_id, title, due_at)
    if lock_state is False:
        existing = await find_task_by_signature(
            session, user_id=user_id, title=title, due_at=due_at
        )
        if existing is not None:
            logger.info(
                "Duplicate task suppressed by redis lock: user=%s title=%r due_at=%s",
                user_id,
                title,
                due_at,
            )
            return existing
        # 锁被占但没有可见行（对方尚未提交）→ 交给数据库唯一索引兜底。
        logger.info(
            "Task lock busy without a visible row; deferring to unique index: %r", title
        )

    task = Task(
        user_id=user_id,
        title=title,
        description=body.description,
        status=body.status,
        # project 必须落在四个枚举之一：为空会让前端项目过滤失效、看板出现
        # 无归属任务。这里用标题语义兜底。
        project=(
            body.project.strip()
            if body.project and body.project.strip()
            else classify_task_project(body.title)
        ),
        assigned_agent=body.assigned_agent,
        priority=body.priority,
        due_at=due_at,
        dependencies=list(body.dependencies or []),
        plan_id=body.plan_id,
        plan_source=body.plan_source,
        source=body.source or "user",
        source_metadata=body.source_metadata,
    )
    try:
        await _insert_task(session, task)
    except IntegrityError as exc:
        existing = await find_task_by_signature(
            session, user_id=user_id, title=title, due_at=due_at
        )
        if existing is None:
            logger.error(
                "Task insert rejected by unique index but no existing row found: %s", exc
            )
            raise
        logger.info(
            "Duplicate task blocked by unique index: user=%s title=%r due_at=%s",
            user_id,
            title,
            due_at,
        )
        return existing
    await session.refresh(task)
    return task


async def list_tasks_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    project: str | None = None,
) -> list[Task]:
    stmt = select(Task).where(Task.user_id == user_id).order_by(Task.created_at.desc())
    if project is not None:
        stmt = stmt.where(Task.project == project)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_user_tasks_between(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
) -> list[Task]:
    """User-visible (source != 'system') tasks whose due_at falls in [start_at, end_at)."""
    from sqlalchemy import or_

    stmt = (
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.source != "system",
            Task.due_at.is_not(None),
        )
        .order_by(Task.due_at.asc())
    )
    result = await session.execute(stmt)
    tasks = list(result.scalars().all())
    return [
        task
        for task in tasks
        if task.due_at is not None
        and start_at <= task.due_at < end_at
    ]


async def list_user_tasks_completed_between(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
) -> list[Task]:
    """User-visible tasks marked done within [start_at, end_at).

    晨报需要单独列出「已完成」——它们可能不是今天到期的任务（昨天到期、
    今天才勾掉），只按 due_at 取会漏掉。
    """
    stmt = (
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.source != "system",
            Task.completed_at.is_not(None),
            Task.completed_at >= start_at,
            Task.completed_at < end_at,
        )
        .order_by(Task.completed_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_task_for_user(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Task | None:
    result = await session.execute(
        select(Task).where(Task.id == task_id, Task.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def update_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    body: TaskUpdateRequest,
) -> Task | None:
    task = await get_task_for_user(session, task_id=task_id, user_id=user_id)
    if task is None:
        return None

    if body.title is not None:
        task.title = body.title.strip()
    if body.description is not None:
        task.description = body.description
    if body.status is not None:
        task.status = body.status
    if body.project is not None:
        task.project = body.project.strip() or classify_task_project(task.title)
    if body.assigned_agent is not None:
        task.assigned_agent = body.assigned_agent or None
    if body.priority is not None:
        task.priority = body.priority
    if body.due_at is not None:
        task.due_at = body.due_at
    if body.dependencies is not None:
        task.dependencies = list(body.dependencies)
    if body.plan_id is not None:
        task.plan_id = body.plan_id
    if body.plan_source is not None:
        task.plan_source = body.plan_source
    if body.source is not None:
        task.source = body.source
    if body.source_metadata is not None:
        task.source_metadata = body.source_metadata

    if body.status == TaskStatus.IN_PROGRESS and task.started_at is None:
        task.started_at = datetime.now(timezone.utc)
    if body.status == TaskStatus.DONE and task.completed_at is None:
        task.completed_at = datetime.now(timezone.utc)
    if body.status == TaskStatus.DONE:
        # Deadline reminders are scheduled ahead of due time, so one would still
        # fire after the user has already ticked the task off.
        await cancel_pending_task_reminders(session, task.id)
    task.last_tracked_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(task)
    return task


async def cancel_pending_task_reminders(session: AsyncSession, task_id: uuid.UUID) -> int:
    """Best-effort cancel of a task's still-pending reminders.

    `get_due_reminders` only filters on `status == PENDING` and `remind_at`, so a
    reminder left behind after its task is done or deleted keeps firing — the user
    marks the task complete and is reminded again half an hour later.
    """
    from services.reminder_service import cancel_task_reminders

    try:
        cancelled_count = await cancel_task_reminders(session, task_id)
    except Exception as exc:
        logger.warning("Failed to cancel reminders for task %s: %s", task_id, exc)
        return 0
    if cancelled_count:
        logger.info("Cancelled %d pending reminder(s) for task %s", cancelled_count, task_id)
    return cancelled_count


async def delete_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    task = await get_task_for_user(session, task_id=task_id, user_id=user_id)
    if task is None:
        return False
    # FK is SET NULL, so deleting the task alone would leave the reminder orphaned
    # and still deliverable at its due time.
    await cancel_pending_task_reminders(session, task_id)
    await session.delete(task)
    await session.flush()
    return True
