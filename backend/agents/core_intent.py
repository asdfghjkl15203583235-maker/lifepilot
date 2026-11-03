"""Core intent: what this conversation is actually about.

Every turn starts a fresh LangGraph thread (see `_fresh_thread_id`), so the
graph cannot "remember" that the last few turns were about Shanghai weather.
Without an explicit carrier, a follow-up like "那后天呢？" gets classified as
small talk and answered with "我没有天气工具".

This module derives a deterministic `core_intent` from the full message
history: the last *anchored* request (one that names a route + entity), the
entities it established, and whether the latest utterance is a follow-up that
must inherit them. The result is a first-class state field, injected into every
agent prompt as structured context instead of being dug out of history.
"""

from __future__ import annotations

import re
from typing import Any

CORE_INTENT_KEY = "core_intent"

ROUTE_DIRECT = "direct"
ROUTE_EMAIL = "email"
ROUTE_CALENDAR = "calendar"
ROUTE_NOTION = "notion"
ROUTE_LIFE = "life"

# Route anchors: a message that matches one of these established the topic.
_ROUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (ROUTE_LIFE, re.compile(r"天气|气温|下雨|下雪|降雨|温度|地图|导航|路线|附近|地铁|公交")),
    (ROUTE_EMAIL, re.compile(r"邮件|邮箱|收件箱|发信|回信|email|inbox", re.IGNORECASE)),
    (ROUTE_CALENDAR, re.compile(r"日程|日历|议程|会议|约会|calendar|meeting", re.IGNORECASE)),
    (ROUTE_NOTION, re.compile(r"notion|工作区|页面|数据库|区块|block", re.IGNORECASE)),
)

# Follow-up utterances carry no new topic of their own: they lean on the
# previous turn ("那后天呢？", "重查一下", "换成北京呢").
_FOLLOWUP_PRONOUN_RE = re.compile(
    r"那[^，。！？]{0,10}(呢|怎么样|如何)|"
    r"呢[？?]?$|"
    r"再查|重查|重新查|再试|再看|再说一遍|重复一下|"
    r"这个|那个|那里|这儿|它|他们|上面|刚才|刚才那个|"
    r"换成|改成|改为|换个"
)
_TEMPORAL_ONLY_RE = re.compile(
    r"^(今天|明天|后天|大后天|今晚|明早|明晚|下周[一二三四五六日天]?|"
    r"周[一二三四五六日天]|上午|下午|晚上|中午|早上|未来几天)"
    r"[呢啊呀吧？?，,。！!]*$"
)

_CITY_PATTERNS = (
    re.compile(
        r"(?:去|到|飞往|前往|搬到|搬去|在|查询|看看|看看|查)\s*"
        r"([一-龥]{2,8}?)(?:的)?(?:天气|气温|地图|附近)"
    ),
    re.compile(r"(?:去|到|飞往|前往|搬到|搬去)\s*([一-龥]{2,8})"),
    re.compile(r"([一-龥]{2,8})(?:明天|后天|今天|下周)?的?(?:天气|气温)"),
)
_NON_CITIES = {
    "明天", "后天", "今天", "下周", "本周", "未来", "这几天", "这里", "那里",
    "附近", "当地", "城市", "天气", "气温", "预报", "什么", "怎么", "如何",
}

# 时间词不可能出现在地名里。旧正则会把「周二天气」抽成地名「周二」、
# 把「下周二上海天气」抽成「二上海」——这正是真实测试里「周二被当成地名」
# 的根因。这里一次性把所有时间词从候选串里剔除，再判定它还像不像地名。
TIME_TOKEN_RE = re.compile(
    r"(上|下|本|这)?(周|星期)[一二三四五六日天]?|周末|"
    r"今天|今日|明天|明日|明早|明晚|后天|大后天|今晚|今夜|"
    r"上午|下午|中午|晚上|早上|凌晨|深夜|"
    r"未来[几两三五]?[天日周]?|最近|现在|目前|这几天|接下来"
)

# 「去上海玩两天」的候选串会带上动作尾巴，剥掉后才剩裸地名。
_TRAILING_ACTIVITY_RE = re.compile(
    r"(玩|待|呆|逛|游|旅行|旅游|出差|度假|行程|攻略|自由行|"
    r"[0-9一二两三四五六七八九十]+[天日]).*$"
)

# 置信度：高 = 正则抽到干净地名；ambiguous = 抽到的其实是时间词/泛称。
DESTINATION_CONFIDENCE_HIGH = 0.9
DESTINATION_CONFIDENCE_AMBIGUOUS = 0.35
DESTINATION_CONFIDENCE_NONE = 0.0
DESTINATION_CONFIDENCE_INHERITED = 0.8


def strip_time_tokens(text: str) -> str:
    """剔除字符串里的全部时间词，用于判定「剩下的部分还像不像地名」。"""
    return TIME_TOKEN_RE.sub("", text or "").strip(" 的了呢啊吧，,、")
