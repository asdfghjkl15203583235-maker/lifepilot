"""本地日历工具 —— 读写 `user_calendar_events` 与带 due_at 的用户任务。

日历代理过去只绑定 Google Calendar 的 OAuth 工具，没有 Google token 时直接
回一句"本地日历尚未连接"。但前端日历页（`GET /calendar/events`）展示的是
"本地 ICS 事件 + 带截止时间的任务"的合并视图，与 Google 无关。于是用户明明
能在日历里看到事件，代理却坚称没连接 —— 这是混合意图场景下"开会失败、报销成功"
的直接原因。

这里的工具以 (db_session, user_id) 闭包，直接操作本地库：
  - 查询：合并本地事件与 due_at 任务，返回统一时间线；
  - 新建：写 `user_calendar_events`（uid 以 `local-` 前缀区分来源）；
  - 新建待办：写 `tasks`（会出现在日历与任务看板）。
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import TaskPriority, TaskStatus
from models.task import Task
from models.user_calendar_event import UserCalendarEvent
from schemas.task import TaskCreateRequest
from services.local_calendar_service import list_local_events_between
from services.task_service import create_task_for_user

logger = logging.getLogger(__name__)

_LOCAL_UID_PREFIX = "local-"
_DEFAULT_EVENT_MINUTES = 60
# 查询窗口上限：防止 LLM 传入离谱区间把整库拉进上下文。
_MAX_QUERY_DAYS = 400


def _parse_iso(value: str, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC"))


def _fmt_local(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


async def _list_tasks_between(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
) -> list[Task]:
    result = await session.execute(
        select(Task).where(
            Task.user_id == user_id,
            Task.due_at.is_not(None),
            Task.due_at >= start_at,
            Task.due_at < end_at,
            Task.source != "system",
        ).order_by(Task.due_at.asc())
    )
    return list(result.scalars().all())


def make_local_calendar_tools(
    session: AsyncSession,
    user_id: uuid.UUID,
    tz_name: str = "UTC",
) -> list:
    """返回绑定本地库的日历工具（查询 / 新建事件 / 新建待办）。"""
    tz = ZoneInfo(tz_name or "UTC")

    @tool
    async def list_events(
        time_min: str = "",
        time_max: str = "",
        query: str = "",
        max_results: int = 50,
    ) -> str:
        """List the user's local calendar events and tasks with a due time.

        time_min / time_max use ISO-8601 (e.g. 2026-10-30T00:00:00+08:00).
        For a whole day use 00:00:00 → 23:59:59. Leave empty to list the next
        7 days. `query` filters by title substring.
        """
        now = datetime.now(tz)
        start = _parse_iso(time_min, tz) or now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = _parse_iso(time_max, tz) or (start.astimezone(tz) + timedelta(days=7))
        if end <= start:
            end = start + timedelta(days=1)
        if (end - start) > timedelta(days=_MAX_QUERY_DAYS):
            end = start + timedelta(days=_MAX_QUERY_DAYS)

        events = await list_local_events_between(
            session, user_id=user_id, start_at=start, end_at=end
        )
        tasks = await _list_tasks_between(
            session, user_id=user_id, start_at=start, end_at=end
        )

        needle = (query or "").strip().lower()
        items: list[dict[str, Any]] = []
        for event in events:
            if needle and needle not in (event.title or "").lower():
                continue
            items.append({
                "kind": "event",
                "id": str(event.id),
                "title": event.title,
                "start": event.starts_at.isoformat(),
                "end": event.ends_at.isoformat() if event.ends_at else None,
                "all_day": event.all_day,
                "location": event.location,
                "display_start": _fmt_local(event.starts_at, tz),
            })
        for task in tasks:
            if needle and needle not in (task.title or "").lower():
                continue
            items.append({
                "kind": "task",
                "id": str(task.id),
                "title": task.title,
                "start": task.due_at.isoformat() if task.due_at else None,
                "end": None,
                "all_day": False,
                "status": task.status.value if task.status else None,
                "display_start": _fmt_local(task.due_at, tz) if task.due_at else None,
            })

        items.sort(key=lambda item: item.get("start") or "")
        return json.dumps(
            {
                "timezone": tz_name,
                "window_start": _fmt_local(start, tz),
                "window_end": _fmt_local(end, tz),
                "count": len(items[: max(1, max_results)]),
                "items": items[: max(1, max_results)],
            },
            ensure_ascii=False,
            default=str,
        )

    @tool
    async def create_event(
        summary: str,
        start: str,
        end: str = "",
        description: str = "",
        location: str = "",
    ) -> str:
        """Create a local calendar event.

        `start` / `end` are ISO-8601 wall-clock times in the user's timezone
        (e.g. 2026-10-30T08:00:00). When `end` is omitted the event lasts 1 hour.
        """
        start_at = _parse_iso(start, tz)
        if start_at is None:
            return json.dumps(
                {"status": "error", "detail": f"无法解析开始时间：{start}"},
                ensure_ascii=False,
            )
        end_at = _parse_iso(end, tz) if end else start_at + timedelta(minutes=_DEFAULT_EVENT_MINUTES)
        if end_at is not None and end_at <= start_at:
            end_at = start_at + timedelta(minutes=_DEFAULT_EVENT_MINUTES)

        title = (summary or "").strip() or "未命名日程"
        event = UserCalendarEvent(
            user_id=user_id,
            uid=f"{_LOCAL_UID_PREFIX}{uuid.uuid4()}",
            source_file=None,
            title=title[:500],
            description=(description or "")[:2000] or None,
            location=(location or "")[:500] or None,
            starts_at=start_at,
            ends_at=end_at,
            all_day=False,
        )
        session.add(event)
        await session.flush()

        return json.dumps(
            {
                "status": "created",
                "event_id": str(event.id),
                "title": title,
                "start": _fmt_local(start_at, tz),
                "end": _fmt_local(end_at, tz) if end_at else None,
                "timezone": tz_name,
            },
            ensure_ascii=False,
        )

    @tool
    async def create_task(
        title: str,
        due_at: str,
        description: str = "",
    ) -> str:
        """Create a local task with a due time (it also shows up on the calendar).

        `due_at` is ISO-8601 wall-clock time in the user's timezone.
        """
        due = _parse_iso(due_at, tz)
        if due is None:
            return json.dumps(
                {"status": "error", "detail": f"无法解析截止时间：{due_at}"},
                ensure_ascii=False,
            )
        clean_title = (title or "").strip()
        if not clean_title:
            return json.dumps({"status": "error", "detail": "标题不能为空"}, ensure_ascii=False)

        row = await create_task_for_user(
            session,
            user_id=user_id,
            body=TaskCreateRequest(
                title=clean_title[:500],
                description=(description or "")[:2000] or None,
                status=TaskStatus.NOT_STARTED,
                assigned_agent="calendar",
                priority=TaskPriority.MEDIUM,
                due_at=due,
            ),
        )
        if row is None:
            return json.dumps({"status": "error", "detail": "用户不存在"}, ensure_ascii=False)
        return json.dumps(
            {
                "status": "created",
                "task_id": str(row.id),
                "title": row.title,
                "due_at": _fmt_local(due, tz),
                "timezone": tz_name,
            },
            ensure_ascii=False,
        )

    return [list_events, create_event, create_task]
