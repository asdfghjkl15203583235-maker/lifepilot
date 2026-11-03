"""Single orchestrator tool: persist a `BackgroundJob` (see services/background_job_service)."""

import json
import logging
import uuid
from typing import Any

from langchain_core.tools import StructuredTool
from sqlalchemy.ext.asyncio import AsyncSession

from schemas.background_jobs import (
    CreateScheduledJobToolArgs,
    DeleteScheduledJobToolArgs,
    StopScheduledJobToolArgs,
)
from services.background_job_service import (
    BackgroundJobInvalidStateError,
    BackgroundJobNotFoundError,
    create_scheduled_job,
    delete_scheduled_job,
    stop_scheduled_job,
)
from services.background_job_serialization import _iso

logger = logging.getLogger(__name__)

CREATE_SCHEDULED_JOB_TOOL_NAME = "create_scheduled_job"

CREATE_SCHEDULED_JOB_TOOL_DESCRIPTION = """\
Create a recurring **background job** the user can view under Background jobs in the app.
Call this ONLY when the user EXPLICITLY asks for a repeating automation with a cycle word —
例如"每天/每周/每月/每日/每周一/每天早上/每天下午" or "daily/weekly/monthly/every <day>"。
NEVER call this to "remember/record" user facts, preferences, habits or memories — the system
automatically saves those to long-term memory; creating a scheduled job for them is a bug.
You must pass a valid 5-field cron expression in the **job's IANA timezone** (minute hour day month weekday),
`schedule_starts_at` and `schedule_ends_at` as ISO-8601 instants in UTC (so the run does not continue without an end;
`schedule_ends_at` is exclusive—no run occurs at or after that instant; confirm dates with the user if unclear),
plus human-readable `schedule_description` and one or more `actions` (UI chips) with action_type: email, calendar, notion, or custom.
Do not call this for one-off requests with no repeat.
"""

# 明确循环指令词：只有命中这些词才允许创建循环定时任务。
_RECURRENCE_TERMS = (
    "每天", "每日", "每周", "每月", "每季度", "每季", "每年",
    "每周一", "每周二", "每周三", "每周四", "每周五", "每周六", "每周日", "每周天",
    "每星期", "每个工作日", "工作日", "周末",
    "daily", "every day", "weekly", "every week", "monthly", "every month",
    "yearly", "every year", "weekdays", "every morning", "every evening",
    "每天早", "每天早上", "每天上午", "每天中午", "每天下午", "每天晚",
    "每早", "每晚",
)
# 记忆/偏好类语义词：命中即判定为"想记住长期信息"，绝不应建定时任务。
_MEMORY_BIAS_TERMS = (
    "偏好", "记忆", "习惯", "喜欢", "喜好", "爱喝", "常喝", "品味",
    "preference", "preferences", "memory", "favorite", "favourite", "habit",
    "likes", "liked", "remembers",
)
# 纯"记录"但缺循环词也拒绝（防 LLM 自创"每天记录"）。
_RECORD_TERMS = ("记录", "记住", "recording", "remember to record")

STOP_SCHEDULED_JOB_TOOL_NAME = "stop_scheduled_job"

STOP_SCHEDULED_JOB_TOOL_DESCRIPTION = """\
Pause a **scheduled background job** so it will not run again until the user resumes it in the app (if that exists) or re-creates it.
Call when the user wants to turn off, pause, or stop an existing recurring automation. Requires the job's UUID.
Does not apply to one-off chat requests.

Raises an error if the user names a job you cannot look up; ask which schedule they mean and use list context if provided.
"""

DELETE_SCHEDULED_JOB_TOOL_NAME = "delete_scheduled_job"

DELETE_SCHEDULED_JOB_TOOL_DESCRIPTION = """\
Permanently **delete** a scheduled background job and its history.
Call when the user wants to remove, cancel forever, or delete a schedule. Requires the job's UUID. This cannot be undone.
"""


