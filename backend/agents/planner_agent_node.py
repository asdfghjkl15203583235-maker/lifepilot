"""规划器节点 — 将用户请求拆解为带依赖关系的子任务。

当前编排流程先分类高层意图，再分发给对应的领域代理。LifePilot 在
这之上增加了规划器层：请求会被拆解为带依赖、优先级和持久化
`plan_id` 的明确步骤，从而使同一计划可见于任务看板，并可在某一步
失败后由监督代理重新规划。
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from langgraph.runtime import Runtime

from agents.core_intent import ROUTE_DIRECT, ROUTE_LIFE, render_core_intent
from agents.common import (
    CONTEXT_ISOLATION_RULE,
    looks_like_local_task_management,
    resolve_local_datetime,
    runtime_value,
    tz_name_of,
)
from agents.state import AgentState
from agents.travel_planning import (
    build_travel_steps,
    extract_travel_destination,
    extract_travel_duration,
    is_travel_request,
)
from config import Settings
from lib.enums import TaskPriority, TaskProject, TaskStatus
from models.task import Task
from schemas.task import TaskCreateRequest
from services.memory_service import get_planner_rules
from services.reminder_service import schedule_deadline_reminder
from services.schedule_conflict_service import (
    find_schedule_conflicts,
    render_conflict_notice,
)
from services.task_project_classifier import classify_task_project, validate_task_project
from services.task_service import create_task_for_user
from services.task_source_classifier import classify_task_source

logger = logging.getLogger(__name__)

_CONTEXT_ISOLATION_RULE = CONTEXT_ISOLATION_RULE

_TIMELINE_LABEL_RE = re.compile(r"^\[[^\]]{1,24}\]\s*(?:user|assistant|用户|助手)\s*[:：]\s*")

# 引用上游结果的句式：「根据天气写邮件」「结合查到的信息…」「基于上面的结果」。
# 命中即说明这一步消费前序产出，必须建立 depends_on，否则上游失败也照跑。
_UPSTREAM_REFERENCE_RE = re.compile(
    r"(根据|结合|基于|按照|参考|依据|利用|用|拿|把).{0,12}"
    r"(天气|预报|气温|结果|内容|邮件|日程|信息|数据|上面|上述|前面|刚才)"
    r"|(根据|结合|基于|按照|参考|依据)(它|这个|那个|这些|那些|上一步|上一条)"
)

PLANNER_SYSTEM_PROMPT = """\
你是 LifePilot 规划器。请把用户的最新请求拆解成一个小型、
带依赖感知、可由并行子代理执行的计划。

每一步必须使用以下路由之一：
- "email"    → 阅读、发送、搜索、起草、回复或管理邮件
- "calendar" → 查看、创建、更新、删除**本地日历**里的日程与带截止时间的待办，
  查询会议议程（本地日历已连接，不需要 Google 授权）
- "notion"   → 仅当用户明确提到 Notion / 工作区 / 页面 / 数据库 / 区块 / 评论时
- "direct"   → 普通对话、无需工具路由的问题、生命周期操作，**以及本应用的本地任务管理**
  （查看/新增/改期/标记完成/删除待办、设提醒）
- "life"     → 天气、地图及其他外部生活服务查询

【notion 不等于任务】用户说"任务/待办/to-do"且没提 Notion 时指本地任务列表，必须是 "direct"。

规则：
- 只把真实工作拆成步骤，不要编造无效工作。
- 用 id 表达依赖；一个步骤可以依赖 0 个、1 个或几个更早的步骤。
- 每个步骤在其依赖执行完成后，都应能独立执行。
- 只要可能，优先使用并行步骤（无依赖）。
- 根据紧急程度和影响分配优先级：low、medium、high、urgent。
- **`intent` 必须是纯动作，绝不包含时间状语、日期、时刻或"提醒我/设置XX提醒"字样。**
  时间状语（"上午 10 点""明天早上八点""下周二下午三点"）必须迁移到 `due_at`。
