"""监督代理节点 — 检查并行代理结果并重新规划受阻任务。

LifePilot 针对计划任务运行子代理并收集 `agent_results`。监督代理会检查
这些结果（在可用时也检查数据库中的任务状态）；当某一步失败或被阻塞时，
返回新的 `tasks` 列表，使图可以重新进入并行执行段。当无需再次尝试时，
返回空字典，让执行继续进入合并节点。
"""

import logging
from typing import Any

from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from langgraph.runtime import Runtime

from agents.common import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_SUCCESS,
    render_blocked_step_notice,
    runtime_value,
)
from agents.planning import is_ready, task_layers
from agents.state import AgentState

logger = logging.getLogger(__name__)

MAX_REPLAN_ITERATIONS_FALLBACK = 4


def _task_key(task: dict[str, Any]) -> str:
    return str(task.get("task_key") or task.get("persisted_task_id") or task.get("id") or "")


def _result_status(result: dict[str, Any]) -> str | None:
    """Agent 显式声明的执行状态；缺失时返回 None（走关键词兜底）。"""
    status = result.get("task_status")
    if status in {TASK_STATUS_SUCCESS, TASK_STATUS_PARTIAL, TASK_STATUS_FAILED}:
        return str(status)
    return None


def _result_failed(result: dict[str, Any]) -> bool:
    status = _result_status(result)
    if status is not None:
        # partial 表示主要工作已落地（例如邮件草案已提交待确认），重跑只会重复执行，
        # 因此只有显式 failed 才算需要重规划。
        return status == TASK_STATUS_FAILED

    # 兼容没有带结构化状态的旧结果（例如外部/测试构造的 agent_results）。
    value = result.get("result") or result.get("error")
    if isinstance(value, str):
        lowered = value.lower()
        return any(
            marker in lowered
            for marker in (
                "error",
                "failed",
                "could not",
                "unable to",
                "not connected",
                "not authorized",
                "错误",
                "失败",
                "无法",
                "未连接",
                "未授权",
            )
        )
    return False


def _failure_reason(task: dict[str, Any], results_by_task: dict[str, list[dict[str, Any]]]) -> str:
    """失败步骤的简短原因，供规划器重规划时参考。"""
    for result in _task_results(task, results_by_task):
        if _result_failed(result):
            reason = str(result.get("error") or result.get("result") or "")
            return reason.strip()[:300]
    return ""


def _result_fatal(result: dict[str, Any]) -> bool:
    """这一步是不是"目标本身不存在"的致命失败（查不到该地点/实体不匹配）？

    致命失败重试一万次也是同样的结果。更危险的是：重规划会把"查火星天气"
    改写成"查天气"，默认城市兜底于是拿上海的数据冒充火星——用户看到的就是
    "AI 自己偷偷换了地点还写了邮件"。
    """
    return result.get("fatal_error") is True


def _attempt_filter(task: dict[str, Any], result: dict[str, Any]) -> bool:
    """把代理结果匹配到任务当前的重试轮次。"""
    task_attempt = int(task.get("retry_count") or 0)
    return int(result.get("attempt") or 0) == task_attempt


