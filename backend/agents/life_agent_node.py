"""生活服务代理节点 — 通过 REST 适配器回答天气、地图和生活服务请求。"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from langgraph.runtime import Runtime

from agents.common import (
    CITY_CONSISTENCY_RULE,
    NO_REPEAT_RULE,
    NUMERIC_RANGE_RULE,
    ROUTE_ORIGIN_RULE,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_SUCCESS,
    mark_task_in_progress_if_persisted,
    render_time_reference,
    result_meta,
    runtime_value,
    step_result_update,
    task_status_meta,
    tz_name_of,
)
from agents.destination_resolver import extract_weather_city, mentions_place
from agents.state import AgentState
from agents.stream_collector import astream_invoke
from agents.tools.life_tool import LifeTool
from services.life_service_client import get_weather

logger = logging.getLogger(__name__)

MAX_LIFE_TOOL_ROUNDS = 3

# 未来时间词：用户问的是"预报/未来某天"，而高德实时接口只返回当前实况。
# 命中即要求在回复中标注"这是当前实时天气，暂无明日/未来预报"，严禁把实况
# 当成预报敷衍（实测模型会把此刻的"多云转晴"说成"明天多云转晴"）。
_FUTURE_TIME_RE = re.compile(
    r"明天|明日|明早|明晚|后天|大后天|下周|下个?星期|下月|未来[几两三五]?[天日周]|"
    r"未来|接下来|之后|稍后|过几天|这两天|明后"
)


def _asks_future_time(text: str) -> bool:
    """用户是否在问未来某个时间的天气（而非当前实况）。"""
    return bool(_FUTURE_TIME_RE.search(text or ""))


# 相对日期词 → 距今天数。命中即说明用户要的是"某一天"的预报。
_DAY_OFFSET_WORDS: tuple[tuple[str, int], ...] = (
    ("大后天", 3),
    ("后天", 2),
    ("明晚", 1),
    ("明早", 1),
    ("明日", 1),
    ("明天", 1),
    ("今天", 0),
    ("今日", 0),
    ("今晚", 0),
)


def _requested_day_offset(intent: str) -> int | None:
    """用户问的是"今天/明天/后天"中的哪一天（返回距今天数），没指定返回 None。

    只用于**选日**：把用户口吻的时间词映射成绝对日期，再从高德 `forecasts`
    里挑出那一天。识别不出来（如只问"未来几天"）返回 None，交给模型按多日
    预报综合回答。
    """
    text = intent or ""
    for word, offset in _DAY_OFFSET_WORDS:
        if word in text:
            return offset
    return None


def _forecast_for_day(
    payload: dict[str, Any],
    *,
    tz_name: str,
    day_offset: int,
) -> dict[str, Any] | None:
    """从高德逐日预报里挑出 offset 天后的那一条；没有则返回 None。"""
    forecasts = payload.get("forecasts") or []
    if not forecasts:
        return None
    try:
        target = (datetime.now(ZoneInfo(tz_name)) + timedelta(days=day_offset)).strftime("%Y-%m-%d")
    except Exception:
        return None
    for cast in forecasts:
        if str(cast.get("date") or "") == target:
            return cast
    return None


def _render_forecast(cast: dict[str, Any], city: str, label: str) -> str:
    """把一天的预报渲染成一句自然语言（昼/夜温差 + 天气现象）。"""
    day_temp = cast.get("day_temp_c")
    night_temp = cast.get("night_temp_c")
    day_weather = cast.get("day_weather") or cast.get("night_weather") or "未知"
    temp_text = (
        # 「至」而不是 `~`：两个 `~` 连同中间内容会被前端 Markdown 当成删除线。
        f"{day_temp}至{night_temp}°C"
        if day_temp is not None and night_temp is not None
        else f"{day_temp if day_temp is not None else night_temp}°C"
    )
    return f"{city}{label}的天气：{day_weather}，气温 {temp_text}。"


def _day_label(day_offset: int) -> str:
    return {0: "今天", 1: "明天", 2: "后天"}.get(day_offset, f"{day_offset}天后")


def _render_realtime_reference(
    payload: dict[str, Any], city_label: str, day_label: str,
) -> str:
    """实况只能作为「参考」出现，且必须点明它不是那一天的预报。"""
    return (
        f"（作为参考，{city_label}当前实时天气："
        f"{payload.get('condition') or '未知'}，"
        f"气温 {payload.get('temperature_c')}°C——这是此刻实况，不是{day_label}的预报。）"
    )


def _weather_answer(
    payload: dict[str, Any],
    intent: str,
    *,
    tz_name: str,
    city_label: str,
) -> str:
    """按用户问的日期作答：逐日预报优先，缺失就明说「暂无该日预报」。

    高德 `extensions=all` 会返回未来几天的 `forecasts`；能匹配到那一天就直接
    引用那一天的昼/夜温度与天气现象。匹配不到（超出预报范围）绝不拿此刻实况
    冒充预报——只把它标注为参考实况。
    """
    day_offset = _requested_day_offset(intent)
    if day_offset is not None:
        label = _day_label(day_offset)
        cast = _forecast_for_day(payload, tz_name=tz_name, day_offset=day_offset)
        if cast is not None:
            return _render_forecast(cast, city_label, label)
        return (
            f"{city_label}{label}暂无该日预报。"
            + _render_realtime_reference(payload, city_label, label)
        )

    forecasts = payload.get("forecasts") or []
    if forecasts:
        return "\n".join(
            _render_forecast(cast, city_label, str(cast.get("date") or ""))
            for cast in forecasts[:4]
        )

    core = (
        f"{city_label}的天气："
        f"{payload.get('condition') or '未知'}，"
        f"气温 {payload.get('temperature_c')}°C"
    )
    if _asks_future_time(intent):
        return (
            f"{core}。（这是当前实时天气，暂无明日/未来预报；"
            "请勿把它当成未来某天的天气。）"
        )
    return core


def _mentions_place(intent: str) -> bool:
    """用户有没有在「天气」前面点名过地点（哪怕解析不出来）。

    用来区分两种完全不同的情况：
      - "天气怎么样"（没提地点）→ 用默认城市合理；
      - "火星天气"（点了名但解析失败）→ 必须失败，绝不能拿默认城市顶替。
    """
    return mentions_place(intent)


def _session_destination(state: AgentState) -> str | None:
    """会话已确认的目的地（来自 core_intent.entities.destination）。

    跨轮场景："去成都" 之后用户只说 "酒店定在太古里"，本轮 intent 没有城市。
    高德会把同名地点解析到别的城市，所以必须沿用会话里已确认的城市。
    """
    core = state.get("core_intent") or {}
    destination = (core.get("entities") or {}).get("destination")
    if isinstance(destination, str) and destination.strip():
        return destination.strip()
    return None


async def _parse_city(intent: str) -> str | None:
    """Extract a city name from a weather intent such as "北京天气怎么样"."""
    return extract_weather_city(intent)


async def life_agent_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    """解析一个生活服务意图，可选用已配置的 LLM。"""
    task = state.get("current_task") or {}
    intent = str(task.get("intent") or "")
    db_session = runtime_value(state, runtime, "db_session")
    settings = runtime_value(state, runtime, "settings")
    meta = result_meta(task)
    await mark_task_in_progress_if_persisted(state, task, runtime)
    if settings is None:
        return {
            "agent_results": [
                {
                    "route": "life",
                    "intent": intent,
                    "result": "当前会话尚未配置生活服务。",
                    **meta,
                    **task_status_meta(TASK_STATUS_FAILED, "生活服务未配置"),
                }
            ]
        }

    # Best-effort narrow parsing before binding tools. Weather is the common case.
    session_destination = _session_destination(state)
    if "weather" in intent.lower() or "天气" in intent:
        city = await _parse_city(intent)
        # 本轮没点名城市，但会话里已经确认过目的地（"去成都" → "那后天呢"）：
        # 沿用会话城市，否则会回落到配置里的默认城市。
        if city is None and session_destination and not _mentions_place(intent):
            city = session_destination
        # 用户点名了地点却解析不出来（"火星天气"里的火星会被剥成空），绝不允许
        # 回落到默认城市（配置里是上海）。真实 badcase：问"火星天气"，高德查不到，
        # 旧代码拿默认城市顶替，用户看到的是上海的天气，还被当成"火星的天气"写进邮件。
        # 只有在用户根本没提地名（"天气怎么样"）时才允许用默认城市。
        if city is None and _mentions_place(intent):
            return {
                "agent_results": [{
                    "route": "life",
                    "intent": intent,
                    "result": (
                        f"没能识别出你要查的地点（{intent}）。"
                        "告诉我确切的城市名，我再查一次。"
                    ),
                    "error": "未识别到地点，拒绝使用默认城市顶替",
                    "fatal_error": True,
                    **meta,
                    **task_status_meta(TASK_STATUS_FAILED, "未识别到地点"),
                }],
            }
        try:
            payload = await get_weather(settings, city=city)
            city_label = payload.get("city") or "你的位置"
            # 高德 extensions=all 会返回逐日预报；用户问"明天/后天"时按日期匹配
            # 那一天的预报，取不到就明说"暂无该日预报"，绝不拿实况冒充。
            text = _weather_answer(
                payload,
                intent,
                tz_name=tz_name_of(settings),
                city_label=city_label,
            )
            return {
                "agent_results": [{
                    "route": "life", "intent": intent, "result": text,
                    **meta, **task_status_meta(TASK_STATUS_SUCCESS),
                }],
                **step_result_update(task, text, TASK_STATUS_SUCCESS),
            }
        except Exception as exc:
            # 绝不用别处的天气凑答案：查不到就明说查不到，并给出可执行的下一步。
            asked = f"「{city}」" if city else "你指定的地点"
            friendly = (
                f"没能查到{asked}的天气（{exc}）。"
                "请确认城市名后我再查一次，或直接告诉我城市即可。"
            )
            return {
                "agent_results": [
                    {
                        "route": "life",
                        "intent": intent,
                        "result": friendly,
                        "error": str(exc),
                        # 目标本身不存在 → 监督代理必须立刻终止整个 DAG，
                        # 不能让下游（写邮件）拿着"查不到"继续跑。
                        **({"fatal_error": True} if getattr(exc, "fatal", False) else {}),
                        **meta,
                        **task_status_meta(TASK_STATUS_FAILED, str(exc)),
                    }
                ]
            }

    # Generic life-service question / map query: use the small tool-using loop.
    user_id = state.get("user_id")
    if not isinstance(db_session, AsyncSession):
        tool_box = LifeTool(None, settings, default_city=session_destination)
    else:
        tool_box = LifeTool(
            db_session,
            settings,
            default_city=session_destination,
            user_id=user_id,
        )

    tools = tool_box.tools()
    tools_by_name = {t.name: t for t in tools}
    system_text = (
        "你是 LifePilot 生活服务代理。请使用工具回答天气、地图与出行问题，"
        "回复要简短、实事求是，不要使用 Markdown。"
        "天气与地图数据已接入高德（Amap），中国境内可直接使用中文城市名"
        "（如上海、北京、杭州），无需转换成 adcode 或经纬度。"
        "【地图查询必须限定城市】调用 `get_map_context` 时始终传 `city`"
        "（本轮点名的城市或下面给出的会话城市）。不限定城市时高德会把同名地点"
        "匹配到别的城市（「外滩」会命中武汉/福州），具体景点也可能查不到坐标。"
        "【你具备路径规划能力】`get_route_planning` 可以用经纬度规划步行/公交地铁/"
        "驾车路线并返回距离与耗时。整理行程、给景点排序时，先用 `get_map_context`"
        "查到景点坐标，再调用 `get_route_planning` 算相邻景点之间的通行时间，"
        "把耗时写进建议里。**不要再声称自己没有路线规划能力。**"
        "【景点评分要引用真实数据】`get_map_context` 返回的每个地点可能带 `rating`"
        "（评分）、`cost`（人均）、`open_time`、`tag`。有就引用具体数值；"
        "字段为 null 就如实说「高德暂无评分/点评」，**严禁**自己编 4.8 分这类数字。"
        "【需要时把行程落库】只有用户明确说「记下来 / 加到待办 / 建个任务」时，"
        "才调用 `create_tasks`（整份行程多节点）或 `create_task`（单条）落库；"
        "只是咨询行程时不要建任务。相对日期必须结合下面给的「今天 / 未来 7 天」换算。"
        "【时间对齐死命令】天气工具返回的 `forecasts` 是逐日预报，"
        "`temperature_c`/`condition` 是**当前实时天气**。用户问明天/后天/未来几天时，"
        "必须从 `forecasts` 里挑出对应日期并引用其 daytemp/nighttemp/dayweather；"
        "若 `forecasts` 中没有那一天，必须如实说明「暂无该日预报」，"
        "**严禁**把当前实时实况说成那一天的预报。"
        f"{NUMERIC_RANGE_RULE}"
        f"{CITY_CONSISTENCY_RULE}"
        f"{ROUTE_ORIGIN_RULE}"
        f"{NO_REPEAT_RULE}"
    )
    if session_destination:
        # 跨轮追问只给了地名、没给城市：明确告知模型城市范围，别自己去猜。
        system_text += (
            f"\n【会话已确认城市】用户此前提过地点是「{session_destination}」。"
            f"查询地名时一律限定在{session_destination}，不要按同名地点到其他城市查找。"
        )
    system_text += f"\n{render_time_reference(tz_name_of(settings))}"
    upstream_context = str(state.get("upstream_context") or "")
    if upstream_context:
        system_text = f"{system_text}\n{upstream_context}\n"
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": intent or "请帮助处理环境或生活信息查询。"},
    ]

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    base_messages = [
        SystemMessage(content=messages[0]["content"]),
        HumanMessage(content=messages[1]["content"]),
    ]
    work_messages = base_messages
    llm_with_tools = llm.bind_tools(tools)

    tool_errors: list[str] = []

    for _ in range(MAX_LIFE_TOOL_ROUNDS):
        response = await astream_invoke(llm_with_tools, work_messages)
        if not isinstance(response, AIMessage) or not response.tool_calls:
            text = response.content if isinstance(response.content, str) else str(response.content)
            # 工具报错但模型仍给了答复 → 部分完成；工具全挂且无答复 → 失败。
            if not tool_errors:
                status = TASK_STATUS_SUCCESS
            elif text.strip():
                status = TASK_STATUS_PARTIAL
            else:
                status = TASK_STATUS_FAILED
            return {
                "agent_results": [
                    {
                        "route": "life",
                        "intent": intent,
                        "result": text,
                        **meta,
                        **task_status_meta(
                            status,
                            tool_errors[0][:300] if tool_errors else None,
                        ),
                    }
                ],
                **step_result_update(task, text, status),
            }
        work_messages.append(response)
        for call in response.tool_calls:
            tool_name = call.get("name", "")
            call_id = call.get("id", "")
            args = call.get("args") or {}
            target = tools_by_name.get(tool_name)
            try:
                output = await target.ainvoke(args) if target else "未知工具"
            except Exception as exc:
                output = f"工具错误：{exc}"
                tool_errors.append(output)
            work_messages.append(ToolMessage(content=output, tool_call_id=call_id))

    return {
        "agent_results": [
            {
                "route": "life",
                "intent": intent,
                "result": "我暂时无法回答这个生活服务请求。",
                **meta,
                **task_status_meta(
                    TASK_STATUS_FAILED if tool_errors else TASK_STATUS_PARTIAL,
                    tool_errors[0][:300] if tool_errors else "工具轮次耗尽仍未回答",
                ),
            }
        ]
    }