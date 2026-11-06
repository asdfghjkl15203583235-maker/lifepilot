"""Small shared helpers for agent nodes."""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# 上下文时间隔离死命令——注入分类器、direct、规划器的 system prompt。
# 放在 common 里做唯一来源：orchestrator 与 planner 互相 import 会成环。
CONTEXT_ISOLATION_RULE = (
    "【执行隔离】只能把用户**最新一条**消息中的写操作/定时任务当作本轮执行目标。"
    "历史消息里的提醒、邮件草案、日程创建、任务修改都已执行完毕，"
    "绝对不能重复执行或重复确认。"
    "【上下文延续 — 同等重要】历史对话同时是本会话的上下文：用户此前陈述的"
    "持续性事实（出差/旅行目的地、所在地、同行人、计划、偏好、正在讨论的主题）"
    "属于**已知信息**，本轮必须直接沿用，绝不能反问用户已经说过的话。"
    "例如用户两轮前说「我要去上海」，本轮问「推荐穿什么衣服」，"
    "应当默认是在问上海，而不是回答「要看你去哪儿」。"
    "【意图必须独立判定】最新消息是闲聊、常识提问或生活查询时，只能归为 "
    "direct 或 life，不得继承上一轮的 email/calendar/notion 写意图。"
    "【回答范围】最终回复只回应最新一条消息；不要复述上一轮已交付的结果，"
    "除非用户本轮明确要求回顾。"
)

# 口头确认 ≠ 卡片确认。用户打字说"ok/可以/确认"只是同意去做，不代表点击了
# 前端确认卡片；HITL 执行只在 /approve 接口里发生，本轮工具记录中不可能出现
# status=approved 的发送回执。写进 system prompt 让模型别自我脑补。
HITL_VERBAL_CONFIRM_RULE = (
    "【口头确认无效】用户在聊天框打字说「可以」「ok」「确认」「发吧」等，"
    "**不等于**点击了界面上的确认卡片，也**不会**触发任何真实发送。"
    "只有前端卡片上的「确认执行」按钮才会真正执行邮件发送/任务变更。"
    "因此本轮永远没有发送成功的证据：只要用户只发了文字，你就必须重新提交"
    "（或指出已有）确认草案，并请用户点击卡片，绝不能声称「已发送」「已回复」。"
)

# 数值区间的写法。后端拼装天气/价格时一律用「至」，system prompt 里也要挡住
# 模型自由发挥：成对出现的 `~` 会被前端 Markdown 当成删除线，整句被划掉。
NUMERIC_RANGE_RULE = (
    "【区间必须写「至」】温度、价格等数值区间一律写成「17至19℃」「20至30元」，"
    "**禁止**用 `~`、`～`、`-` 连接两个数字——成对的 `~` 会被前端 Markdown "
    "渲染成删除线，整句会被划掉。"
)

# 同一轮多个生活服务工具必须同城。真实 badcase：用户问"今天的天气 + 找个去处"，
# 天气解析出北京、地点查询却回落到配置默认城市上海，回复里两个城市互相打架，
# 模型只好把内心纠结原样念给用户听。
CITY_CONSISTENCY_RULE = (
    "【城市一致性 — 同一轮硬规则】同一轮里的天气、地点、路线工具必须使用**同一个**城市："
    "优先用户本轮点名的城市，其次会话已确认的城市；配置里的默认城市只是毫无线索时的"
    "最后兜底，**严禁**把它当成用户的实际所在城市。"
    "如果某次工具返回的结果城市与用户所在城市不符，必须按正确城市重新查询，"
    "而不是把错误结果原样罗列给用户。"
    "【不要暴露内部决策】回复里严禁出现「这个分支」「推荐工具」「没有采用」「返回的是」"
    "这类内部执行叙述；工具之间结果冲突时自己解决，只给用户结论与建议。"
)