def _task_results(
    task: dict[str, Any],
    results_by_task: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return [
        result
        for result in results_by_task.get(_task_key(task), [])
        if _attempt_filter(task, result)
    ]


def _task_needs_replan(task: dict[str, Any], results_by_task: dict[str, list[dict[str, Any]]]) -> bool:
    return any(_result_failed(result) for result in _task_results(task, results_by_task))


def _task_completed_this_turn(
    task: dict[str, Any],
    results_by_task: dict[str, list[dict[str, Any]]],
) -> bool:
    return bool(_task_results(task, results_by_task)) and not _task_needs_replan(task, results_by_task)


def _retry_count(task: dict[str, Any]) -> int:
    return int(task.get("retry_count") or 0)


def _blocked_downstream(
    tasks: list[dict[str, Any]],
    failed_keys: set[str],
    results_by_task: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """找出因上游失败而必须跳过执行的下游步骤。

    真实 badcase：问「火星天气」查不到，助手仍然用别处的数据写了一封邮件。
    只要上游没有真实产出，下游就绝不能在缺数据的情况下继续跑——把它们从本轮
    `tasks` 里摘掉（永不进入 `_fan_out`），并给用户一条可决策的提示。
    """
    if not failed_keys:
        return [], []
    by_key = {_task_key(task): task for task in tasks if _task_key(task)}

    blocked: dict[str, dict[str, Any]] = {}
    frontier = set(failed_keys)
    changed = True
    while changed:
        changed = False
        for task in tasks:
            key = _task_key(task)
            if not key or key in blocked or key in failed_keys:
                continue
            deps = [str(dep) for dep in (task.get("depends_on") or [])]
            if any(dep in frontier for dep in deps):
                blocked[key] = task
                frontier.add(key)
                changed = True

    notices: list[str] = []
    for task in blocked.values():
        dep_key = next(
            (
                str(dep)
                for dep in (task.get("depends_on") or [])
                if str(dep) in failed_keys or str(dep) in blocked
            ),
            "",
        )
        upstream = by_key.get(dep_key) or {}
        reason = (
            _failure_reason(upstream, results_by_task)
            if dep_key in failed_keys and upstream
            else "上游步骤没有完成"
        )
        notices.append(
            render_blocked_step_notice(
                str(task.get("intent") or ""),
                str(upstream.get("intent") or dep_key or "上游步骤"),
                reason,
            )
        )
    return list(blocked.values()), notices


async def _persist_task_status_changes(
    state: AgentState,
    db_session: AsyncSession,
    tasks: list[dict[str, Any]],
    results_by_task: dict[str, list[dict[str, Any]]],
) -> None:
    """把本轮完成/受阻的任务状态写回 `tasks` 表。

    成功任务标记为 DONE；受阻任务重置回 NOT_STARTED，等待监督代理重试轮。
    """
    from lib.enums import TaskStatus
    from schemas.task import TaskUpdateRequest
    from services.task_service import update_task

    user_id = state.get("user_id")

    for task in tasks:
        key = _task_key(task)
        persisted_task_id = task.get("persisted_task_id")
        if not key or not persisted_task_id:
            continue

        if _task_completed_this_turn(task, results_by_task):
            target = TaskStatus.DONE
        elif _task_needs_replan(task, results_by_task):
            target = TaskStatus.NOT_STARTED
        else:
            continue

        try:
            await update_task(
                db_session,
                task_id=persisted_task_id,
                user_id=user_id,
                body=TaskUpdateRequest(status=target.value),
            )
            logger.info(
                "Supervisor wrote task %s → %s",
                persisted_task_id,
                target.value,
            )
        except Exception as exc:
            logger.warning(
                "Supervisor task status write-back failed for %s (%s): %s",
                persisted_task_id,
                target.value,
                exc,
            )


async def supervisor_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    """跟踪已完成任务、暴露失败结果，并指示下一个依赖层。"""
    tasks = list(state.get("tasks") or [])
    if not tasks:
        return {}

    plan = state.get("plan") or {}
    current_iteration = int(plan.get("iteration") or 0)
    settings = runtime_value(state, runtime, "settings")
    max_iterations = MAX_REPLAN_ITERATIONS_FALLBACK
    if settings is not None and hasattr(settings, "planner_max_iterations"):
        max_iterations = int(getattr(settings, "planner_max_iterations") or MAX_REPLAN_ITERATIONS_FALLBACK)

    results_by_task: dict[str, list[dict[str, Any]]] = {}
    for result in state.get("agent_results") or []:
        key = str(
            result.get("task_key")
            or result.get("persisted_task_id")
            or result.get("id")
            or ""
        )
        if key:
            results_by_task.setdefault(key, []).append(result)

    completed_ids = set(state.get("completed_task_ids") or [])
    # Only tasks that produced a result for the current retry attempt qualify as complete.
    for task in tasks:
        key = _task_key(task)
        if key and _task_completed_this_turn(task, results_by_task):
            completed_ids.add(key)

    db_session = runtime_value(state, runtime, "db_session")
    if isinstance(db_session, AsyncSession):
        await _persist_task_status_changes(
            state,
            db_session,
            tasks,
            results_by_task,
        )

    any_blocked = any(
        _task_needs_replan(task, results_by_task)
        for task in tasks
    )

    next_ready = [
        task
        for task in tasks
        if is_ready(task, completed_ids)
        and _task_key(task) not in completed_ids
        and not _task_needs_replan(task, results_by_task)
    ]

    # All tasks are done → hand off to merge.
    if len(completed_ids) >= len({_task_key(task) for task in tasks if _task_key(task)}):
        return {
            "completed_task_ids": sorted(completed_ids),
            "supervisor_decision": {"action": "done", "iteration": current_iteration},
        }

    # 致命失败（目标不存在）必须立刻终止：不再重规划，下游一律不执行。
    # 放在重规划分支之前，是因为"再试一次"对这类失败毫无意义，而且重规划
    # 会让城市从 intent 里消失，进而触发默认城市兜底，把别处的数据当成答案。
    fatal_keys = {
        _task_key(task)
        for task in tasks
        if any(_result_fatal(result) for result in _task_results(task, results_by_task))
    }
    if fatal_keys:
        blocked_tasks, blocked_notices = _blocked_downstream(
            tasks, fatal_keys, results_by_task,
        )
        if blocked_tasks:
            blocked_keys = {_task_key(task) for task in blocked_tasks}
            tasks = [task for task in tasks if _task_key(task) not in blocked_keys]
        logger.warning(
            "Supervisor aborted after fatal failure on %s; skipped %d downstream step(s)",
            sorted(fatal_keys),
            len(blocked_tasks),
        )
        return {
            "completed_task_ids": sorted(completed_ids),
            "tasks": tasks,
            "supervisor_decision": {
                "action": "done",
                "iteration": current_iteration,
                "stopped_with_errors": True,
                "fatal": True,
            },
            **({"blocked_step_notices": blocked_notices} if blocked_notices else {}),
        }

    # Reached the retry budget with unresolved failures: keep their error results
    # and hand off to merge instead of looping planner forever.
    if any_blocked and current_iteration >= max_iterations:
        # 重规划机会已用尽：上游注定拿不到数据时，下游不能带着错误数据继续跑。
        # 这里才是唯一该永久摘除下游的时点——还有重规划余地时保留，否则重规划
        # 会把仍在等待的步骤整批丢掉。
        failed_keys = {
            _task_key(task) for task in tasks if _task_needs_replan(task, results_by_task)
        }
        blocked_tasks, blocked_notices = _blocked_downstream(
            tasks, failed_keys, results_by_task,
        )
        if blocked_tasks:
            blocked_keys = {_task_key(task) for task in blocked_tasks}
            tasks = [task for task in tasks if _task_key(task) not in blocked_keys]
            logger.warning(
                "Supervisor skipped %d downstream step(s) after upstream failure: %s",
                len(blocked_tasks),
                [task.get("intent") for task in blocked_tasks],
            )
        logger.warning(
            "Supervisor stopping after %s replan iteration(s) with unresolved tasks",
            max_iterations,
        )
        return {
            "completed_task_ids": sorted(completed_ids),
            "tasks": tasks,
            "supervisor_decision": {
                "action": "done",
                "iteration": current_iteration,
                "stopped_with_errors": True,
            },
            **({"blocked_step_notices": blocked_notices} if blocked_notices else {}),
        }

    # A blocked task exists → force a real re-plan instead of silently retrying
    # the same steps. Failed steps are dropped from `tasks` (only unfinished,
    # non-failed steps survive) and the planner rebuilds the failed part from
    # `replan_context`.
    if any_blocked:
        blocked = [task for task in tasks if _task_needs_replan(task, results_by_task)]
        logger.warning(
            "Supervisor re-planning %d blocked step(s) at iteration %d: %s",
            len(blocked),
            current_iteration,
            [task.get("intent") for task in blocked],
        )

        # Keep every unfinished, non-failed step (not just the ready layer) so a
        # replan cannot silently drop downstream work.
        remaining = [
            task
            for task in tasks
            if _task_key(task) not in completed_ids
            and not _task_needs_replan(task, results_by_task)
        ]
        replan_context = {
            "failed": [
                {**task, "error": _failure_reason(task, results_by_task)}
                for task in blocked
            ],
            "pending": [
                {"intent": task.get("intent"), "route": task.get("route")}
                for task in remaining
            ],
            "completed": [
                {"intent": task.get("intent"), "route": task.get("route")}
                for task in tasks
                if _task_key(task) in completed_ids
            ],
        }
        return {
            "completed_task_ids": sorted(completed_ids),
            "tasks": remaining,
            "force_replan": True,
            "replan_context": replan_context,
            "supervisor_decision": {
                "action": "replan",
                "iteration": current_iteration + 1,
                "blocked_task_ids": [_task_key(task) for task in blocked],
            },
            "plan": {
                **(plan or {}),
                "iteration": current_iteration + 1,
                "last_decision": "replan",
            },
        }

    # Nothing failed (for this attempt): continue with the next dependency layer.
    # Keep the full task list — `_fan_out` already filters on completed/ready.
    # Returning only `next_ready` silently dropped steps whose dependencies were
    # still pending, so any chain longer than two steps lost its downstream work.
    return {
        "completed_task_ids": sorted(completed_ids),
        "tasks": tasks,
        "supervisor_decision": {
            "action": "continue",
            "iteration": current_iteration,
        },
    }