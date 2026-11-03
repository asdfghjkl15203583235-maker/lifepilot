"""任务表扫描与规划 — 汇总用户任务，识别冲突、阻塞和下一步行动。

LifePilot 的规划器会把计划步骤写入全局 `tasks` 表，但这个表也需要
一个“扫描规划”视图：把分散的待办、依赖、优先级和截止时间聚合成
可直接给用户看的行动建议。
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from models.task import Task
from services.task_service import list_tasks_for_user
from services.task_source_classifier import classify_task_source

UPCOMING_WINDOW = timedelta(hours=24)
STALE_WINDOW = timedelta(hours=24)


def _task_id(task: Task) -> str:
    return str(task.id)


def _task_key(task: Task) -> str:
    return str(task.id)


def _parse_dependency_id(dep: Any) -> str | None:
    if isinstance(dep, str):
        return dep
    if isinstance(dep, dict):
        for key in ("id", "step", "task_id"):
            value = dep.get(key)
            if value:
                return str(value)
    return None


def _done_ids(tasks: list[Task]) -> set[str]:
    return {_task_id(task) for task in tasks if task.status.value == "done"}


def _is_blocked(task: Task, done_ids: set[str]) -> bool:
    for dep in task.dependencies or []:
        dep_id = _parse_dependency_id(dep)
        if dep_id and dep_id not in done_ids:
            return True
    return False


def analyze_tasks(tasks: list[Task], now: datetime) -> dict[str, Any]:
    """把任务表扫描成一份可执行的规划摘要。"""
    done_ids = _done_ids(tasks)

    overdue: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    ready: list[dict[str, Any]] = []
    stalled: list[dict[str, Any]] = []
    recommendations: list[str] = []

    for task in tasks:
        # 系统内部任务（记录偏好、更新记忆）不进"任务概览"，只进实时动态/长期记忆。
        # 对分类器上线前落库的旧数据，用同一规则兜底判定，避免残留 system 混入概览。
        if task.source == "system" or classify_task_source(task.title) == "system":
            continue
        status = task.status.value
        due_at = task.due_at
        due_at_utc = due_at if due_at and due_at.tzinfo else None
        if due_at_utc:
            due_at_utc = due_at_utc.astimezone(timezone.utc)

        item = {
            "id": _task_id(task),
            "title": task.title,
            "status": status,
            "priority": task.priority.value,
            "due_at": due_at.isoformat() if due_at else None,
            "assigned_agent": task.assigned_agent,
            "plan_id": task.plan_id,
            "source": task.source or "user",
        }

        if status != "done" and _is_blocked(task, done_ids):
            blocked.append(item)
            recommendations.append(
                f"任务「{task.title}」依赖任务尚未完成，当前无法推进。"
            )
            continue

        if status != "done" and due_at_utc and due_at_utc < now:
            overdue.append(item)
            recommendations.append(f"任务「{task.title}」已逾期，尽快处理。")
            continue

        if status != "done" and due_at_utc and due_at_utc <= now + UPCOMING_WINDOW:
            upcoming.append(item)
            recommendations.append(f"任务「{task.title}」临近截止，请预留时间。")
            continue

        if status == "in_progress":
            last_tracked = task.last_tracked_at
            if last_tracked and last_tracked.tzinfo and now - last_tracked.astimezone(timezone.utc) > STALE_WINDOW:
                stalled.append(item)
                recommendations.append(
                    f"任务「{task.title}」超过 24 小时没有进度更新，建议检查是否停滞。"
                )
            continue

        if status == "not_started" and not _is_blocked(task, done_ids):
            ready.append(item)
            recommendations.append(
                f"任务「{task.title}」已就绪，可立即执行或并行安排。"
            )

    # 冲突：同优先级 + 同 due_at 的活跃任务
    conflict_map: dict[str, list[dict[str, Any]]] = {}
    for item in overdue + upcoming:
        key = f"{item.get('due_at')}|{item.get('priority')}"
        conflict_map.setdefault(key, []).append(item)
    conflicts = [group for group in conflict_map.values() if len(group) > 1]
    for group in conflicts:
        recommendations.append(
            "多任务撞期："
            + "、".join(t.get("title", "") for t in group[:4])
            + " 挤在同一时间窗，建议调整顺序或拆分。"
        )

    return {
        "scanned_at": now.isoformat(),
        "summary": {
            "total": len(tasks),
            "not_started": sum(1 for t in tasks if t.status.value == "not_started"),
            "in_progress": sum(1 for t in tasks if t.status.value == "in_progress"),
            "done": sum(1 for t in tasks if t.status.value == "done"),
            "overdue": len(overdue),
            "upcoming": len(upcoming),
            "ready": len(ready),
            "blocked": len(blocked),
        },
        "overdue": overdue,
        "upcoming": upcoming,
        "blocked": blocked,
        "ready": ready,
        "stalled": stalled,
        "conflicts": conflicts,
        "recommendations": recommendations[:20],
    }


async def scan_user_task_plan(
    session: Any,
    *,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回某位用户的扫描规划结果。"""
    tasks = await list_tasks_for_user(session, user_id=user_id)
    return analyze_tasks(tasks, now or datetime.now(timezone.utc))
