"""统一地名解析 — 正则层唯一入口。

历史问题：地名提取分散在 `life_agent_node`（天气）、`travel_planning`（旅行）、
`core_intent`（地图/附近）三处，各写一套"动词前缀 + 时间词"清理正则，同一个 bug
要修三遍（「查看青岛」被抽成「看青岛」）。这里把三类候选模式收敛到一处，下游只调
这一个模块；LLM 复核统一走 `entity_resolution`，不在这里重复。

调用关系（无循环依赖）：
    destination_resolver → core_intent（city_candidate_of_text / strip_time_tokens）
    life_agent_node      → destination_resolver
    travel_planning      → destination_resolver
"""

from __future__ import annotations

import re
from typing import Any

from agents.core_intent import (
    DESTINATION_CONFIDENCE_AMBIGUOUS,
    DESTINATION_CONFIDENCE_HIGH,
    DESTINATION_CONFIDENCE_NONE,
    city_candidate_of_text,
    strip_time_tokens,
)

# ── 天气类：找「天气」前面的分句，再剥掉动词/时间状语 ──────────────────
# 长词在前（「查看」排在「查」前、「查询」排在「查」前），避免短动词截断长词，
# 把「查看青岛」拆成「查」+「看青岛」。
_CITY_MAX_CHARS = 8
_CITY_CLAUSE_SPLIT_RE = re.compile(r"[，,。.；;！!？?、\s]")
_WEATHER_LEADING_FILLER_RE = re.compile(
    r"^(?:麻烦|请问|请|帮我|帮忙|给我|你能|你可以|能|可以|"
    r"查一下|查下|查询|查看|查|看一下|看看|看|了解一下|了解|"
    r"告诉我|想知道|问一下|问|在|的|和|跟|"
    # 规划器会把 intent 改写成「根据上海天气…」「结合上海天气…」这类句式。
    r"根据|结合|按照|参考|基于)+"
)
_WEATHER_TRAILING_TIME_RE = re.compile(
    r"(?:今天|明天|后天|大后天|现在|当前|目前|本地|最近|未来|这几天|"
    r"近[几两三五]?[天日周个星期]|"
    r"一[周个星期]|两[天日]|三[天日]|[0-9一二三四五六七八九十]+[天日周])+$"
)
_WEATHER_LEADING_TIME_RE = re.compile(
    r"^(?:今天|明天|后天|大后天|现在|当前|目前|最近|未来|这几天)+"
)


def extract_weather_city(intent: str) -> str | None:
    """从天气类请求里提取城市，例如「北京天气怎么样」→「北京」。

    覆盖英文（"weather in Paris"）与中文（含规划器改写「查询上海近两天的天气情况」、
    「根据上海天气制定两天出行计划」）。取不到返回 None。
    """
    if not intent:
        return None
    lowered = intent.lower()
    for prefix in ("weather in ", "weather for ", "weather at ", "weather"):
        if lowered.startswith(prefix):
            return intent[len(prefix):].strip().strip("?.!。，").strip() or None
    index = intent.find("天气")
    if index <= 0:
        return None
    # 只取「天气」前面最后一个分句，再剥掉动词/时间状语，避免把整句当城市。
    head = _CITY_CLAUSE_SPLIT_RE.split(intent[:index])[-1]
    for _ in range(3):
        before = head
        head = head.strip().rstrip("的")
        head = _WEATHER_LEADING_TIME_RE.sub("", head)
        head = _WEATHER_LEADING_FILLER_RE.sub("", head)
        head = _WEATHER_TRAILING_TIME_RE.sub("", head)
        if head == before:
            break
    head = head.strip().rstrip("的")
    # 「周二」「下周三」是时间词，绝不能当成城市名去查；剥掉后为空即视为未提地点。
    head = strip_time_tokens(head)
    if 1 <= len(head) <= _CITY_MAX_CHARS:
        return head
    return None


