"""日历代理节点 — 基于本地日历（user_calendar_events + 带 due_at 的任务）的工具调用循环。

历史问题：本节点曾只绑定 Google Calendar 的 OAuth 工具，没有 Google token 时
直接返回"本地日历尚未连接"。但前端日历页展示的是本地 ICS 事件与带截止时间
任务的合并视图，用户明明看得到日程，代理却坚称没连接；混合意图里"开会"
因此必然失败。现在统一走本地库，不再需要 Google 授权。
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime

from agents.common import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_SUCCESS,
    mark_task_in_progress_if_persisted,
    result_meta,
    runtime_value,
    step_result_update,
    task_status_meta,
    tz_name_of,
)
from agents.state import AgentState
from agents.stream_collector import astream_invoke
from agents.tools.local_calendar_tools import make_local_calendar_tools

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
_MAX_TOOL_OUTPUT_CHARS = 30_000
# 工具抛异常时拼进 ToolMessage 的前缀；用于判定"这轮确实出过错"。
_TOOL_ERROR_PREFIX = "工具错误："

CALENDAR_SYSTEM_PROMPT = """\
你是一位可以完整读写用户**本地日历**的日历助手。
本地日历由两部分组成：用户的日程事件（events）与带截止时间的任务（tasks），
两者都会显示在应用的日历页上。工具已经直接连好本地数据库，**不需要任何
Google 账号或外部授权**；绝不要说"日历未连接""需要先授权 Google 日历"。

请使用可用工具完整完成用户的请求。
- 查询日程时：使用 list_events，传入 ISO-8601 时间范围；查询整天时设置
  time_min=当天 00:00:00、time_max=当天 23:59:59
- 新建日程时：使用 create_event，start/end 传用户本地时区的墙壁时间
  （例如 2026-10-30T08:00:00）；未给结束时间时默认 1 小时
- 新建带截止时间的待办时：使用 create_task，due_at 传用户本地时区的墙壁时间
- 写入成功后必须核对并回报标题、开始时间和结束时间，只能报告工具返回的内容
- 遇到"今天""明天""下周一"等相对日期时：基于今天的日期进行解析
- 如果本轮输入附带"上游步骤…的结果"，那是前置步骤已查到的真实数据，
  请直接基于这些内容安排日程，不要重新查询或编造
