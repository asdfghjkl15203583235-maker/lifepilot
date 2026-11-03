"""旅行规划的并行拆解 + 合成支撑。

真实测试里「去上海玩两天」只走到一个 direct 步骤，合并节点拿不到任何真实数据，
最后输出的是网上抄来的攻略。这里提供三件确定性工具：

1. `is_travel_request` — 识别旅行规划类请求（不依赖 LLM，避免漏判）；
2. `build_travel_steps` — 把它拆成天气 / 景点 / 交通（+ 可选联网检索攻略）几个
   **无依赖**步骤，由 graph 的 `_fan_out` 并行调度；
3. `build_weather_advice` — 从天气分支的真实输出里推导「带伞/穿衣」建议，
   保证合成结果一定有可执行的穿衣提醒，而不是模型自由发挥。
"""

from __future__ import annotations

import re
from typing import Any

from agents.common import NUMERIC_RANGE_RULE
from agents.core_intent import city_candidate_of_text, strip_time_tokens

ROUTE_LIFE = "life"
ROUTE_SEARCH = "search"

# 「玩两天」「旅游」「行程」「攻略」「自由行」这类词出现，才可能是旅行规划。
_TRAVEL_HINT_RE = re.compile(
    r"旅游|旅行|游玩|度假|自由行|行程|攻略|出行计划|"
    r"玩[0-9一二两三四五六七八九十]+[天日]|"
    r"[0-9一二两三四五六七八九十]+[天日]的?(行程|计划|游)"
)
# 「去/到 X」里的 X 是目的地候选。
_TRAVEL_DEST_RE = re.compile(
    r"(?:去|到|飞往|前往|在)\s*([一-龥]{2,6}?)(?:玩|待|呆|逛|游|旅游|旅行|出差|度假|"
    r"的|，|,|。|\s|$)"
)
_DURATION_RE = re.compile(
    r"([0-9一二两三四五六七八九十]+)\s*[天日]"
)
# 「北京周末两日游」「帮我规划上海两日游」这类没有「去/到」的目的地写法：
# 先剥掉祈使动词，再取紧挨着「周末/两日游/旅游/攻略」的那个词。
_LEADING_VERB_RE = re.compile(
    r"^(?:帮我|帮忙|请你|请|麻烦|给我|我要|我想|想要|想|要|"
    r"规划|计划|安排|制定|做一?个?|来个|来一?个?|一份)+"
)
_TRAVEL_DEST_NEAR_RE = re.compile(
    r"([一-龥]{2,4}?)(?:周末|假期|[0-9一二两三四五六七八九十]+[日天](?:游|行程|计划)?|"
    r"旅游|旅行|自由行|攻略|行程)"
)
# 这些词本身不是地名，防止「周末两日游」被解析成目的地「周末」。
_DEST_STOPWORDS = {
    "周末", "假期", "今天", "明天", "后天", "这周", "下周", "一个",
    "这份", "我的", "两日", "三日", "行程", "旅游", "攻略", "计划",
}

TRAVEL_STEP_PRIORITY = "medium"
TRAVEL_STEP_PROJECT = "生活"

# 降水词 → 必须给出带伞提醒。
_RAIN_HINTS = ("雨", "阵雨", "雷阵雨", "暴雨", "小雪", "中雪", "大雪", "雪")
_TEMPERATURE_RE = re.compile(r"(-?\d{1,2})\s*(?:~|-|到)?\s*(-?\d{1,2})?\s*°?C", re.IGNORECASE)


def is_travel_request(text: str) -> bool:
    """用户是否在要求做旅行/出行规划（"去上海玩两天"）。"""
    if not text:
        return False
    return bool(_TRAVEL_HINT_RE.search(text))


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
    condensed = _LEADING_VERB_RE.sub("", text).strip()
    for candidate_text in (condensed, text):
        near = _TRAVEL_DEST_NEAR_RE.search(candidate_text)
        if not near:
            continue
        candidate = strip_time_tokens(near.group(1)).strip()
        if 2 <= len(candidate) <= 4 and candidate not in _DEST_STOPWORDS:
            return candidate

    destination, confidence = city_candidate_of_text(text)
    return destination


def extract_travel_duration(text: str) -> str:
    """「玩两天」→「两天」；取不到就返回空串（合成时按两天默认）。"""
    match = _DURATION_RE.search(text or "")
    if match is None:
        return ""
    return f"{match.group(1)}天"