def mentions_place(intent: str) -> bool:
    """用户有没有在「天气」前面点名过地点（哪怕解析不出来）。

    用来区分两种完全不同的情况：
      - "天气怎么样"（没提地点）→ 用默认城市合理；
      - "火星天气"（点了名但解析失败）→ 必须失败，绝不能拿默认城市顶替。
    """
    if not intent:
        return False
    lowered = intent.lower()
    if "weather" in lowered:
        rest = lowered.split("weather", 1)[1]
        rest = rest.replace("in", " ").replace("for", " ").replace("at", " ")
        return bool(rest.strip(" ?.!。，"))
    index = intent.find("天气")
    if index <= 0:
        return False
    head = _CITY_CLAUSE_SPLIT_RE.split(intent[:index])[-1]
    for _ in range(3):
        before = head
        head = head.strip().rstrip("的")
        head = _WEATHER_LEADING_TIME_RE.sub("", head)
        head = _WEATHER_LEADING_FILLER_RE.sub("", head)
        head = _WEATHER_TRAILING_TIME_RE.sub("", head)
        if head == before:
            break
    head = strip_time_tokens(head)
    return bool(head.strip().strip("的"))


# ── 旅行类：紧挨时长词的目的地（「北京周末两日游」「规划上海游」）───────
_TRAVEL_DEST_RE = re.compile(
    r"(?:去|到|飞往|前往|在)\s*([一-龥]{2,6}?)(?:玩|待|呆|逛|游|旅游|旅行|出差|度假|"
    r"的|，|,|。|\s|$)"
)
# 「北京周末两日游」「帮我规划上海两日游」这类没有「去/到」的目的地写法：
# 先剥掉祈使动词，再取紧挨着「周末/两日游/旅游/攻略」的那个词。
_TRAVEL_LEADING_VERB_RE = re.compile(
    r"^(?:帮我|帮忙|请你|请|麻烦|给我|我要|我想|想要|想|要|"
    r"规划|计划|安排|制定|做一?个?|来个|来一?个?|一份)+"
)
_TRAVEL_DEST_NEAR_RE = re.compile(
    r"([一-龥]{2,4}?)(?:周末|假期|[0-9一二两三四五六七八九十]+[日天](?:游|行程|计划)?|"
    r"旅游|旅行|自由行|攻略|行程)"
)
# 这些词本身不是地名，防止「周末两日游」被解析成目的地「周末」。
_TRAVEL_DEST_STOPWORDS = {
    "周末", "假期", "今天", "明天", "后天", "这周", "下周", "一个",
    "这份", "我的", "两日", "三日", "行程", "旅游", "攻略", "计划",
}


def extract_travel_destination(
    text: str,
    fallback: str | None = None,
) -> str | None:
    """从旅行请求里取目的地；取不到就用会话已确认的目的地。"""
    if fallback and fallback.strip():
        return fallback.strip()
    if not text:
        return None
    match = _TRAVEL_DEST_RE.search(text)
    if match:
        candidate = match.group(1).strip(" 的，,。")
        if 2 <= len(candidate) <= 6:
            return candidate

    # 「北京周末两日游」「帮我规划上海两日游」：没有 去/到，城市紧挨着时长词。
    condensed = _TRAVEL_LEADING_VERB_RE.sub("", text).strip()
    for candidate_text in (condensed, text):
        near = _TRAVEL_DEST_NEAR_RE.search(candidate_text)
        if not near:
            continue
        candidate = strip_time_tokens(near.group(1)).strip()
        if 2 <= len(candidate) <= 4 and candidate not in _TRAVEL_DEST_STOPWORDS:
            return candidate

    destination, _confidence = city_candidate_of_text(text)
    return destination


def extract_destination_candidate(text: str) -> tuple[str | None, float]:
    """正则层统一入口：旅行紧邻 → 天气 → 地图/附近，返回 (候选, 置信度)。

    置信度语义与 `city_candidate_of_text` 一致：
      - 抽到干净地名 → (地名, HIGH)
      - 命中时间词/泛称但没地名 → (None, AMBIGUOUS)
      - 完全没提地点 → (None, NONE)

    这是给"需要最全正则候选"的场景用的统一入口；LLM 复核仍走 entity_resolution。
    """
    if not text:
        return None, DESTINATION_CONFIDENCE_NONE

    # 天气（含英文）优先：含「天气/weather」的请求不应被旅行紧邻模式误提取
    # （「查询上海近两天的天气情况」里的「近两天」会被旅行正则当成时长词）。
    weather = extract_weather_city(text)
    if weather:
        return weather, DESTINATION_CONFIDENCE_HIGH

    travel = extract_travel_destination(text)
    if travel:
        return travel, DESTINATION_CONFIDENCE_HIGH

    return city_candidate_of_text(text)
