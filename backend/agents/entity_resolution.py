"""地名实体消歧 — 正则置信度不足时的 LLM 二次推理。

`core_intent` 的正则抽取只能给出字符串匹配与置信度，遇到中文时间词就会误判：
「周二天气怎么样」会被抽成地名「周二」。中文里「周/星期/明天」不可能出现在
城市名中，这类候选一律标记为低置信度（`DESTINATION_CONFIDENCE_AMBIGUOUS`）。

本模块是混合路由的第二层：低置信度时把**用户原话**和**会话已确认目的地**
一起交给 LLM 做二次推理。历史目的地作为强先验显式注入——除非用户明确换了
城市，否则不得改动，从而把「周二天气」正确还原成「上海（周二这天）」。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agents.core_intent import (
    DESTINATION_SOURCE_DICTIONARY,
    DESTINATION_SOURCE_INHERITED,
    DESTINATION_SOURCE_REGEX,
    ROUTE_LIFE,
)
from config import Settings, chat_llm

logger = logging.getLogger(__name__)

# 低于该置信度的新城市不覆盖历史先验，避免一次模型幻觉把整个会话带偏。
MIN_ACCEPTED_CONFIDENCE = 0.6

# 只有可能涉及地点/出行的句子才值得多花一次 LLM 调用。
PLACE_HINT_RE = re.compile(
    r"天气|气温|地图|导航|路线|附近|去|到|飞往|前往|玩|旅游|旅行|出差|行程|攻略|出行|住"
)

_RESOLUTION_SYSTEM_PROMPT = """\
你是中文地点实体消歧器。系统先用正则从用户消息里抽了一个候选地名，需要你复核它是否正确，并给出最终城市。

硬性规则：
1. 「周二」「周三」「明天」「后天」「下周」「周末」「上午」「下午」这类词是**时间**，
   绝不是地名。绝不允许把时间词当作 destination 返回。
2. 正则候选可能被动词前缀污染（「查看青岛」被错抽成「看青岛」、「查一下北京」
   被错抽成「查北京」）。候选里若残留「看/查/去/到/在/问/飞往/前往」这类引导
   动词，去掉它，返回干净的城市名。
3. 如果本轮只说了时间（如「那周二呢」「周二天气怎么样」），说明用户仍在追问同一
   地点：直接返回【会话已确认目的地】里的城市，confidence 用 0.9。
4. 如果本轮明确说了新城市（「那北京呢」「去杭州玩」），返回该城市，confidence 用 0.9。
5. 如果本轮完全没提地点、会话也没有已确认目的地，destination 返回空字符串，
   confidence 用 0。
6. 只输出城市/地区名称，不要带上「的」「天气」「玩」等修饰。

只回复 JSON，不要 Markdown：
{"destination": "<城市名或空串>", "confidence": <0-1 的小数>, "reason": "<一句话依据>"}
"""


def needs_llm_resolution(core: dict[str, Any] | None, text: str) -> bool:
    """当前这一轮是否需要走 LLM 二次推理/校验。

    按 destination 来源分流，实现"同城缓存"：
      - dictionary → 词典精确匹配真实城市，可信，跳过 LLM；
      - inherited  → 继承历史目的地（已确认过），跳过 LLM（缓存命中）；
      - regex      → 正则候选可能被动词前缀污染（「查看青岛」→「看青岛」），
                     即便给了高置信度也照样错，必须 LLM 校验；
      - none       → 没抽到地名，仅在涉及地点/出行时交给 LLM 推理（继承历史、
                     识别正则/词典都认不出的地名）。
    """
    if not core:
        return False
    source = str(core.get("destination_source") or "")
    if source in (DESTINATION_SOURCE_DICTIONARY, DESTINATION_SOURCE_INHERITED):
        return False
    if source == DESTINATION_SOURCE_REGEX:
        return True
    if str(core.get("route") or "") == ROUTE_LIFE:
        return True
    return bool(PLACE_HINT_RE.search(text or ""))


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(
            line for line in cleaned.split("\n") if not line.strip().startswith("```")
        ).strip()
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("place resolution returned non-object JSON")
    return payload


def _safe_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def apply_resolution(core: dict[str, Any], destination: str, confidence: float) -> dict[str, Any]:
    """把消歧结果写回 core_intent（保持 entities 结构不变）。"""
    resolved = dict(core)
    previous_destination = (core.get("entities") or {}).get("destination")
    if destination:
        resolved["entities"] = {**(core.get("entities") or {}), "destination": destination}
        resolved["destination_confidence"] = confidence
    else:
        # LLM 也判断不出地名：保留历史目的地作为弱先验，绝不编造新城市。
        resolved["entities"] = (
            {**(core.get("entities") or {})}
            if previous_destination
            else {}
        )
        resolved["destination_confidence"] = confidence
    return resolved


async def resolve_core_intent_destination(
    settings: Settings | None,
    core: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """涉及地点的地名 LLM 复核/纠正；任何失败都原样返回，不影响主链路。

    正则抽取结果只当 pre-filter，这里用 LLM 校验：修正被动词前缀污染的城市
    （「查看青岛」→「看青岛」）、把时间词还原成会话已确认目的地等。
    """
    if not core:
        return core
    latest_text = str(core.get("latest_utterance") or core.get("topic") or "")
    if not needs_llm_resolution(core, latest_text):
        return core

    if settings is None:
        return core

    # 正则候选（待校验）与历史目的地（已确认）分开：候选可能被动词前缀污染
    # （「查看青岛」→「看青岛」），绝不能把它当强先验喂给 LLM。
    regex_candidate = (core.get("entities") or {}).get("destination")
    regex_confidence = float(core.get("destination_confidence") or 0.0)
    history = [
        str(item) for item in (core.get("history_destinations") or [])
        if str(item).strip() and str(item).strip() != regex_candidate
    ]
    prior_text = "、".join(history[-3:]) or "（无）"

    try:
        llm = chat_llm(settings)
        response = await llm.ainvoke(
            [
                SystemMessage(content=_RESOLUTION_SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"【会话已确认目的地（强先验）】{prior_text}\n"
                        f"【正则抽取候选】{regex_candidate or '（无）'}（置信度 {regex_confidence:.2f}）\n"
                        f"【用户本轮原话】{latest_text}\n"
                        "请复核正则候选是否正确，并给出最终城市。"
                    )
                ),
            ],
        )
        raw = response.content if isinstance(response.content, str) else ""
        payload = _extract_json(raw)
    except Exception as exc:
        logger.warning("LLM place resolution failed, keeping regex result: %s", exc)
        return core

    destination = str(payload.get("destination") or "").strip()
    confidence = _safe_confidence(payload.get("confidence"))
    if destination and confidence < MIN_ACCEPTED_CONFIDENCE:
        # 低置信度的新城市不值得覆盖历史先验，避免一次幻觉把会话带偏。
        logger.info(
            "LLM place resolution returned low-confidence %r (%.2f); keeping prior",
            destination,
            confidence,
        )
        return core

    if destination != (core.get("entities") or {}).get("destination"):
        logger.info(
            "Place resolution: %r -> %r (confidence %.2f, reason=%s)",
            latest_text,
            destination,
            confidence,
            str(payload.get("reason") or "")[:120],
        )
    return apply_resolution(core, destination, confidence)