def build_travel_steps(
    destination: str,
    duration: str,
    *,
    plan_id: str,
    include_search: bool = False,
) -> list[dict[str, Any]]:
    """并行步骤：天气 / 景点 / 交通（+ 可选联网攻略），彼此无依赖。

    返回的是 planner 的原始 step dict（`_plan_to_tasks` 之前的结构），
    由 planner 统一做 route 校验、due_at 解析与落库判定。

    `include_search=True` 时追加一条 `route="search"` 的联网检索步骤，
    让「最新攻略 / 避坑 / 排队情况」进入合成，而不是靠模型常识编。
    """
    destination_text = destination.strip() or "目的地"
    duration_text = duration or "两天"
    common = {
        "route": ROUTE_LIFE,
        "priority": TRAVEL_STEP_PRIORITY,
        "project": TRAVEL_STEP_PROJECT,
        "depends_on": [],
        "due_at": None,
        "plan_id": plan_id,
        "source_metadata": {"planner": "travel", "plan_summary": f"{destination_text}{duration_text}出行规划"},
        "created_by_plan": True,
    }
    steps = [
        {
            **common,
            "id": "step-weather",
            "intent": f"查询{destination_text}{duration_text}的天气与气温",
        },
        {
            **common,
            "id": "step-sights",
            "intent": f"查询{destination_text}的热门景点与游客评价",
        },
        {
            **common,
            "id": "step-transport",
            "intent": f"查询{destination_text}的市内交通与景点间路线",
        },
    ]
    if include_search:
        steps.append(
            {
                **common,
                "id": "step-tips",
                "route": ROUTE_SEARCH,
                "intent": f"检索{destination_text}{duration_text}最新旅游攻略、避坑与排队情况",
            }
        )
    return steps


def build_weather_advice(results: list[dict[str, Any]]) -> str:
    """从天气分支的真实输出推导带伞/穿衣建议；无天气数据时返回空串。

    需求：合成的行程必须包含「提醒带伞穿衣」，而不是单纯罗列攻略。
    这一步是确定性兜底——LLM 忘了写，页脚也会补上。
    """
    weather_text = ""
    for result in results:
        intent = str(result.get("intent") or "")
        route = str(result.get("route") or "")
        if route == ROUTE_LIFE and ("天气" in intent or "气温" in intent):
            weather_text = str(result.get("result") or "")
            break
    if not weather_text:
        return ""

    advice: list[str] = []
    if any(hint in weather_text for hint in _RAIN_HINTS):
        advice.append("有降水，记得带伞或雨具")

    temperatures: list[int] = []
    for match in _TEMPERATURE_RE.finditer(weather_text):
        for group in match.groups():
            if group is not None:
                try:
                    temperatures.append(int(group))
                except ValueError:
                    continue
    if temperatures:
        lowest = min(temperatures)
        highest = max(temperatures)
        if lowest <= 10:
            advice.append(f"最低约 {lowest}°C，穿厚外套注意保暖")
        elif highest >= 30:
            advice.append(f"最高约 {highest}°C，穿透气衣物并注意防晒补水")
        else:
            # 用「至」而不是 `~`：成对的 `~` 会被前端 Markdown 当作删除线。
            advice.append(f"气温约 {lowest}至{highest}°C，正常长袖/薄外套即可")

    if not advice:
        return ""
    return "出行提醒：" + "；".join(advice) + "。"


TRAVEL_SYNTHESIS_RULES = f"""\
本轮是**旅行规划**，不是数据播报。请把各分支结果综合成一份可直接执行的行程：
- 先给一句整体判断（天气是否适合出行、几天节奏怎么安排）。
- 按天给出路线：每天上午/下午/晚上去哪些景点，景点之间用什么交通方式、大约多久。
- 天气信息必须落到**可执行建议**上：要不要带伞、穿什么衣服、哪一天适合户外。
- 景点部分要写"为什么去"（评价/人均/评分/适合人群/避坑），不要只罗列名字；
  有 `rating`/`cost` 就引用具体数值，没有就说"暂无评分"，不要编造。
- 联网检索（search 分支）里的预约规则、闭馆日、排队/避坑提醒要写进对应那一天的
  行程里，并说明"据最新攻略"；不要整段照抄网页，也不要编造检索结果里没有的信息。
- 只有分支结果里真实出现的天气数值、地名、交通信息才能当事实陈述；
  工具没有返回的细节（门票、营业时间、餐厅评分）不要编造具体数字，
  用"建议出行前确认"带过。
- {NUMERIC_RANGE_RULE}
- 不要出现"以下是查询结果""根据资料"这类话术。
"""


def render_travel_advice_footer(results: list[dict[str, Any]]) -> str:
    """确定性页脚：保证带伞/穿衣提醒一定出现。"""
    advice = build_weather_advice(results)
    return f"\n\n{advice}" if advice else ""