# 追问"算路程"时必须有真实起点，且目的地必须是上一轮推荐的那个地点。
# 真实 badcase：助手问"需要我算到门口的路程吗"，用户回"需要"，模型却给出
# 榆次老城东门→榆次站的无关路线，既没问起点、也没用上一轮推荐的图书馆/自习室。
ROUTE_ORIGIN_RULE = (
    "【算路程必须有起点】调用 get_route_planning 前必须知道起点；用户没给出发位置时，"
    "必须先反问「你从哪儿出发」（或请用户授权定位），严禁拿无关地点凑一条路线。"
    "上一轮推荐过的具体地点（图书馆/自习室等）就是本轮要算路程的目的地，"
    "不得换成别的景点或车站。"
)

# 追问只答最新问题，不把上一轮交付过的完整信息再播报一遍。
NO_REPEAT_RULE = (
    "【不要重复已交付信息】用户追问时只回答最新问题，不要重复播报上一轮已经给过的"
    "天气、推荐列表等完整内容；确有需要时只补一句最关键的变化。"
)

# 发送/回复"成功声称"词。HITL 架构下草案阶段绝无 sent/replied 回执，
# 因此这类声称在 agent 节点内永远是幻觉，必须机械拦截。
SEND_CLAIM_MARKERS = (
    "已发送", "发送成功", "已发出", "已寄出", "已发送给", "已回复", "回复成功",
    "已经发送", "已经回复", "sent", "replied", "delivered", "已发出邮件",
)

# 精确形态：只认「声称发送这个动作已完成」，放过「已发送的邮件」「已发送列表」
# 这类名词用法（用户查看已发送文件夹时，正常回答会包含这些词）。
# 「邮件已按草稿发送」这类中间插话的说法也要命中，因此允许已→动词之间有少量修饰，
# 但用标点截断，避免把「已准备好草稿，等待你确认后发送」误判成声称。
_SEND_CLAIM_RE = re.compile(
    r"已(?:经)?[^，。！？、；：\n]{0,8}?(?:发送|发出|寄出|回复)"
    r"(?!的|记录|(?:邮件)?(?:列表|文件夹|箱))"
    r"|(?:发送|回复)成功"
    r"|\b(?:have|has|had|already)\s+(?:been\s+)?sent\b"
    r"|\bwas\s+sent\b",
    re.IGNORECASE,
)


def contains_send_claim(text: str) -> bool:
    """True only when the text claims the send already happened.

    Deliberately narrower than `SEND_CLAIM_MARKERS`: a bare substring check
    would rewrite legitimate answers to "show me my sent folder".
    """
    return bool(_SEND_CLAIM_RE.search(text or ""))

# 短追问继承死命令——注入分类器与规划器的 system prompt。
# 上一轮刚查过天气/日历/邮件，用户紧接着说"那后天呢""重查一下"这类不含新
# 主题的短追问时，必须继承上一轮的路由与工具，严禁被降级成 direct 闲聊。
# 分类器只看到最新一条消息，无法自己知道上一轮在哪条路由上，所以这条规则
# 由 core_intent（携带 anchor 路由）配合代码兜底一起保证。单一来源在 common，
# orchestrator 与 planner 互相 import 会成环。
FOLLOWUP_INHERITANCE_RULE = (
    "【追问必须继承路由】如果用户最新消息是一句简短追问或指代词（如"
    "「那后天呢」「重查一下」「换成北京呢」），它没有包含新主题，只是在继续"
    "上一轮的话题。此时必须继承上一轮的路由与工具（例如上一轮查天气，本轮"
    "仍是查天气），**严禁**降级为 direct 闲聊，更不得回答「没有可用的天气工具」。"
    "只有用户明确提出了新的、可独立成立的请求时，才允许切换路由。"
)