- **`due_at` 必须用用户本地时区的墙壁时间（naive，无 Z、无 +08:00 后缀）表达，且必须生成绝对时间戳**（"YYYY-MM-DDTHH:MM:SS"）。按下列优先级解析：
  - 明确的时刻或相对时刻（"明天早上八点""上午 10 点""下周二下午三点"）→ 按字面钟点换算成绝对的 YYYY-MM-DDTHH:MM:SS；
  - **模糊时间词 "一会/稍后/过会/等会/待会/晚点/回头" → 用今天的时间加 1 小时**；
  - **"明天/后天" 仅给日期、未给钟点 → 该日 09:00（"明天"→ 明天 09:00，"后天"→ 后天 09:00）**；
  - 仅给日期未给钟点（"10月25日"）→ 该日 09:00；完全无法推断具体时间的非常规请求才设为 null。
- 例如 "上午10点去库迪咖啡买拿铁" → `intent` 必须是 "去库迪咖啡买拿铁"，
  `due_at` 是今天 10:00（naive）。"设置明天早上八点的简历修改提醒" → `intent` 必须是 "修改简历"，
  `due_at` 是明天 08:00（naive）。"稍后提醒我交水电费" → `intent` 必须是 "交水电费"，
  `due_at` 是当前时间 +1 小时（naive）。
- **`project` 必须根据任务语义自动归类，只能返回以下枚举之一："工作"、"生活"、"学习"、"其他"。**
  工作相关（写周报/邮件/简历/会议/项目/报表等）→ "工作"；日常事务/家务/健康/购物/缴费等 → "生活"；
  课程/读书/练习/考证/听讲座等 → "学习"；判断不出来时 → "生活"。
- 如果没有必要规划，返回一个简短的 "direct" 步骤即可。

只回复 JSON 对象，不要使用 Markdown：
{{
  "plan_id": "<该计划的稳定标识>",
  "summary": "<一句话计划摘要>",
  "tasks": [
    {{
      "id": "step-1",
      "intent": "<纯动作描述，不含任何时间状语>",
      "route": "email|calendar|notion|direct|life|search",
      "priority": "low|medium|high|urgent",
      "project": "工作|生活|学习|其他",
      "depends_on": ["step-0"],
      "due_at": "YYYY-MM-DDTHH:MM:SS 或 null（用户本地时间，naive，无 Z）"
    }}
  ]
}}

今天的本地日期和时间（用户时区 {tz_name}）：{today}

{context_isolation_rule}