# Leading request verbs / filler and time adverbials wrap the city
# ("帮我查一下明天上海的天气"). Strip them in a fixpoint loop so the residual
# is the bare place name. 长词在前，避免"查询"被"查"截断成"询"。
_LEADING_FILLER_RE = re.compile(
    r"^(麻烦|请问|请|帮我|帮忙|给我|你能|你可以|看一下|了解一下|查一下|"
    r"告诉我|想知道|问一下|查询|查看|看看|了解|一下|帮忙|帮忙|"
    r"能|可以|在|的|和|跟|查|问)+"
)
_FILLER_TIME_RE = re.compile(
    r"^(今天|明天|后天|大后天|今天|今晚|明早|明晚|早上|上午|中午|下午|晚上|"
    r"下周|本周|这周|未来几天|未来|最近|现在|目前)+"
)
_TRAILING_TIME_RE = re.compile(
    r"(今天|明天|后天|大后天|今晚|明早|明晚|早上|上午|中午|下午|晚上|"
    r"下周|本周|这周|未来几天|最近|现在|目前)+$"
)


def _strip_filler(name: str) -> str:
    """剥掉地名前后的动词/时间状语/动作尾巴，返回裸地点名。"""
    name = name.strip(" 的了呢啊吧，,、")
    for _ in range(4):
        before = name
        name = TIME_TOKEN_RE.sub("", name)
        name = _FILLER_TIME_RE.sub("", name)
        name = _LEADING_FILLER_RE.sub("", name)
        name = _TRAILING_TIME_RE.sub("", name)
        name = name.strip(" 的了呢啊吧，,、")
        if name == before:
            break
    # 「上海玩两天」→「上海」：动作/天数尾巴不是地名的一部分。
    name = _TRAILING_ACTIVITY_RE.sub("", name)
    return name.strip(" 的了呢啊吧，,、")

# Deterministic HITL replies. "同意" after a draft must never be re-classified
# by the LLM — a mis-route turns a confirmation into small talk and the write
# silently never happens.
_HITL_APPROVE_RE = re.compile(
    r"^(同意|确认|确认执行|确定|好的?[，,]?确认|可以|执行|发出去|发送吧|批准|同意执行)[。！!，,\s]*$"
)
_HITL_REJECT_RE = re.compile(
    r"^(拒绝|取消|不要了|算了|别发了|不发了|不同意|撤销|不用了|否)[。！!，,\s]*$"
)


def _latest_user_text(message_payloads: list[dict[str, Any]]) -> str:
    for item in reversed(message_payloads or []):
        if item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str):
                return content.strip()
    return ""


def _user_texts(message_payloads: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("content") or "").strip()
        for item in message_payloads or []
        if item.get("role") == "user" and str(item.get("content") or "").strip()
    ]


def route_of_text(text: str) -> str | None:
    """The route a single utterance anchors, or None when it is topic-free."""
    if not text:
        return None
    for route, pattern in _ROUTE_PATTERNS:
        if pattern.search(text):
            return route
    return None


def city_candidate_of_text(text: str) -> tuple[str | None, float]:
    """抽取目的地 + 置信度。

    返回 `(地名, 置信度)`：
      - 干净地名 → (地名, DESTINATION_CONFIDENCE_HIGH)
      - 候选其实是时间词/泛称（「周二」）→ (None, DESTINATION_CONFIDENCE_AMBIGUOUS)
      - 完全没提地名 → (None, 0.0)

    置信度低或命中时间词时，调用方必须降级到 LLM 二次推理或澄清反问，
    绝不能把这个候选当成真实地名去查数据。
    """
    if not text:
        return None, DESTINATION_CONFIDENCE_NONE
    saw_ambiguous = False
    for pattern in _CITY_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1)
            candidate = _strip_filler(raw)
            if not candidate:
                # 候选被时间词/泛称吃干净了（「周二天气」）→ 语义上可能提过地点，
                # 但正则不可信。
                saw_ambiguous = True
                continue
            if len(candidate) < 2 or len(candidate) > 8 or candidate in _NON_CITIES:
                saw_ambiguous = True
                continue
            return candidate, DESTINATION_CONFIDENCE_HIGH
    confidence = DESTINATION_CONFIDENCE_AMBIGUOUS if saw_ambiguous else DESTINATION_CONFIDENCE_NONE
    return None, confidence


def city_of_text(text: str) -> str | None:
    """Best-effort destination/city mentioned in a request (high confidence only)."""
    candidate, confidence = city_candidate_of_text(text)
    if confidence >= DESTINATION_CONFIDENCE_HIGH:
        return candidate
    return None