- 始终以清晰、友好、易于阅读的方式呈现结果
{format_instruction}
今天：{today}（用户时区 {tz_name}）
"""

MARKDOWN_INSTRUCTION = "请用简洁的 Markdown 格式回复。"
PLAIN_TEXT_INSTRUCTION = (
    "用户正在通过语音播报收听回复。请只使用纯文本，"
    "不要使用 Markdown、项目符号星号或标题，使用简短自然的句子。"
)


async def calendar_agent_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict:
    """运行工具调用循环来完成日历任务。"""
    task = state.get("current_task") or {}
    intent = task.get("intent", "")
    meta = result_meta(task)
    await mark_task_in_progress_if_persisted(state, task, runtime)

    settings = runtime_value(state, runtime, "settings")
    db_session = runtime_value(state, runtime, "db_session")
    user_id = state.get("user_id")
    tz_name = tz_name_of(settings)

    # 本地日历依赖数据库；没有库时如实说明不可用（而不是谎称"未连接日历"）。
    if not isinstance(db_session, AsyncSession) or not isinstance(user_id, uuid.UUID):
        return {
            "agent_results": [{
                "route": "calendar",
                "intent": intent,
                "result": "本地日历暂时不可用（数据库未就绪），请稍后重试。",
                **meta,
                **task_status_meta(TASK_STATUS_FAILED, "本地日历数据库不可用"),
            }]
        }

    is_voice = state.get("is_voice_request", False)
    today = datetime.now(ZoneInfo(tz_name)).strftime("%A, %B %d, %Y %I:%M %p")
    fmt = PLAIN_TEXT_INSTRUCTION if is_voice else MARKDOWN_INSTRUCTION

    system_text = CALENDAR_SYSTEM_PROMPT.format(
        format_instruction=fmt,
        today=today,
        tz_name=tz_name,
    )
    upstream_context = str(state.get("upstream_context") or "")
    if upstream_context:
        system_text = f"{system_text}\n{upstream_context}\n"
    system = SystemMessage(content=system_text)

    tools = make_local_calendar_tools(db_session, user_id, tz_name)
    agent = llm.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}

    conversation = list(state.get("messages", []))
    work_messages = [system, *conversation, HumanMessage(content=intent)]

    tool_errors: list[str] = []
    # 写库成功回执：只有拿到 created 回执才允许声称"已添加日程/任务"。
    created_receipts: list[dict] = []

    for round_num in range(MAX_TOOL_ROUNDS):
        response = await astream_invoke(agent, work_messages)
        work_messages.append(response)

        if not response.tool_calls:
            result = response.content if isinstance(response.content, str) else ""
            logger.info("Calendar agent completed in %d round(s)", round_num + 1)
            # 确定性闸门：声称已写入但没有 created 回执 → 判定失败，避免幻觉。
            if _claims_write(result) and not created_receipts:
                logger.warning("Calendar agent claimed a write without receipt: %.120s", result)
                return {
                    "agent_results": [{
                        "route": "calendar", "intent": intent, "result": result,
                        **meta,
                        **task_status_meta(TASK_STATUS_FAILED, "声称已写入日程但没有成功回执"),
                    }]
                }
            return {
                "agent_results": [{
                    "route": "calendar", "intent": intent, "result": result,
                    **meta, **task_status_meta(TASK_STATUS_SUCCESS),
                }],
                **step_result_update(task, result, TASK_STATUS_SUCCESS),
            }

        tool_results = await asyncio.gather(*[
            _invoke_tool(tools_by_name, call) for call in response.tool_calls
        ])
        for tool_message in tool_results:
            content = tool_message.content
            if content.startswith(_TOOL_ERROR_PREFIX):
                tool_errors.append(content)
                continue
            receipt = _parse_receipt(content)
            if receipt and receipt.get("status") == "created":
                created_receipts.append(receipt)
            elif receipt and receipt.get("status") == "error":
                tool_errors.append(str(receipt.get("detail") or "工具返回错误"))
        work_messages.extend(tool_results)

    last = work_messages[-1]
    result = last.content if isinstance(last.content, str) else "日历请求已完成。"
    logger.warning("Calendar agent hit max tool rounds (%d)", MAX_TOOL_ROUNDS)
    status = TASK_STATUS_FAILED if tool_errors else TASK_STATUS_PARTIAL
    return {
        "agent_results": [{
            "route": "calendar", "intent": intent, "result": result,
            **meta,
            **task_status_meta(
                status,
                tool_errors[0][:300] if tool_errors else "工具轮次耗尽仍未完成",
            ),
        }],
        **step_result_update(task, result, status),
    }


_WRITE_CLAIM_MARKERS = (
    "已添加", "已创建", "已安排", "已加入", "已写入", "已记录", "添加成功", "创建成功",
    "已经帮你", "已为你", "added", "created", "scheduled",
)


def _claims_write(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker.lower() in lowered for marker in _WRITE_CLAIM_MARKERS)


def _parse_receipt(content: str) -> dict | None:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _invoke_tool(tools_by_name: dict, call: dict) -> ToolMessage:
    name = call.get("name", "")
    call_id = call.get("id", "")
    args = call.get("args") or {}
    target = tools_by_name.get(name)
    try:
        output = await target.ainvoke(args) if target else f"未知工具：{name}"
    except Exception as exc:
        logger.error("Calendar tool %s failed: %s", name, exc)
        output = f"{_TOOL_ERROR_PREFIX}{exc}"
    content = output if isinstance(output, str) else str(output)
    if len(content) > _MAX_TOOL_OUTPUT_CHARS:
        logger.warning("Calendar tool %s output truncated: %d → %d chars", name, len(content), _MAX_TOOL_OUTPUT_CHARS)
        content = content[:_MAX_TOOL_OUTPUT_CHARS] + "\n...[输出已截断以适配上下文窗口]"
    return ToolMessage(content=content, tool_call_id=call_id)