{user_rules}
"""

_MAX_ROUTE_FALLBACK = "direct"


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [line for line in cleaned.split("\n") if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    return json.loads(cleaned)


def _parse_priority(value: Any, default: TaskPriority = TaskPriority.MEDIUM) -> TaskPriority:
    try:
        return TaskPriority(str(value).lower())
    except (ValueError, TypeError):
        return default


def _validate_route(route: Any) -> str:
    if route not in {"email", "calendar", "notion", "direct", "life", "search"}:
        return _MAX_ROUTE_FALLBACK
    return str(route)


def _plan_to_tasks(payload: dict[str, Any], user_id: uuid.UUID) -> tuple[str, list[dict[str, Any]]]:
    raw_tasks = payload.get("tasks") or []
    plan_id = str(payload.get("plan_id") or uuid.uuid4())
    known_ids = {str(item.get("id", "")) for item in raw_tasks if item.get("id")}
    tasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_tasks[:8]):
        step_id = str(item.get("id") or f"step-{index + 1}")
        depends = [str(dep) for dep in (item.get("depends_on") or []) if str(dep) in known_ids]
        intent = str(item.get("intent") or "计划步骤")[:400]
        raw_project = item.get("project")
        tasks.append(
            {
                "id": step_id,
                "intent": intent,
                "route": _validate_route(item.get("route")),
                "priority": _parse_priority(item.get("priority")),
                "project": validate_task_project(raw_project) or classify_task_project(intent),
                "depends_on": depends,
                "due_at": item.get("due_at") or None,
                "plan_id": plan_id,
                "source_metadata": {"planner": "life_pilot", "plan_summary": str(payload.get("summary") or "")[:200]},
                "created_by_plan": True,
            }
        )
        if not tasks[-1]["intent"].strip():
            tasks[-1]["intent"] = f"计划步骤 {index + 1}"
    if not tasks:
        tasks = [
            {
                "id": "step-1",
                "intent": "普通对话",
                "route": "direct",
                "priority": TaskPriority.MEDIUM,
                "project": TaskProject.LIFE.value,
                "depends_on": [],
                "due_at": None,
                "plan_id": plan_id,
                "source_metadata": {"planner": "life_pilot", "plan_summary": ""},
                "created_by_plan": True,
            }
        ]
    return plan_id, tasks


_TEMPORAL_CLUE_RE = re.compile(
    r"((上|今|明|后|下)天(早上|上午|中午|下午|晚上)?|"
    r"(今晚|今夜|明早|明天早上|后天|大后天|"
    r"周一|周二|周三|周四|周五|周六|周日|周天|"
    r"星期一|星期二|星期三|星期四|星期五|星期六|星期日|"
    r"下周[一二三四五六日天]?|"
    r"(上午|早上|下午|中午|晚上|深夜|凌晨)|"
    r"一会|一会儿|稍后|过会|过一会|等会|待会|晚点|回头|马上|尽快|"
    r"[0-9零一壹二两叁三肆四伍五陆六柒七捌八玖九拾十]{1,2}\s*[点时]\s*[0-9零一壹二两叁三肆四伍五陆六柒七捌八玖九拾十]{0,2}分?|"
    r"[0-9]{1,2}月[0-9]{1,2}日|"
    r"tomorrow|tonight|this afternoon|next week|\bat\s+\d{1,2}|later today))"
)


def _has_temporal_clue(messages: list) -> bool:
    """最新消息是否含时间线索——单步也要走 LLM 剥离时间状语到 due_at。"""
    if not messages:
        return False
    latest = messages[-1].content if hasattr(messages[-1], "content") else ""
    return bool(_TEMPORAL_CLUE_RE.search(latest or ""))


# 模糊时间词 → 当前时间 +1 小时；(词, 后天数) → 该日 09:00（无钟点兜底）。
_FUZZY_TIME_WORDS = ("一会", "一会儿", "稍后", "过会", "过一会", "等会", "待会", "晚点", "回头", "马上", "尽快")
_DAY_SHIFT_WORDS: dict[str, int] = {
    "今天": 0,
    "今日": 0,
    "今晚": 0,
    "明天": 1,
    "明日": 1,
    "明早": 1,
    "后天": 2,
    "大后天": 3,
}
# 明确钟点：阿拉伯或中文数字 + 点/时（"8点""八点""08:30""下午三点"）→ 有钟点不兜底。
_EXPLICIT_CLOCK_RE = re.compile(
    r"\d{1,2}[:.]\d{2}|"
    r"[0-9零一壹二两叁三肆四伍五陆六柒七捌八玖九拾十]{1,2}\s*[点时]"
)
_MONTH_DAY_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")


def _infer_due_at_local(text: str, now: datetime, tz_name: str) -> datetime | None:
    """LLM 未给 due_at 时的代码兜底：模糊词→+1h、明天/后天→该日 09:00。

    返回 naive 本地墙钟（呼应用户本地时区），交给 resolve_local_datetime 转 UTC。
    完全无法推断才返回 None（保持无截止时间）。
    """
    clean = (text or "").strip()
    if not clean:
        return None
    if any(word in clean for word in _FUZZY_TIME_WORDS):
        return now.replace(second=0, microsecond=0) + timedelta(hours=1)
    for day_word, shift in _DAY_SHIFT_WORDS.items():
        if day_word in clean:
            # 已带具体钟点 → 交由 LLM 的精确 due_at；只兜底"纯日期无钟点"。
            if _EXPLICIT_CLOCK_RE.search(clean):
                continue
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            return target + timedelta(days=shift)
    m = _MONTH_DAY_RE.search(clean)
    if m and not _EXPLICIT_CLOCK_RE.search(clean):
        try:
            return now.replace(
                month=int(m.group(1)),
                day=int(m.group(2)),
                hour=9,
                minute=0,
                second=0,
                microsecond=0,
            )
        except ValueError:
            return None
    return None


def _strip_temporal_prefix(title: str) -> str:
    """把残留时间状语从 title 剥离的兜底清洗（LLM 偶发仍会留下时间词）。

    多 pass 删除任意位置的时间短语（"上午10点去…"→"去…"、"明天早上八点的…"→
    先删"明天早上"再删"八点"），再剥掉"设置…提醒/提醒我…"框架词，最终只留动作。
    全剥空才回退原文，避免把纯时间任务误清空。真正的剥离主力是 Prompt 规则，
    这里只做最后防线。
    """
    text = (title or "").strip()
    cleaned = text
    for _ in range(4):
        cleaned = _TEMPORAL_CLUE_RE.sub("", cleaned).strip()
        cleaned = re.sub(r"[，, 、]+$", "", cleaned).strip()
        if cleaned == text or not cleaned:
            break
        text = cleaned
    # 剥框架："设置X提醒/预约X安排/提醒我X" → 只留动作。
    cleaned = re.sub(r"^(设置|预约|安排|提醒我)的?\s*", "", cleaned).strip()
    cleaned = cleaned.lstrip("的，, 、").strip()
    # 收尾："简历修改提醒"→"修改简历"（把尾部"提醒/闹钟"标记去掉）。
    cleaned = re.sub(r"^(.*?)(提醒|闹钟)$", r"\1", cleaned).strip()
    cleaned = re.sub(r"的$", "", cleaned).lstrip("的").strip("，, 、").strip()
    return cleaned if cleaned else (title or "").strip()


def _is_plan_applicable(messages: list) -> bool:
    if not messages:
        return False
    latest = messages[-1].content if hasattr(messages[-1], "content") else ""
    lowered = latest.lower() if isinstance(latest, str) else ""
    multi_step_terms = (
        "then",
        "after that",
        "and then",
        "first",
        "next",
        "finally",
        "以及",
        "然后",
        "之后",
        "先",
        "再",
        "最后",
    )
    planning_verbs = ("plan", "schedule", "organize", "prepare", "安排", "计划", "准备", "整理")
    return any(term in lowered for term in multi_step_terms) or any(
        term in lowered for term in planning_verbs
    )


EXECUTABLE_ROUTES = {"email", "calendar", "notion", "life"}


def _build_plan_tasks(
    state: AgentState,
    plan_id: str,
    llm: ChatOpenAI,
    messages: list,
    parsed: dict[str, Any],
) -> list[dict[str, Any]]:
    """多步骤走规划 LLM；单步工具请求直接复用编排器分类结果。"""
    if _is_plan_applicable(messages) or not state.get("tasks"):
        return parsed.get("tasks") or _plan_to_tasks({"tasks": []}, state.get("user_id"))[1]

    # 编排器已经对 direct 去重/合并。这里保留全部意图（含 direct）——
    # 不能把 direct 过滤掉，否则"提醒我明早开会，顺便查个天气"这类混合
    # 意图会让提醒任务静默消失（direct 由 direct_reply 的工具真正落库）。
    classified = list(state.get("tasks", []))
    if not classified:
        return parsed.get("tasks") or _plan_to_tasks({"tasks": []}, state.get("user_id"))[1]

    return [
        {
            "id": f"step-{index + 1}",
            "intent": task.get("intent") or "执行任务",
            "route": task.get("route", "direct"),
            "priority": TaskPriority.MEDIUM,
            "project": classify_task_project(task.get("intent") or ""),
            "depends_on": [],
            "due_at": None,
            "plan_id": plan_id,
            "source_metadata": {"planner": "life_pilot", "plan_summary": ""},
            "created_by_plan": True,
        }
        for index, task in enumerate(classified[:8])
    ]


# 规划步骤默认只是**内部执行环节**（查天气、写邮件、查日历），绝不能出现在
# 用户的任务看板上。只有用户明确要求"提醒我 / 加到待办"时，步骤才是用户
# 可见的待办；即便如此，direct 步骤也由 create_reminder 工具统一落库，
# 规划器不再重复创建，避免同一件事出现两条任务。
_EXPLICIT_TODO_RE = re.compile(
    r"提醒我|提醒一下|记得提醒|叫我|喊我|通知我|"
    r"加到待办|加个待办|记到待办|新建任务|建个任务|创建任务|待办事项",
    re.IGNORECASE,
)


def is_explicit_todo_request(latest_user_text: str) -> bool:
    """用户是否明确要求"提醒我 / 加到待办"这类可见待办。"""
    return bool(_EXPLICIT_TODO_RE.search(latest_user_text or ""))


# 只读取数的步骤（查天气、翻邮件、看日历）永远只是执行手段，哪怕同一轮里
# 用户说了"提醒我"，这些步骤也不该变成看板上的一行。
_READ_ONLY_STEP_RE = re.compile(
    r"^\s*(查询|查看|查|搜索|检索|总结|浏览|列出|获取|读取|确认|定位|查找|检查)"
)


def is_read_only_step(intent: str) -> bool:
    return bool(_READ_ONLY_STEP_RE.search(intent or ""))


def _replan_instruction(replan_context: dict[str, Any]) -> str:
    """失败后重规划的唯一提示块：作废旧步骤，只重做失败部分。"""
    failed = replan_context.get("failed") or []
    completed = replan_context.get("completed") or []

    def _fmt(items: list[dict[str, Any]]) -> str:
        return "\n".join(
            f"- {item.get('intent') or '未命名步骤'}"
            + (f"（失败原因：{item['error']}）" if item.get("error") else "")
            for item in items
        ) or "（无）"

    return (
        "【重新规划，禁止原样重试】上一版计划中以下步骤执行失败，旧步骤已作废：\n"
        f"{_fmt(failed)}\n"
        f"已完成步骤（不得重复规划）：\n{_fmt(completed)}\n"
        "请针对失败的目标重新设计可替代的执行步骤，修正导致失败的原因；"
        "若判断该目标确实无法完成，返回一个 route=direct 的步骤说明原因。"
        "只输出新的步骤，不要包含已完成或未失败的步骤。"
    )


def expand_travel_tasks(
    tasks: list[dict[str, Any]],
    *,
    latest_user_text: str,
    core_intent: dict[str, Any] | None,
    plan_id: str,
    include_search: bool = False,
) -> list[dict[str, Any]]:
    """把旅行请求确定性拆成天气 / 景点 / 交通三路**无依赖**步骤。

    单个 direct/life 步骤拿不到任何真实数据，合并节点只能抄攻略。三路并行
    步骤由现有 `_fan_out` 同时调度，结果进入 merge_results 做深度合成，
    图结构与流转完全不变。同一句里明确要求的其它路由（邮件/日历/Notion）
    原样保留，不会被旅行步骤顶掉。
    """
    destination = extract_travel_destination(
        latest_user_text,
        ((core_intent or {}).get("entities") or {}).get("destination"),
    )
    if not destination:
        return tasks
    kept_steps = [
        step
        for step in tasks
        if str(step.get("route")) not in {ROUTE_LIFE, ROUTE_DIRECT}
    ]
    expanded = [
        *build_travel_steps(
            destination,
            extract_travel_duration(latest_user_text),
            plan_id=plan_id,
            include_search=include_search,
        ),
        *kept_steps,
    ]
    logger.info(
        "Travel request expanded into %d parallel step(s) for %s",
        len(expanded),
        destination,
    )
    return expanded


async def planner_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    """把最新用户消息拆解为计划并持久化规划任务。"""
    db_session = runtime_value(state, runtime, "db_session")
    user_id = state.get("user_id")
    settings = runtime_value(state, runtime, "settings")
    messages = state.get("messages", [])
    force_replan = bool(state.get("force_replan"))
    replan_context = state.get("replan_context") or {}

    # Supervisor continues a plan by feeding the next ready layer through this
    # node; keep those tasks instead of re-persisting a new plan. A forced
    # replan is the exception: the old plan must be rebuilt.
    if not force_replan and state.get("plan") and state.get("tasks"):
        return {}

    latest_user_text = ""
    if messages:
        last_content = getattr(messages[-1], "content", "")
        if isinstance(last_content, str):
            # 时间线标签（`[最新请求] user:` / `[10-28 09:00] user:`）不是用户原话，
            # 会让"提醒我/待办"等关键词匹配与时间词推断失真，先剥掉。
            latest_user_text = _TIMELINE_LABEL_RE.sub("", last_content).strip()

    travel_request = is_travel_request(latest_user_text)

    if not force_replan and not travel_request and not _is_plan_applicable(messages) and not _has_temporal_clue(messages) and not any(
        task.get("route") in EXECUTABLE_ROUTES for task in (state.get("tasks") or [])
    ):
        return {}

    if not isinstance(db_session, AsyncSession) or not isinstance(user_id, uuid.UUID):
        logger.info("Planner skipped: no database session available for user %s", user_id)
        return {}

    # 单步工具请求：若含时间线索（"上午10点去库迪咖啡买拿铁"）也必须走规划
    # LLM 把时间状语剥离进 due_at；无时间线索才直接复用编排器分类结果落表。
    # 强制重规划必须走规划 LLM（复用旧分类等于原样重试）。
    if not force_replan and not _is_plan_applicable(messages) and not _has_temporal_clue(messages):
        raw_tasks = _build_plan_tasks(state, str(uuid.uuid4()), llm, messages, {})
        plan_id, tasks = _plan_to_tasks({"tasks": raw_tasks}, user_id)
        parsed = {}
    else:
        tz_name = tz_name_of(settings)
        today = datetime.now(ZoneInfo(tz_name)).strftime("%A, %B %d, %Y %I:%M %p")
        user_rules_block = ""
        if isinstance(db_session, AsyncSession) and isinstance(user_id, uuid.UUID):
            try:
                rules = await get_planner_rules(db_session, user_id)
                if rules:
                    user_rules_block = (
                        "\n[用户规则 — 规划时必须遵守]\n"
                        + "\n".join(f"- {rule}" for rule in rules[:10])
                    )
            except Exception as exc:
                logger.warning("Failed to load planner rules for user %s: %s", user_id, exc)
        replan_block = (
            "\n" + _replan_instruction(replan_context) if force_replan else ""
        )
        core_block = render_core_intent(state.get("core_intent"))
        if core_block:
            core_block = f"\n{core_block}\n短追问请继承上一轮路由（天气/日历/邮件），不得降级为 direct 闲聊。"

        prompt_messages = [
            SystemMessage(
                content=PLANNER_SYSTEM_PROMPT.format(
                    today=today,
                    tz_name=tz_name,
                    user_rules=user_rules_block,
                    context_isolation_rule=_CONTEXT_ISOLATION_RULE,
                )
                + replan_block
                + core_block
            ),
            *messages[-6:],
        ]
        try:
            response = await llm.ainvoke(prompt_messages)
            raw = response.content if isinstance(response.content, str) else ""
            parsed = _extract_json(raw)
            plan_id, tasks = _plan_to_tasks(parsed, user_id)
        except Exception as exc:
            logger.warning("Planner LLM parse failed, falling back to single direct step: %s", exc)
            plan_id = str(uuid.uuid4())
            parsed = {}
            tasks = _plan_to_tasks({"tasks": _build_plan_tasks(state, plan_id, llm, messages, {})}, user_id)[1]

    # 只对走 agent 的可执行路由落库；direct 由 direct_reply 的工具落库
    # （create_reminder/create_scheduled_job），planner 不重复持久化。
    # 规划步骤默认是内部执行环节（查天气、写邮件、查日历），不进用户看板；
    # 只有用户明确说"提醒我 / 加到待办"且该步骤带截止时间时才建 Task。
    if travel_request and not force_replan:
        tasks = expand_travel_tasks(
            tasks,
            latest_user_text=latest_user_text,
            core_intent=state.get("core_intent"),
            plan_id=plan_id,
            # 没有联网搜索能力时不加 search 步骤：宁可少一路，也不让必失败的
            # 分支污染合成结果。
            include_search=bool(getattr(settings, "tavily_api_key", None)),
        )

    persisted_tasks: list[Task] = []
    row_by_step_index: dict[int, Task] = {}
    conflict_notices: list[str] = []
    tz_name = tz_name_of(settings)
    explicit_todo = is_explicit_todo_request(latest_user_text)
    # direct 步骤的 intent 已被 LLM 剥掉时间状语（"我待会儿还要把快递放回宿舍"
    # → intent="把快递放回宿舍"），兜底推断 due_at 时必须回看用户原话，否则时间
    # 会在 planner → direct_reply 的交接处整条丢失（latest_user_text 上面已取）。
    local_task_turn = looks_like_local_task_management(latest_user_text)

    for index, step in enumerate(tasks):
        # 提示词已写明"任务/待办且没提 Notion → direct"，但 LLM 对"任务"二字
        # 联想太强，编排器层已做确定性纠偏；规划器这里再兜一次，否则步骤会被
        # 落库成 notion 代理并去搜索别的工作区。
        if local_task_turn and step.get("route") == "notion":
            logger.info("Corrected step notion -> direct for local task request: %s", step.get("id"))
            step["route"] = "direct"
        is_direct = step.get("route") == "direct"
        # Planner LLM returns wall-clock `due_at`; interpret naive values in the
        # user's local tz and persist an aware UTC instant (avoids the whole
        # task/reminder shifting by the tz offset when displayed later).
        # 代码兜底：LLM 疏忽没给 due_at 且标题含模糊/日期词（"稍后""明天"）时，
        # 强制按规则推断绝对时间戳（模糊词+1h、纯日期 09:00），保证落库必有时间。
        due_utc = resolve_local_datetime(step.get("due_at"), tz_name)
        if due_utc is None:
            now_local = datetime.now(ZoneInfo(tz_name))
            infer_source = str(step.get("intent") or "")
            if is_direct and _has_temporal_clue(messages):
                infer_source = f"{infer_source} {latest_user_text}".strip()
            fallback_local = _infer_due_at_local(infer_source, now_local, tz_name)
            if fallback_local is not None:
                due_utc = resolve_local_datetime(fallback_local, tz_name)
        if is_direct:
            # 不落库，但要把解析出的绝对时刻交给 direct_reply：
            # 它据此判断"有时间意图 → 必须建提醒"，不再依赖 intent 里是否还有时间词。
            step["due_at"] = due_utc.isoformat() if due_utc else None
            continue
        # 只读取数的步骤（查天气/翻邮件/看日历）永远只是执行手段，即使同一轮
        # 用户说了"提醒我"也不该变成看板上的一行。
        # 其余步骤只有两种情况进看板：用户明确要求提醒/加待办，或这是唯一一步
        # 且带明确时间的用户待办；多步计划里的中间环节一律留在图状态里。
        if is_read_only_step(step.get("intent")):
            logger.info("Read-only plan step (not persisted): %s", step.get("intent"))
            continue
        keep_on_board = explicit_todo or (len(tasks) == 1 and due_utc is not None)
        if not keep_on_board:
            logger.info("Internal plan step (not persisted as a task): %s", step.get("intent"))
            continue
        depends = [
            {"id": dep, "step": dep}
            for dep in step.get("depends_on", [])
        ]
        # 兜底清洗：即使 LLM 偶发把时间状语留在 intent，title 也只保留动作，
        # 时间已由 due_at 承担，避免标题与 due_at 冲突。
        clean_title = _strip_temporal_prefix(step.get("intent") or "计划步骤")
        # 落库前查这个时段是否已有日程/待办：双排同一时段必须如实告知用户，
        # 不能默默接受。查库失败只降级为"不提示"，不影响任务创建本身。
        try:
            conflicts = await find_schedule_conflicts(
                db_session,
                user_id=user_id,
                due_at=due_utc,
            )
        except Exception as exc:
            logger.warning(
                "Schedule conflict check failed for step %s: %s", step.get("id"), exc,
            )
            conflicts = []
        if conflicts:
            logger.info(
                "Schedule conflict for step %s at %s: %s",
                step.get("id"),
                due_utc,
                [item.get("title") for item in conflicts],
            )
            conflict_notices.append(render_conflict_notice(clean_title, conflicts))
        # Keep one failing step from rolling back the rest of the plan.
        try:
            async with db_session.begin_nested():
                row = await create_task_for_user(
                    db_session,
                    user_id=user_id,
                    body=TaskCreateRequest(
                        title=clean_title,
                        description=f"该步骤由计划 {plan_id} 自动生成。 " + (step.get("intent") or ""),
                        status=TaskStatus.NOT_STARTED,
                        project=validate_task_project(step.get("project")) or classify_task_project(clean_title),
                        assigned_agent=step["route"],
                        priority=_parse_priority(step.get("priority")),
                        due_at=due_utc,
                        dependencies=depends,
                        plan_id=plan_id,
                        plan_source="planner",
                        source=classify_task_source(step.get("intent", "")),
                        source_metadata=step.get("source_metadata"),
                    ),
                )
                persisted_tasks.append(row)
                row_by_step_index[index] = row
                if due_utc:
                    try:
                        await schedule_deadline_reminder(
                            db_session,
                            user_id,
                            row.id,
                            row.title,
                            due_utc,
                            minutes_before=settings.reminder_default_lead_minutes,
                        )
                    except Exception as exc:
                        logger.warning("Deadline reminder scheduling failed for task %s: %s", row.id, exc)
        except Exception as exc:
            logger.warning("Planner persistence failed for step %s; continuing without it: %s", step.get("id"), exc)

    # Tasks are created during the chat transaction; the FastAPI dependency commits
    # after the graph has returned, so no explicit commit here.
    # 显式按索引取 row，避免 zip 因跳过 direct 后错位（把 life 的 row 配到 direct 上）。
    # depends_on 必须改写成下游步骤的 task_key：supervisor 用 task_key 记 completed，
    # 若这里仍留 planner 的 "step-1"，下游永远 is_ready()==False，多层 DAG 不会流转。
    # 确定性依赖兜底：LLM 经常把「查天气 → 根据天气写邮件」规划成两个互不
    # 依赖的步骤。一旦没有 depends_on，监督代理的 `_blocked_downstream` 就找
    # 不到下游，查火星失败后邮件步骤照样执行——这就是"AI 自己偷偷查了上海还
    # 强行弹写邮件卡片"的成因。这里按语义强制补边：凡是引用上游结果的步骤，
    # 必须依赖它前面所有产数据的步骤。
    data_steps = [
        str(step.get("id"))
        for step in tasks
        if str(step.get("route")) in {"life", "notion", "calendar", "email", "search"}
    ]
    for index, step in enumerate(tasks):
        route = str(step.get("route"))
        intent_text = str(step.get("intent") or "")
        if route == "life":
            continue
        references_upstream = bool(_UPSTREAM_REFERENCE_RE.search(intent_text))
        if not references_upstream:
            continue
        earlier = [step_id for step_id in data_steps if step_id in {
            str(s.get("id")) for s in tasks[:index]
        }]
        existing = {str(dep) for dep in (step.get("depends_on") or [])}
        missing = [step_id for step_id in earlier if step_id not in existing]
        if missing:
            logger.info(
                "Injected upstream dependency into step %s: %s",
                step.get("id"),
                missing,
            )
            step["depends_on"] = [*(step.get("depends_on") or []), *missing]

    key_by_step_id = {
        str(step.get("id")): (
            str(row_by_step_index[index].id)
            if row_by_step_index.get(index) is not None
            else str(step.get("id"))
        )
        for index, step in enumerate(tasks)
    }
    mapped_tasks = [
        {
            **{
                k: v
                for k, v in step.items()
                if k in {"id", "intent", "route", "priority", "project", "due_at"}
            },
            "depends_on": [
                key_by_step_id.get(str(dep), str(dep))
                for dep in (step.get("depends_on") or [])
            ],
            "plan_id": plan_id,
            "persisted_task_id": str(row.id) if row is not None and row.id else None,
            "task_key": str(row.id) if row is not None and row.id else step.get("id"),
        }
        for index, step in enumerate(tasks)
        for row in [row_by_step_index.get(index)]
    ]
    if force_replan:
        # 失败步骤已被 supervisor 丢弃；把尚未执行的步骤接在新计划后面，
        # 否则每次重规划都会吞掉下游工作。
        # 新步骤的 task_key 可能与旧步骤同名（direct 的 step-1 不落库），
        # 提高 retry_count 让 attempt 与旧的失败结果错开，避免一上线就被判失败。
        replan_attempt = int((state.get("plan") or {}).get("iteration") or 1)
        mapped_tasks = [
            {**task, "retry_count": replan_attempt} for task in mapped_tasks
        ]
        mapped_tasks = [*state.get("tasks", []), *mapped_tasks]
        logger.info(
            "Replanner rebuilt %d failed step(s); %d pending step(s) kept",
            len(replan_context.get("failed") or []),
            len(state.get("tasks") or []),
        )

    return {
        "tasks": mapped_tasks,
        "plan": {
            "plan_id": plan_id,
            "summary": parsed.get("summary") if "parsed" in locals() else "",
            "status": "planned",
            "iteration": int((state.get("plan") or {}).get("iteration") or 0),
        },
        # 冲突提示由 merge_results 追加进最终回复（operator.add 合并）。
        "schedule_conflicts": conflict_notices,
        # 消费掉标志，下一轮按正常流程规划。
        "force_replan": False,
        "replan_context": {},
    }