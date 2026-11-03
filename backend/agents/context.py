"""上下文与状态分离：叙事流（messages）与事实库（State 一等公民字段）。

架构原则
--------
把一切塞进 messages 会导致两个问题：Token 暴涨（工具返回的完整 JSON 无限追加）
和模型"失忆"（在长历史里考古，被上一轮的旧工具输出带偏）。

这里把两者严格分开：

  ┌── 叙事流 (messages) ─────────────────────────────────────────────┐
  │ 只保留用户与助手的**对话文本**。工具调用的原始 JSON 结果不进历史。 │
  └──────────────────────────────────────────────────────────────────┘
  ┌── 事实库 (AgentState 一等公民字段) ──────────────────────────────┐
  │ core_intent   业务状态 —— 这轮会话到底在谈什么（主题/路由/实体） │
  │ step_results  执行状态 —— 各步骤产出的简短结论（不是原始 JSON）  │
  │ pending_actions 任务状态 —— 待确认草案                           │
  └──────────────────────────────────────────────────────────────────┘

事实库通过 `render_state_facts` 结构化注入 System Prompt，模型读事实，不考古。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import SystemMessage

# 工具回执/原始 JSON 的特征键。命中 + 整体可被 json 解析成 dict → 判定为工具
# 原始载荷，不应进入对话历史（其结论已由 agent 以自然语言写进最终回复）。
_TOOL_PAYLOAD_KEYS = frozenset({
    "provider", "raw", "status", "reminder_id", "job_id", "task_id",
    "action_id", "action_type", "adcode", "formatted_address", "lives",
    "temperature_c", "humidity", "latitude", "longitude", "results",
})
_MAX_TOOL_PAYLOAD_CHARS = 800


def is_tool_payload(text: str) -> bool:
    """这条消息是不是工具调用的原始 JSON 载荷（应该只留在 State，不进历史）。"""
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped.startswith("{") and not stripped.startswith("["):
        return False
    if len(stripped) > 20_000:
        # 超大 blo b 一望即知是原始载荷。
        return True
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    if any(key in parsed for key in _TOOL_PAYLOAD_KEYS):
        return True
    # 一个纯 JSON dict 且很长：多半是工具结果，prose 不会是纯 JSON。
    return len(stripped) > _MAX_TOOL_PAYLOAD_CHARS


def sanitize_narrative(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从对话历史里剔除工具原始 JSON 载荷，只保留叙事文本流。"""
    cleaned: list[dict[str, Any]] = []
    for item in payloads or []:
        role = item.get("role", "")
        content = item.get("content", "")
        if role in ("user", "assistant") and isinstance(content, str) and is_tool_payload(content):
            continue
        cleaned.append(item)
    return cleaned


def render_state_facts(state: dict[str, Any]) -> SystemMessage | None:
    """把 State 里的事实（core_intent / step_results / pending_actions）渲染成
    一条结构化 System 消息，供各节点在组装 Prompt 时注入。

    只注入"结构化事实"；没有任何事实时返回 None（不产生空噪音）。
    """
    sections: list[str] = []

    core = state.get("core_intent") or {}
    if core and core.get("topic"):
        from agents.core_intent import render_core_intent

        core_line = render_core_intent(core)
        if core_line:
            sections.append(core_line)

    step_results = state.get("step_results") or {}
    if step_results:
        lines = [f"- {key}：{_one_line(value)}" for key, value in step_results.items()]
        sections.append("【本步骤已产出的事实 — 直接引用，勿再查询】\n" + "\n".join(lines))

    pending = state.get("pending_actions") or []
    if pending:
        lines = [
            f"- {item.get('action_type') or '操作'}（待确认 id={item.get('action_id')}）"
            for item in pending
            if isinstance(item, dict)
        ]
        if lines:
            sections.append("【待用户确认的草案】\n" + "\n".join(lines))

    if not sections:
        return None
    return SystemMessage(content="\n".join(sections))


def _one_line(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:300]


__all__ = [
    "is_tool_payload",
    "sanitize_narrative",
    "render_state_facts",
]
