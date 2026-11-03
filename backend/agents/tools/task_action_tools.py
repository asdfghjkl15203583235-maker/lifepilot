"""High-risk task mutation tools for the orchestrator `direct_reply` agent.

`propose_task_update` / `propose_task_delete` never mutate a task directly.
They freeze a structured payload into a PendingAction and return a `pending`
receipt; only a user-confirmed /approve call executes the mutation. A read-only
`list_user_tasks` lets the agent locate the target task before proposing.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import PendingActionType, TaskStatus
from schemas.task import TaskCreateRequest
from services.pending_action_service import create_pending_action
from services.task_service import create_task_for_user, get_task_for_user, list_tasks_for_user

logger = logging.getLogger(__name__)

PROPOSE_TASK_UPDATE_TOOL_NAME = "propose_task_update"
PROPOSE_TASK_DELETE_TOOL_NAME = "propose_task_delete"
LIST_USER_TASKS_TOOL_NAME = "list_user_tasks"
CREATE_TASK_TOOL_NAME = "create_task"
CREATE_TASKS_TOOL_NAME = "create_tasks"

_MAX_TASKS_INLIST = 20
# 一份行程通常 2~6 个节点；留出余量但别让模型一次塞几十条。
_MAX_BATCH_TASKS = 20


def _task_summary(task: Any) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "title": task.title,
        "status": getattr(task.status, "value", task.status),
        "priority": getattr(task.priority, "value", task.priority),
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "project": task.project,
    }


class ListTasksArgs(BaseModel):
    project: str | None = Field(default=None, description="Optional project filter.")
    limit: int = Field(default=20, ge=1, le=_MAX_TASKS_INLIST, description="Max rows to return.")


class TaskUpdateArgs(BaseModel):
    task_id: str = Field(description="The target task's uuid (from list_user_tasks).")
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=10000)
    status: str | None = Field(default=None, description="not_started | in_progress | done")
    priority: str | None = Field(default=None, description="low | medium | high | urgent")
    due_at: str | None = Field(
        default=None,
        description="ISO-8601 due instant (e.g. 2026-10-26T09:00:00). Null keeps current.",
    )


class TaskDeleteArgs(BaseModel):
    task_id: str = Field(description="The target task's uuid (from list_user_tasks).")


class CreateTaskArgs(BaseModel):
    title: str = Field(max_length=500, description="Short task title.")
    due_at: str | None = Field(
        default=None,
        description="Optional ISO-8601 due instant resolved to the user's local timezone.",
    )
    description: str | None = Field(default=None, max_length=2000)


class BatchTaskItem(BaseModel):
    title: str = Field(max_length=500, description="单条任务标题，如「天安门广场」。")
    due_at: str | None = Field(
        default=None,
        description="ISO-8601 时间（用户本地时区已解析为绝对时间），如 2026-11-07T09:00:00+08:00。",
    )
    description: str | None = Field(default=None, max_length=2000)


class CreateTasksArgs(BaseModel):
    tasks: list[BatchTaskItem] = Field(
        min_length=1,
        max_length=_MAX_BATCH_TASKS,
        description="要一次性创建的任务列表（行程的每一天/每个节点各一条）。",
    )


def make_task_action_tools(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Any = None,
) -> list[StructuredTool]:
    """Bound tools — one set per chat request, share the request DB session."""

    async def _list_tasks(project: str | None = None, limit: int = 20) -> str:
        tasks = await list_tasks_for_user(session, user_id=user_id, project=project)
        rows = [_task_summary(t) for t in tasks[: int(limit)]]
        return json.dumps({"status": "ok", "tasks": rows, "count": len(rows)}, ensure_ascii=False)

    async def _propose_update(**kwargs: Any) -> str:
        from datetime import timezone

        task_id_str = str(kwargs.get("task_id") or "").strip()
        try:
            task_id = uuid.UUID(task_id_str)
        except (ValueError, TypeError):
            return json.dumps({"status": "error", "message": f"task_id 无效：{task_id_str}"}, ensure_ascii=False)
        task = await get_task_for_user(session, task_id=task_id, user_id=user_id)
        if task is None:
            return json.dumps(
                {"status": "error", "message": "未找到该任务，请先用 list_user_tasks 确认任务 id。"},
                ensure_ascii=False,
            )

        changes: dict[str, Any] = {}
        if kwargs.get("title"):
            changes["title"] = str(kwargs["title"]).strip()
        if kwargs.get("description"):
            changes["description"] = str(kwargs["description"]).strip()
        if kwargs.get("status"):
            changes["status"] = str(kwargs["status"]).strip()
        if kwargs.get("priority"):
            changes["priority"] = str(kwargs["priority"]).strip()
        due_raw = kwargs.get("due_at")
        if due_raw:
            try:
                due = datetime.fromisoformat(str(due_raw).replace("Z", "+00:00"))
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                changes["due_at"] = due.isoformat()
            except (ValueError, TypeError):
                return json.dumps(
                    {"status": "error", "message": f"due_at 格式无效：{due_raw}"},
                    ensure_ascii=False,
                )
        if not changes:
            return json.dumps({"status": "error", "message": "未提供任何变更。"}, ensure_ascii=False)

        change_lines = "\n".join(f"- {k}: {v}" for k, v in changes.items())
        row = await create_pending_action(
            session,
            user_id=user_id,
            action_type=PendingActionType.TASK_UPDATE,
            title=f"更新任务「{task.title[:80]}」",
            summary=f"任务：{task.title}\n\n计划变更：\n{change_lines}",
            payload={
                "task_id": str(task_id),
                "task_title": task.title,
                "changes": changes,
            },
        )
        return json.dumps(
            {
                "status": "pending",
                "action_id": str(row.id),
                "action_type": "task_update",
                "task_id": str(task_id),
            },
            ensure_ascii=False,
        )

    async def _propose_delete(**kwargs: Any) -> str:
        task_id_str = str(kwargs.get("task_id") or "").strip()
        try:
            task_id = uuid.UUID(task_id_str)
        except (ValueError, TypeError):
            return json.dumps({"status": "error", "message": f"task_id 无效：{task_id_str}"}, ensure_ascii=False)
        task = await get_task_for_user(session, task_id=task_id, user_id=user_id)
        if task is None:
            return json.dumps(
                {"status": "error", "message": "未找到该任务，请先用 list_user_tasks 确认任务 id。"},
                ensure_ascii=False,
            )
        row = await create_pending_action(
            session,
            user_id=user_id,
            action_type=PendingActionType.TASK_DELETE,
            title=f"删除任务「{task.title[:80]}」",
            summary=f"将永久删除任务：{task.title}",
            payload={"task_id": str(task_id), "task_title": task.title},
        )
        return json.dumps(
            {
                "status": "pending",
                "action_id": str(row.id),
                "action_type": "task_delete",
                "task_id": str(task_id),
            },
            ensure_ascii=False,
        )

    def _resolve_due_at(due_at: str | None) -> tuple[str | None, str | None]:
        """Parse an ISO instant; naive values use the user's local timezone."""
        if not due_at:
            return None, None
        try:
            due = datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None, f"due_at 格式无效：{due_at}"
        if due.tzinfo is None:
            from zoneinfo import ZoneInfo

            tz_name = getattr(settings, "planner_timezone", None) or "UTC"
            due = due.replace(tzinfo=ZoneInfo(tz_name))
        return due.isoformat(), None

    async def _create_one(
        title: str,
        due_at: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Create one task and return a receipt dict (never raises)."""
        clean_title = (title or "").strip()
        if not clean_title:
            return {"status": "error", "message": "标题不能为空。"}
        due_iso, error = _resolve_due_at(due_at)
        if error:
            return {"status": "error", "message": error, "title": clean_title}
        row = await create_task_for_user(
            session,
            user_id=user_id,
            body=TaskCreateRequest(
                title=clean_title[:500],
                description=(description or "")[:2000] or None,
                status=TaskStatus.NOT_STARTED,
                due_at=datetime.fromisoformat(due_iso) if due_iso else None,
            ),
        )
        if row is None:
            return {"status": "error", "message": "用户不存在", "title": clean_title}
        return {
            "status": "created",
            "task_id": str(row.id),
            "title": row.title,
            "due_at": due_iso,
        }

    async def _create_task(
        title: str,
        due_at: str | None = None,
        description: str | None = None,
    ) -> str:
        """创建本地待办。用户直接说"建个任务/加到待办"时用，无需确认草案——
        创建是低风险动作，可直接落库并回执。"""
        return json.dumps(
            await _create_one(title, due_at, description), ensure_ascii=False
        )

    def _item_value(item: Any, key: str) -> Any:
        if isinstance(item, dict):
            return item.get(key)
        return getattr(item, key, None)

    async def _create_tasks(tasks: list[Any]) -> str:
        """批量创建（行程一次落库多条），单条失败不影响其它条目。"""
        created: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for item in list(tasks or [])[:_MAX_BATCH_TASKS]:
            receipt = await _create_one(
                _item_value(item, "title") or "",
                _item_value(item, "due_at"),
                _item_value(item, "description"),
            )
            if receipt.get("status") == "created":
                created.append(receipt)
            else:
                failed.append(receipt)
        if created and not failed:
            status = "created"
        elif created:
            status = "partial"
        else:
            status = "error"
        return json.dumps(
            {
                "status": status,
                "count": len(created),
                "task_ids": [row["task_id"] for row in created],
                "created": created,
                "failed": failed,
            },
            ensure_ascii=False,
        )

    return [
        StructuredTool.from_function(
            name=LIST_USER_TASKS_TOOL_NAME,
            description=(
                "List the user's tasks (read-only). Returns id/title/status/priority/due_at. "
                "Call this FIRST to locate the exact task id before proposing an update or delete."
            ),
            coroutine=_list_tasks,
            args_schema=ListTasksArgs,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name=CREATE_TASK_TOOL_NAME,
            description=(
                "Create a NEW task/todo directly (low risk). Use when the user asks to add a task "
                "or todo item (e.g. '下周出差', '帮我建个任务'). Provide a concise `title` and, when "
                "the user gave a time, an ISO-8601 `due_at` in the user's local timezone. This one "
                "does NOT need confirmation."
            ),
            coroutine=_create_task,
            args_schema=CreateTaskArgs,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name=CREATE_TASKS_TOOL_NAME,
            description=(
                "一次性创建多条任务（行程/日程一次性落库时用）。当用户确认「把这两天的行程"
                "记成待办」时，把所有节点（如 周六 09:00 天安门、周六 11:00 故宫）放进 "
                "`tasks` 数组，每个节点给 title + ISO-8601 `due_at`。低风险，无需确认；"
                "同名的重复条目会被幂等去重。"
            ),
            coroutine=_create_tasks,
            args_schema=CreateTasksArgs,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name=PROPOSE_TASK_UPDATE_TOOL_NAME,
            description=(
                "Propose updating a task (title/status/priority/due_at). This does NOT apply the "
                "change — it creates a confirmation draft the user must approve. Mark done uses "
                "status='done'."
            ),
            coroutine=_propose_update,
            args_schema=TaskUpdateArgs,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            name=PROPOSE_TASK_DELETE_TOOL_NAME,
            description=(
                "Propose deleting a task. This does NOT delete it — it creates a confirmation "
                "draft the user must approve."
            ),
            coroutine=_propose_delete,
            args_schema=TaskDeleteArgs,
            infer_schema=False,
        ),
    ]