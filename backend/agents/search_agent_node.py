"""联网搜索代理节点（route = `search`）。

旅行规划里"推荐理由"最容易被模型写成通用常识。这个节点把用户的检索意图交给
Tavily 拿真实网页结果，作为一条独立的 agent_result 进入合并节点（Synthesis），
和天气/景点/交通并列——而不是让模型凭记忆编攻略。

刻意保持"单次检索"：一个任务一次搜索，最多 5 条结果，成本可控；需要更多视角时
由 Planner 拆成多个 search 步骤并行。
"""

import logging

from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime

from agents.common import (
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCESS,
    mark_task_in_progress_if_persisted,
    result_meta,
    runtime_value,
    step_result_update,
    task_status_meta,
)
from agents.state import AgentState
from services.web_search_client import WebSearchError, format_search_result, search_web

logger = logging.getLogger(__name__)


async def search_agent_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict:
    """Run one web search for the current task and report honest success/failure."""
    task = state.get("current_task") or {}
    intent = str(task.get("intent") or "").strip()
    meta = result_meta(task)
    await mark_task_in_progress_if_persisted(state, task, runtime)

    settings = runtime_value(state, runtime, "settings")
    query = intent or "最新资讯"
    try:
        payload = await search_web(settings, query)
    except WebSearchError as exc:
        # 没配置 key / 网络失败都要如实说，不能让模型假装搜过。
        logger.warning("Web search failed for %r: %s", query, exc)
        message = f"联网检索没有成功（{exc}）。以下建议请不要假装来自搜索结果。"
        return {
            "agent_results": [{
                "route": "search",
                "intent": intent,
                "result": message,
                "error": str(exc),
                **meta,
                **task_status_meta(TASK_STATUS_FAILED, str(exc)),
            }],
            **step_result_update(task, message, TASK_STATUS_FAILED),
        }
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读失败
        logger.exception("Unexpected web search error for %r", query)
        message = f"联网检索出现异常（{exc}）。"
        return {
            "agent_results": [{
                "route": "search",
                "intent": intent,
                "result": message,
                "error": str(exc),
                **meta,
                **task_status_meta(TASK_STATUS_FAILED, str(exc)),
            }],
            **step_result_update(task, message, TASK_STATUS_FAILED),
        }

    text = format_search_result(payload)
    logger.info("Web search for %r returned %d result(s)", query, len(payload.get("results") or []))
    return {
        "agent_results": [{
            "route": "search",
            "intent": intent,
            "result": text,
            **meta,
            **task_status_meta(TASK_STATUS_SUCCESS),
        }],
        **step_result_update(task, text, TASK_STATUS_SUCCESS),
    }