# 「本应用里的任务」的说法。分类器/规划器曾把这些判给 notion（因为 notion 的
# 路由描述里也写了"任务"），结果用户让标记完成待办，agent 跑去 Notion 工作区
# 搜索，回一句"没找到它"。提示词已写明规则，但 LLM 对"任务"二字的联想太强，
# 这里提供确定性兜底。放在 common 里做唯一来源：orchestrator 与 planner 互相
# import 会成环。
_LOCAL_TASK_RE = re.compile(
    r"待办|任务|to-?do|提醒|截止|到期|逾期|due\s*date|标记.{0,4}完成|标为完成|"
    r"改期|延期|改截止|删除.{0,6}(任务|待办)",
    re.IGNORECASE,
)
# 只要用户明说了 Notion 相关词汇，就尊重 notion 路由，不做纠偏。
_NOTION_MENTION_RE = re.compile(r"notion|工作区|workspace|数据库|页面|区块|block", re.IGNORECASE)


def looks_like_local_task_management(text: str) -> bool:
    """用户是否在说"本应用里的任务"，且没有提 Notion。"""
    if not text or _NOTION_MENTION_RE.search(text):
        return False
    return bool(_LOCAL_TASK_RE.search(text))


def tz_name_of(settings: Any) -> str:
    """User-local IANA timezone from app settings (default UTC)."""
    tz = getattr(settings, "planner_timezone", None)
    if not isinstance(tz, str) or not tz:
        return "UTC"
    return tz


_CN_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_CN_FULL_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# 时间锚点死命令 —— 随【当前时间】一起注入所有 agent，严禁模型自己猜日期/星期。
TIME_ANCHOR_RULE = (
    "【时间基准 — 硬性规则】你必须严格以【当前时间】为基准推算所有相对日期"
    "（今天/明天/后天/周几/下周X）。当用户询问“今天”时，只能基于今天作答；"
    "若当前条件不满足（例如今天已过、无当日数据），必须明确告知用户，"
    "并询问是否改期，严禁擅自将回答推演至未来日期。"
)


def _local_now(tz_name: str, now: datetime | None = None) -> datetime:
    if now is not None:
        return now
    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError):
        tz = ZoneInfo("UTC")
    return datetime.now(tz)


def render_now_reference(tz_name: str = "UTC", *, now: datetime | None = None) -> str:
    """当前精确时间锚点：【当前时间】：YYYY-MM-DD HH:MM，星期X。

    所有 agent 的 System Prompt 开头都注入这一行，相对时间一律以它为基准，
    禁止模型从训练数据里猜"今天是哪天/周几"。
    """
    current = _local_now(tz_name, now)
    return (
        f"【当前时间】：{current.strftime('%Y-%m-%d %H:%M')}，"
        f"{_CN_FULL_WEEKDAYS[current.weekday()]}"
    )


def render_upcoming_days(tz_name: str = "UTC", *, days: int = 7, now: datetime | None = None) -> str:
    """未来 N 天的「月-日 星期X」查表，供"明天/周六/下周一"直接查表换算。"""
    current = _local_now(tz_name, now)
    upcoming = [
        f"{(current + timedelta(days=offset)).strftime('%m-%d')}"
        f" {_CN_FULL_WEEKDAYS[(current + timedelta(days=offset)).weekday()]}"
        for offset in range(1, max(1, days) + 1)
    ]
    return "未来 " + str(max(1, days)) + " 天：" + "；".join(upcoming)


def render_time_reference(tz_name: str = "UTC", *, days: int = 7, now: datetime | None = None) -> str:
    """时间锚点 + 未来查表 + 死命令，一次注入所有 agent 的 System Prompt。"""
    return "\n".join(
        (
            render_now_reference(tz_name, now=now),
            render_upcoming_days(tz_name, days=days, now=now),
            TIME_ANCHOR_RULE,
        )
    )