def make_create_scheduled_job_tool(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> StructuredTool:
    """Bound tool — each chat request gets a new instance sharing the current DB session."""

    async def _invoke(**kwargs: Any) -> str:
        try:
            payload = CreateScheduledJobToolArgs(**kwargs)
        except Exception as exc:
            return json.dumps({"status": "error", "message": f"Invalid tool arguments: {exc}"})

        combined = f"{payload.title} {payload.instruction} {payload.schedule_description}".lower()
        has_recurrence = any(term in combined for term in _RECURRENCE_TERMS)
        has_memory_bias = any(term in combined for term in _MEMORY_BIAS_TERMS)
        # 记忆/偏好语义：无论是否含循环词都拒绝——"记住偏好"永远不需要定时任务。
        if has_memory_bias:
            return json.dumps(
                {
                    "status": "error",
                    "message": (
                        "这条请求的语义是记录/记住用户偏好或记忆，系统会自动保存到长期记忆，"
                        "不需要创建定时任务。请不要调用 create_scheduled_job。"
                    ),
                },
                ensure_ascii=False,
            )
        # 无明确循环指令词：不允许建循环定时任务（防"记录用户饮品偏好"这类误判自繁殖）。
        if not has_recurrence:
            return json.dumps(
                {
                    "status": "error",
                    "message": (
                        "未识别到明确的循环指令（如“每天/每周/每月/daily/weekly/monthly”）。"
                        "创建定时任务必须包含明确的循环周期；一次性提醒请用 create_reminder，"
                        "普通信息会自动保存到长期记忆。"
                    ),
                },
                ensure_ascii=False,
            )
        # 仅"记录/记住"而无真实循环执行动作（如"每天记录X"而 X 是用户信息）→ 拒绝。
        if any(term in combined for term in _RECORD_TERMS) and not any(
            term in combined for term in _RECURRENCE_TERMS
        ):
            return json.dumps(
                {
                    "status": "error",
                    "message": (
                        "这条请求没有明确的循环周期，不能创建定时任务。"
                        "如需记住信息请直接交给长期记忆，无需定时任务。"
                    ),
                },
                ensure_ascii=False,
            )
        try:
            job = await create_scheduled_job(
                session,
                user_id,
                title=payload.title,
                instruction=payload.instruction,
                schedule_description=payload.schedule_description,
                timezone_name=payload.timezone,
                schedule_starts_at=payload.schedule_starts_at,
                schedule_ends_at=payload.schedule_ends_at,
                cron_expression=payload.cron_expression,
                actions=[dict(x) for x in payload.actions],
            )
        except ValueError as exc:
            logger.info("create_scheduled_job validation failed: %s", exc)
            return json.dumps({"status": "error", "message": str(exc)})

        return json.dumps(
            {
                "status": "created",
                "job_id": str(job.id),
                "title": job.title,
                "next_run_at": _iso(job.next_run_at),
            },
        )

    return StructuredTool.from_function(
        name=CREATE_SCHEDULED_JOB_TOOL_NAME,
        description=CREATE_SCHEDULED_JOB_TOOL_DESCRIPTION,
        coroutine=_invoke,
        args_schema=CreateScheduledJobToolArgs,
        infer_schema=False,
    )


def make_stop_scheduled_job_tool(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> StructuredTool:
    async def _invoke(**kwargs: Any) -> str:
        try:
            payload = StopScheduledJobToolArgs(**kwargs)
        except Exception as exc:
            return json.dumps({"status": "error", "message": f"Invalid tool arguments: {exc}"})
        try:
            job = await stop_scheduled_job(session, user_id, payload.job_id)
        except BackgroundJobNotFoundError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        except BackgroundJobInvalidStateError as exc:
            return json.dumps({"status": "error", "message": str(exc)})

        return json.dumps(
            {
                "status": "stopped",
                "job_id": str(job.id),
                "title": job.title,
            },
        )

    return StructuredTool.from_function(
        name=STOP_SCHEDULED_JOB_TOOL_NAME,
        description=STOP_SCHEDULED_JOB_TOOL_DESCRIPTION,
        coroutine=_invoke,
        args_schema=StopScheduledJobToolArgs,
        infer_schema=False,
    )


def make_delete_scheduled_job_tool(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> StructuredTool:
    async def _invoke(**kwargs: Any) -> str:
        try:
            payload = DeleteScheduledJobToolArgs(**kwargs)
        except Exception as exc:
            return json.dumps({"status": "error", "message": f"Invalid tool arguments: {exc}"})
        try:
            await delete_scheduled_job(session, user_id, payload.job_id)
        except BackgroundJobNotFoundError as exc:
            return json.dumps({"status": "error", "message": str(exc)})

        return json.dumps(
            {
                "status": "deleted",
                "job_id": str(payload.job_id),
            },
        )

    return StructuredTool.from_function(
        name=DELETE_SCHEDULED_JOB_TOOL_NAME,
        description=DELETE_SCHEDULED_JOB_TOOL_DESCRIPTION,
        coroutine=_invoke,
        args_schema=DeleteScheduledJobToolArgs,
        infer_schema=False,
    )
