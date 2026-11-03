"""Notion 代理节点 — 基于 Notion 工具的工具调用循环。"""

import asyncio
import logging
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
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
from agents.tools.notion_agent_tools import make_notion_tools

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10  # Notion may need more rounds for multi-step operations
_TOOL_ERROR_PREFIX = "工具错误："

NOTION_SYSTEM_PROMPT = """\
你是一位可以完整访问用户 Notion 工作区的 Notion 助手。
请使用可用工具完整完成用户的请求。

重要规则：
- 绝不能猜测或编造页面 ID、数据库 ID 或区块 ID
- 如果需要按名称操作某个页面或数据库，一定要先使用 search_workspace 找到真实 ID
- 只能使用实际工具结果中出现的 ID
- 多步操作（例如先搜索数据库再查询数据库）要按顺序多次调用工具

处理未知 ID 的流程：
1. 使用 search_workspace 按名称查找项目
2. 从结果中提取真实 ID
3. 再使用该 ID 调用相应工具

始终以清晰、友好、易于阅读的方式呈现结果。
不要编造信息，只报告工具返回的内容。
如果本轮输入附带"上游步骤…的结果"，那是前置步骤已查到的真实数据，
请直接基于这些内容继续操作（例如写入页面正文），不要重新搜索或编造。
{format_instruction}
今天：{today}
"""

MARKDOWN_INSTRUCTION = "请用简洁的 Markdown 格式回复。"
PLAIN_TEXT_INSTRUCTION = (
    "用户正在通过语音播报收听回复。请只使用纯文本，"
    "不要使用 Markdown、项目符号星号或标题，使用简短自然的句子。"
)


async def notion_agent_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict:
    """运行工具调用循环来完成 Notion 任务。"""
    task = state.get("current_task") or {}
    intent = task.get("intent", "")
    access_token = runtime_value(state, runtime, "notion_access_token")
    meta = result_meta(task)
    await mark_task_in_progress_if_persisted(state, task, runtime)

    if not access_token:
        return {
            "agent_results": [{
                "route": "notion",
                "intent": intent,
                "result": (
                    "Notion 尚未连接。请在 backend/.env 配置 NOTION_API_KEY"
                    "（内部集成 token），或在设置中连接 Notion 工作区。"
                ),
                **meta,
                **task_status_meta(TASK_STATUS_FAILED, "Notion 未连接"),
            }]
        }

    is_voice = state.get("is_voice_request", False)
    settings = runtime_value(state, runtime, "settings")
    today = datetime.now(ZoneInfo(tz_name_of(settings))).strftime("%A, %B %d, %Y %I:%M %p")
    fmt = PLAIN_TEXT_INSTRUCTION if is_voice else MARKDOWN_INSTRUCTION

    system_text = NOTION_SYSTEM_PROMPT.format(format_instruction=fmt, today=today)
    upstream_context = str(state.get("upstream_context") or "")
    if upstream_context:
        system_text = f"{system_text}\n{upstream_context}\n"
    system = SystemMessage(content=system_text)

    tools = make_notion_tools(access_token)
    agent = llm.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}

    conversation = list(state.get("messages", []))
    work_messages = [system, *conversation, HumanMessage(content=intent)]

    tool_errors: list[str] = []

    for round_num in range(MAX_TOOL_ROUNDS):
        response = await astream_invoke(agent, work_messages)
        work_messages.append(response)

        if not response.tool_calls:
            result = response.content if isinstance(response.content, str) else ""
            logger.info("Notion agent completed in %d round(s)", round_num + 1)
            return {
                "agent_results": [{
                    "route": "notion", "intent": intent, "result": result,
                    **meta, **task_status_meta(TASK_STATUS_SUCCESS),
                }],
                **step_result_update(task, result, TASK_STATUS_SUCCESS),
            }

        tool_results = await asyncio.gather(*[
            _invoke_tool(tools_by_name, call) for call in response.tool_calls
        ])
        for tool_message in tool_results:
            if tool_message.content.startswith(_TOOL_ERROR_PREFIX):
                tool_errors.append(tool_message.content)
        work_messages.extend(tool_results)

    last = work_messages[-1]
    result = last.content if isinstance(last.content, str) else "Notion 请求已完成。"
    logger.warning("Notion agent hit max tool rounds (%d)", MAX_TOOL_ROUNDS)
    status = TASK_STATUS_FAILED if tool_errors else TASK_STATUS_PARTIAL
    return {
        "agent_results": [{
            "route": "notion", "intent": intent, "result": result,
            **meta,
            **task_status_meta(
                status,
                tool_errors[0][:300] if tool_errors else "工具轮次耗尽仍未完成",
            ),
        }],
        **step_result_update(task, result, status),
    }


async def _invoke_tool(tools_by_name: dict, call: dict) -> ToolMessage:
    name = call.get("name", "")
    call_id = call.get("id", "")
    args = call.get("args") or {}
    target = tools_by_name.get(name)
    try:
        output = await target.ainvoke(args) if target else f"未知工具：{name}"
    except Exception as exc:
        logger.error("Notion tool %s failed: %s", name, exc)
        output = f"{_TOOL_ERROR_PREFIX}{exc}"
    return ToolMessage(
        content=output if isinstance(output, str) else str(output),
        tool_call_id=call_id,
    )