def render_date_reference(tz_name: str = "UTC", *, days: int = 7) -> str:
    """Anchor for relative dates: 今天 + 未来 N 天（含周几）。

    HITL 场景里用户说「周六 09:00 天安门」，模型必须把它换算成绝对日期。
    只给一个英文日期字符串时，模型经常算错周次；把未来 7 天的「月-日 周X」
    直接列出来，映射就变成查表而不是推算。
    """
    today = _local_now(tz_name)
    lines = [
        f"今天：{today.strftime('%Y-%m-%d')}（{_CN_WEEKDAYS[today.weekday()]}）"
    ]
    upcoming = [
        f"{(today + timedelta(days=offset)).strftime('%m-%d')}"
        f" {_CN_WEEKDAYS[(today + timedelta(days=offset)).weekday()]}"
        for offset in range(1, max(1, days) + 1)
    ]
    lines.append("未来 " + str(max(1, days)) + " 天：" + "；".join(upcoming))
    return "\n".join(lines)


def resolve_local_datetime(value: Any, tz_name: str = "UTC") -> datetime | None:
    """Interpret a naive/aware ISO string or datetime in the user-local tz as an aware UTC instant.

    Naive values are assumed to be wall-clock time in *tz_name* (e.g. "10:00" in
    Asia/Shanghai → 02:00Z). Aware values are converted straight to UTC.
    """
    if not value:
        return None
    tz = ZoneInfo(tz_name or "UTC")
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def runtime_context(runtime: Runtime | None) -> dict[str, Any] | None:
    """Return run-scoped values injected through LangGraph Runtime.context."""
    if runtime is None:
        return None
    context = getattr(runtime, "context", None)
    if isinstance(context, dict):
        return context
    return None


def runtime_value(
    state: dict[str, Any],
    runtime: Runtime | None,
    key: str,
) -> Any:
    """Read a run-scoped value, preferring Runtime.context over legacy state."""
    context = runtime_context(runtime)
    if context is not None and key in context:
        return context[key]
    return state.get(key)


def result_meta(task: dict[str, Any]) -> dict[str, Any]:
    """Attach routing metadata to an agent result for supervisor tracking."""
    attempts = int(task.get("retry_count") or 0)
    return {
        "task_key": task.get("task_key"),
        "attempt": attempts,
    }


# ── 结构化执行状态 ────────────────────────────────────────────────────────
# 执行类 Agent 必须在结果里带上明确的成败标记，供监督代理判定是否需要重规划。
# 只靠关键词（"失败""无法"）会误伤正常回答——例如查天气成功但正文里写了
# "无法确定明天是否下雨"，就会被当成执行失败并触发重试。
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_PARTIAL = "partial"


def task_status_meta(status: str, error: str | None = None) -> dict[str, Any]:
    """Build the structured status payload every executable agent must attach."""
    payload: dict[str, Any] = {"task_status": status}
    if error:
        payload["error"] = error
    return payload


def step_result_update(
    task: dict[str, Any],
    result: str,
    status: str = TASK_STATUS_SUCCESS,
) -> dict[str, Any]:
    """Wrap a finished step's output so LangGraph merges it into `step_results`.

    Only successful work is recorded: a failed step must not feed its error text
    into downstream prompts as if it were real data.
    """
    if status == TASK_STATUS_FAILED:
        return {}
    key = step_result_key(task)
    if not key or not (result or "").strip():
        return {}
    return {"step_results": {key: result}}


def step_result_key(task: dict[str, Any]) -> str:
    """The key under which a task's output is stored in `step_results`."""
    return str(task.get("task_key") or task.get("persisted_task_id") or task.get("id") or "")