def is_followup_utterance(text: str) -> bool:
    """True when the utterance only makes sense on top of the previous turn."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if route_of_text(stripped) and not _FOLLOWUP_PRONOUN_RE.search(stripped):
        # A full new request ("帮我查北京天气") stands on its own.
        return False
    if _TEMPORAL_ONLY_RE.match(stripped):
        return True
    if _FOLLOWUP_PRONOUN_RE.search(stripped):
        # A bare pronoun alone is too weak; require it to be a short utterance
        # so "那封邮件我改主意了，改成…" is not mistaken for a follow-up.
        return len(stripped) <= 24
    return False


def hitl_reply_of(text: str) -> str | None:
    """Classify a bare confirmation/rejection ("同意"/"拒绝")."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    if _HITL_APPROVE_RE.match(stripped):
        return "approve"
    if _HITL_REJECT_RE.match(stripped):
        return "reject"
    return None


def build_core_intent(
    message_payloads: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive the conversation's core intent from the full payload history.

    Returns:
        {
          "topic": <anchor request text>,
          "route": <inherited route or direct>,
          "entities": {"destination": <city>},
          "followup": <latest utterance inherits the anchor>,
          "query": <intent text agents should execute this turn>,
          "hitl_reply": "approve"|"reject"|None,
        }
    """
    latest = _latest_user_text(message_payloads)
    texts = _user_texts(message_payloads)

    anchor_text = ""
    anchor_route: str | None = None
    for text in texts:
        route = route_of_text(text)
        if route is not None:
            anchor_text, anchor_route = text, route

    # The latest request may itself be the anchor (fresh topic).
    latest_route = route_of_text(latest)
    followup = bool(
        anchor_route
        and anchor_text
        and anchor_text != latest
        and is_followup_utterance(latest)
    )

    if latest_route and not followup:
        topic, route = latest, latest_route
    elif followup:
        topic, route = anchor_text, anchor_route
    else:
        topic = latest or anchor_text
        route = latest_route or anchor_route or (previous or {}).get("route") or ROUTE_DIRECT

    # 实体抽取是纯正则，必须带置信度一起返回：正则抽到的可能只是时间词
    # （「周二天气」→「周二」）。低置信度交给 entity_resolution 用 LLM 二次推理。
    latest_destination, latest_confidence = city_candidate_of_text(latest)
    anchor_destination, anchor_confidence = city_candidate_of_text(anchor_text)
    previous_destination = ((previous or {}).get("entities") or {}).get("destination")

    # 历史目的地：用户在更早的轮次明确说过「去上海」。这类事实是追问的强先验，
    # 只依赖 anchor 会漏（anchor 往往是本轮那句「周二天气」本身）。
    history_destinations: list[str] = []
    for text in texts:
        candidate, candidate_confidence = city_candidate_of_text(text)
        if candidate and candidate_confidence >= DESTINATION_CONFIDENCE_HIGH:
            if not history_destinations or history_destinations[-1] != candidate:
                history_destinations.append(candidate)
    history_destination = history_destinations[-1] if history_destinations else None

    if latest_destination is not None:
        destination, confidence = latest_destination, latest_confidence
    elif anchor_destination is not None:
        destination, confidence = anchor_destination, anchor_confidence
    elif history_destination is not None:
        destination, confidence = history_destination, DESTINATION_CONFIDENCE_INHERITED
    elif isinstance(previous_destination, str) and previous_destination.strip():
        destination, confidence = previous_destination.strip(), DESTINATION_CONFIDENCE_INHERITED
    else:
        destination, confidence = None, max(latest_confidence, anchor_confidence)

    if followup and anchor_text and latest:
        query = f"{anchor_text}；{latest}"
    else:
        query = latest or anchor_text

    return {
        "topic": topic[:200],
        "route": route,
        "entities": {"destination": destination} if destination else {},
        "destination_confidence": confidence,
        # 给 LLM 二次推理的强先验：本会话里用户明确说过的目的地（最近 3 个）。
        "history_destinations": history_destinations[-3:],
        "followup": followup,
        "anchor": anchor_text[:200],
        "query": query[:400],
        # LLM 二次推理要看到用户原话，而不是被模式匹配改写过的 topic。
        "latest_utterance": latest[:400],
        "hitl_reply": hitl_reply_of(latest),
    }


def render_core_intent(core: dict[str, Any] | None) -> str:
    """Render `core_intent` as the system-prompt block every agent receives."""
    if not core:
        return ""
    topic = str(core.get("topic") or "").strip()
    if not topic:
        return ""
    destination = (core.get("entities") or {}).get("destination")
    details = [f"路由：{core.get('route') or ROUTE_DIRECT}"]
    if destination:
        details.append(f"目的地/对象：{destination}")
    if core.get("followup"):
        details.append("本轮是追问，必须沿用上述主题，不得重新解释成闲聊")
    return (
        f"当前会话核心上下文：【{topic}】（{'；'.join(details)}）。"
        "请结合此上下文理解用户的最新消息；用户没有明确更换主题时，"
        "不得改变地点、对象或任务目标。"
    )