def upstream_results_context(
    task: dict[str, Any],
    step_results: dict[str, str] | None,
    tasks: list[dict[str, Any]] | None = None,
) -> str:
    """Render the outputs of a task's `depends_on` predecessors as prompt text.

    The planner rewrites `depends_on` to persisted task keys, so a direct lookup
    normally hits; the id fallback keeps hand-built plans working.
    """
    if not step_results:
        return ""
    by_key: dict[str, dict[str, Any]] = {}
    for item in tasks or []:
        if item.get("task_key"):
            by_key[str(item["task_key"])] = item
        if item.get("id"):
            by_key.setdefault(str(item["id"]), item)
    lines: list[str] = []
    for dep in task.get("depends_on") or []:
        dep_id = str(dep)
        producer = by_key.get(dep_id)
        output = step_results.get(dep_id)
        if not output and producer is not None:
            output = step_results.get(step_result_key(producer))
        if not output:
            continue
        label = str((producer or {}).get("intent") or dep_id)
        lines.append(f"[上游步骤 {label}] 的结果：\n{output.strip()}")
    if not lines:
        return ""
    return (
        "以下是前置步骤已经完成并返回的真实结果，请直接依据这些内容完成当前步骤，"
        "不要重新查询或编造：\n\n" + "\n\n".join(lines)
    )


# ── 情绪识别（Bug 3）─────────────────────────────────────────────────────
# 真实 badcase：用户说"今天心情崩溃"，助手照常播报天气，冷得像没听见。
# 安抚必须是确定性兜底，不能指望合并提示词里的 LLM 良心发现——但安抚只是
# 一句开场白，用户真正问的信息该给还得给。
EMOTIONAL_DISTRESS_RE = re.compile(
    r"(崩溃|快崩溃|要崩溃|熬不住|受不了|撑不住|扛不住|想哭|哭了|难过|伤心|"
    r"心情很差|心情不好|心情低落|情绪低落|很丧|绝望|焦虑|压力大|压力很大|"
    r"心累|沮丧|失落|烦躁|郁闷|压抑|委屈|无助|emo|抑郁|失眠|难受)",
    re.IGNORECASE,
)
_COMFORT_LINE = "抱抱你，先深呼吸，别太逼自己。"


def detect_emotional_distress(text: str) -> bool:
    """用户最新消息是否在表达情绪低落/崩溃（需要先安抚，再给信息）。"""
    return bool(EMOTIONAL_DISTRESS_RE.search(text or ""))


def emotional_comfort_prefix(text: str) -> str:
    """情绪化输入的确定性开场白；不需要安抚时返回空串。

    不做心理诊断，也不替代正文：只加一句共情，天气/日程该报的照报。
    """
    if not detect_emotional_distress(text):
        return ""
    return f"{_COMFORT_LINE}\n\n"


def render_blocked_step_notice(
    intent: str,
    upstream_intent: str,
    reason: str = "",
) -> str:
    """上游失败导致下游被跳过时的用户提示（含"是否取消"的选择）。

    真实 badcase：问"火星天气"查不到，助手仍用别处的数据写了邮件。下游步骤
    绝不能在上游没有真实数据时继续跑——跳过并让用户决定是否取消。
    """
    name = (intent or "后续步骤").strip()
    upstream = (upstream_intent or "上游步骤").strip()
    detail = " ".join((reason or "").split())[:120]
    tail = f"（{detail}）" if detail else ""
    return (
        f"「{name}」已暂停：上游「{upstream}」没有拿到数据{tail}，"
        f"为避免用到错误信息，这一步没有执行。是否取消「{name}」？"
    )


async def mark_task_in_progress_if_persisted(
    state: dict[str, Any],
    task: dict[str, Any],
    runtime: Runtime | None = None,
) -> None:
    """Mark a persisted task IN_PROGRESS when an agent starts executing it."""
    persisted_task_id = task.get("persisted_task_id")
    db_session = runtime_value(state, runtime, "db_session")
    user_id = state.get("user_id")
    if not persisted_task_id or not isinstance(db_session, AsyncSession) or not user_id:
        return

    from lib.enums import TaskStatus
    from schemas.task import TaskUpdateRequest
    from services.task_service import update_task

    try:
        await update_task(
            db_session,
            task_id=persisted_task_id,
            user_id=user_id,
            body=TaskUpdateRequest(status=TaskStatus.IN_PROGRESS),
        )
        logger.info("Task %s marked in_progress", persisted_task_id)
    except Exception as exc:
        logger.warning("Task in-progress write failed for %s: %s", persisted_task_id, exc)