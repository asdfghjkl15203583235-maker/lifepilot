"""编排器节点 — 分类意图、路由代理、合并结果。

三个节点：
  orchestrate_node   — 从用户消息分类全部意图 → tasks 列表
  direct_reply_node  — 普通对话 + 定时任务调度（工具调用循环）
  merge_results_node — 把并行代理结果合并为一个最终回答
"""

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from langgraph.runtime import Runtime

from agents.common import (
    CONTEXT_ISOLATION_RULE,
    FOLLOWUP_INHERITANCE_RULE,
    HITL_VERBAL_CONFIRM_RULE,
    CITY_CONSISTENCY_RULE,
    NO_REPEAT_RULE,
    NUMERIC_RANGE_RULE,
    ROUTE_ORIGIN_RULE,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_SUCCESS,
    contains_send_claim,
    emotional_comfort_prefix,
    looks_like_local_task_management,
    mark_task_in_progress_if_persisted,
    render_now_reference,
    render_time_reference,
    resolve_local_datetime,
    result_meta,
    runtime_value,
    step_result_update,
    task_status_meta,
    tz_name_of,
)
from agents.core_intent import (
    build_core_intent,
    render_core_intent,
)
from agents.context import render_state_facts
from agents.planner_agent_node import (
    _has_temporal_clue,
    _infer_due_at_local,
    _strip_temporal_prefix,
)
from agents.state import AgentState
from agents.stream_collector import astream_invoke
from agents.travel_planning import (
    TRAVEL_SYNTHESIS_RULES,
    is_travel_request,
    render_travel_advice_footer,
)
from agents.tools.reminder_tool import make_create_reminder_tool
from agents.tools.schedule_tool import (
    make_create_scheduled_job_tool,
    make_delete_scheduled_job_tool,
    make_stop_scheduled_job_tool,
)
from agents.tools.task_action_tools import (
    CREATE_TASK_TOOL_NAME,
    CREATE_TASKS_TOOL_NAME,
    LIST_USER_TASKS_TOOL_NAME,
    PROPOSE_TASK_DELETE_TOOL_NAME,
    PROPOSE_TASK_UPDATE_TOOL_NAME,
    make_task_action_tools,
)

logger = logging.getLogger(__name__)

ROUTE_DIRECT = "direct"
ROUTE_EMAIL = "email"
ROUTE_CALENDAR = "calendar"
ROUTE_NOTION = "notion"
ROUTE_LIFE = "life"
ROUTE_SEARCH = "search"
VALID_ROUTES = {ROUTE_DIRECT, ROUTE_EMAIL, ROUTE_CALENDAR, ROUTE_NOTION, ROUTE_LIFE, ROUTE_SEARCH}

MAX_TASKS_PER_MESSAGE = 5
MAX_DIRECT_TOOL_ROUNDS = 4

MARKDOWN_INSTRUCTION = "请用简洁的 Markdown 格式回复整个回答。"
PLAIN_TEXT_VOICE_INSTRUCTION = (
    "用户正在通过语音播报收听回复。请只使用纯文本："
    "不要使用 Markdown（不要使用 **粗体**、# 标题、`代码`、项目符号星号、表格或链接标记）。"
    "使用适合朗读的简短自然句子。"
)

CLASSIFY_SYSTEM_PROMPT = """\
你是个人助手的路由分类器。
请基于对话内容，识别用户最新消息中的全部不同意图。

一条消息可能包含多个意图（例如“查看邮件，再看看我今天的日历”）。

只回复 JSON 对象（不要使用 Markdown，不要添加多余文字）：
{{
  "tasks": [
    {{
      "intent": "<用户想做什么的简短描述>",
      "route": "direct" | "email" | "calendar" | "notion" | "life" | "search"
    }}
  ]
}}

规则：
- "direct"   → 普通对话、问候、无需工具即可回答的问题、**本地任务管理**（查看/新增/改期/标记完成/删除本应用里的待办），以及创建或管理定时后台自动化（定时摘要、每日提醒、定时报告）。
- "email"    → 阅读、发送、搜索、起草、回复、归档或管理邮件。
- "calendar" → 查看、创建、更新、删除**本地日历**里的日程与带截止时间的待办，
  查询会议议程（本地日历已连接，无需 Google 授权，绝不要判断"未连接"）。
- "notion"   → 仅当用户明确提到 Notion、工作区、页面、数据库、区块、评论，或要求做 Notion 相关搜索时。
- "life"     → 天气、地图及其他外部生活服务查询。
- "search"   → 需要**联网检索**才能回答的信息：最新攻略、避坑/排队情况、近期活动、
  政策与价格变化、新闻或任何"查一下网上怎么说"的请求。旅行规划里的"攻略/避坑/最新"
  这类子步骤用 "search"。

【notion 不等于任务】用户说"任务/待办/to-do"而没提 Notion 时，指的是本应用的任务列表，
必须归为 "direct"（本地任务管理）。绝不能因为出现"任务"二字就路由到 notion 去别的工作区里搜。

如果消息包含多个意图，请在 "tasks" 数组中返回多个对象。
如果只有一个意图，返回包含一个元素的数组。
始终至少返回一个任务。

{CONTEXT_ISOLATION_RULE}

今天的日期和时间：{today}
"""

_MERGE_SYSTEM_PROMPT_TMPL = """\
你是一位友好、简洁的个人助手，名叫 Chief of Staff。
用户的一次请求被拆成了多个并行分支，下面是每个分支的**真实执行结果**。
请把它们合并成一段连贯、组织清晰的内容，自然地呈现信息，不要说“以下是查询结果”。

硬性要求：
- 必须逐个覆盖每个分支，任何分支都不得遗漏或省略。
- 分支状态为 failed 的，必须如实告知用户这一项**没有成功**，并说明原因；
  绝不能因为其它分支成功就把它粉饰成已完成，也不能把失败项从回复里删掉。
- 分支状态为 success 的，如实说明已经完成的内容。
- 有成功也有失败时，要明确区分："X 已完成；Y 没有成功，原因是……"。
{fmt}
"""


def _response_format_instruction(is_voice: bool) -> str:
    return PLAIN_TEXT_VOICE_INSTRUCTION if is_voice else MARKDOWN_INSTRUCTION


_TIMELINE_LABEL_RE = re.compile(r"^\[[^\]]{1,24}\]\s*(?:user|assistant|用户|助手)\s*[:：]\s*")


def _latest_user_text(messages: list[BaseMessage]) -> str:
    """最新一条用户消息的纯文本；分类只看它，避免历史意图串台。

    `_build_timeline_messages` 会给每条消息加 `[最新请求] user:` 前缀，
    分类器要的是用户原话，前缀留着会干扰"是不是写意图"的判断。
    """
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            if not isinstance(content, str):
                return ""
            return _TIMELINE_LABEL_RE.sub("", content).strip()
    return ""


def build_core_intent_from_messages(messages: list[BaseMessage]) -> dict[str, Any]:
    """从 timeline messages（BaseMessage）重建 core_intent。

    agent_runner 组装消息时给每条消息加了 `[最新请求] user:` 这类时间线前缀，
    推导 core_intent 前先剥掉。该 helper 是兜底：正常路径下 core_intent 由
    agent_runner 直接用原始 payload 构建并写入 state。
    """
    payloads: list[dict[str, Any]] = []
    for message in messages:
        role = "assistant" if isinstance(message, AIMessage) else "user"
        content = message.content
        if isinstance(content, str) and content.strip():
            cleaned = _TIMELINE_LABEL_RE.sub("", content).strip()
            payloads.append({"role": role, "content": cleaned})
    return build_core_intent(payloads)


def _extract_json(text: str) -> dict[str, Any]:
    """解析 LLM 输出的 JSON，并去掉可能存在的 Markdown 代码围栏。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [l for l in cleaned.split("\n") if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    return json.loads(cleaned)


# ── Node functions ───────────────────────────────────────────────────────


async def orchestrate_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict:
    """分类全部意图并生成用于并行分发的任务列表。

    - 从 state 读取 `core_intent`（agent_runner 按完整 payload 历史推导），
      没有则用 timeline messages 兜底重建。
    - 把 `render_core_intent` 注入分类 prompt，让分类器知道上一轮的路由与
      实体，短追问（"那后天呢"）必须继承路由而不是降级成 direct。
    - 返回时回填 `core_intent`，让后续节点（planner / direct / merge）都能看到。
    """
    messages: list[BaseMessage] = state.get("messages", [])
    settings = runtime_value(state, runtime, "settings")
    today = render_now_reference(tz_name_of(settings))
    latest_user_text = _latest_user_text(messages)

    core = state.get("core_intent") or build_core_intent_from_messages(messages)

    # HITL 状态机：用户消息是"同意/确认/拒绝"这类对确认草案的回复时，
    # 严禁交给 LLM 重新分类意图——一个误判就把"确认"降级成闲聊，写操作
    # 永远不会执行。直接锁定为 direct 路由并附上 hitl_reply 标记，由
    # direct_reply 调用 PendingActionService 执行/拒绝最新一条待确认草案。
    hitl_reply = (core or {}).get("hitl_reply")
    if hitl_reply in ("approve", "reject"):
        logger.info(
            "HITL reply detected (%s); forcing direct route for %r",
            hitl_reply, latest_user_text,
        )
        return {
            "tasks": [{
                "route": ROUTE_DIRECT,
                "intent": latest_user_text,
                "hitl_reply": hitl_reply,
            }],
            "core_intent": core,
        }

    # 分类只喂最新一条用户消息。把整段历史交给分类器时，上一轮的邮件草稿/
    # 提醒会持续把后续闲聊拉回写意图（问"附近美食"却重弹邮件草案）。
    # 历史由 direct_reply 的对话上下文承担，分类器不需要它。
    classify_user_message = (
        HumanMessage(content=latest_user_text)
        if latest_user_text
        else HumanMessage(content="（用户没有发送文字内容）")
    )

    # 追问继承：如果 core_intent 判定本轮是 short follow-up（如"那后天呢"），
    # 把继承规则 + 上一轮路由/实体显式注入分类器，并要求 route 沿用 anchor 路由。
    core_block = render_core_intent(core)
    followup_inherit_block = (
        FOLLOWUP_INHERITANCE_RULE
        + (f"上一轮的会话核心上下文：{core_block}" if core_block else "")
        if core and core.get("followup")
        else core_block
    )

    classification_messages = [
        SystemMessage(
            content=CLASSIFY_SYSTEM_PROMPT.format(
                today=today,
                CONTEXT_ISOLATION_RULE=(
                    f"{CONTEXT_ISOLATION_RULE}\n{followup_inherit_block}"
                    if followup_inherit_block
                    else CONTEXT_ISOLATION_RULE
                ),
            )
        ),
        classify_user_message,
    ]

    response = await llm.ainvoke(classification_messages)
    raw = response.content if isinstance(response.content, str) else ""
    logger.info("Intent classification raw: %s", raw)

    try:
        parsed = _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse classification, falling back to direct: %s", raw)
        parsed = {"tasks": [{"intent": "普通对话", "route": ROUTE_DIRECT}]}

    raw_tasks = parsed.get("tasks") or [{"intent": "普通对话", "route": ROUTE_DIRECT}]
    # 本轮是"本地任务管理"时，notion 路由一律纠回 direct：notion 代理只能访问
    # Notion 工作区，跑到那里去改待办只会回一句"没找到"。提示词已写明规则，
    # 但 LLM 对"任务"二字的联想太强，用确定性规则兜底。
    local_task_turn = looks_like_local_task_management(latest_user_text)
    validated: list[dict[str, Any]] = []
    for task in raw_tasks[:MAX_TASKS_PER_MESSAGE]:
        route = task.get("route", ROUTE_DIRECT)
        if route not in VALID_ROUTES:
            route = ROUTE_DIRECT
        if local_task_turn and route == ROUTE_NOTION:
            logger.info("Corrected notion -> direct for local task request: %s", latest_user_text)
            route = ROUTE_DIRECT
        validated.append({"intent": task.get("intent", "一般任务"), "route": route})

    if not validated:
        validated = [{"intent": "普通对话", "route": ROUTE_DIRECT}]

    # 追问强制继承：代码兜底，不依赖 LLM。
    # 只要上一轮调用过工具（route != direct）、本轮 core_intent 判定是 short
    # follow-up（"那后天呢""重查一下"），就整体继承上一轮路由与查询串，
    # 绝不给分类器降级成 direct 闲聊的机会。
    inherited_route = (core or {}).get("route")
    if (
        core
        and core.get("followup")
        and inherited_route
        and inherited_route != ROUTE_DIRECT
    ):
        anchor_query = (core.get("query") or "").strip()
        logger.info(
            "Follow-up detected; forcing route %s for %r", inherited_route, latest_user_text
        )
        validated = [{
            "route": inherited_route,
            "intent": anchor_query or latest_user_text,
        }]
    else:
        # 多个 direct 意图合并为单个节点：避免"设提醒 + 闲聊"被拆成两个并行
        # direct_reply，各自带独立工具导致 create_reminder 等被重复调用/重复落库。
        # 非 direct 意图（email/calendar/life/notion）保持独立并行。
        pass

    if not (core and core.get("followup") and inherited_route and inherited_route != ROUTE_DIRECT):
        direct_ints = [t["intent"] for t in validated if t["route"] == ROUTE_DIRECT]
        others = [t for t in validated if t["route"] != ROUTE_DIRECT]
        if len(direct_ints) > 1:
            validated = others + [{"route": ROUTE_DIRECT, "intent": "；".join(direct_ints)}]
        elif direct_ints and others:
            # 单 direct + 其它路由并存：保持现状，direct 单独跑一轮。
            validated = others + [t for t in validated if t["route"] == ROUTE_DIRECT]

    logger.info("Classified %d task(s): %s", len(validated), validated)
    return {"tasks": validated, "core_intent": core if core else state.get("core_intent")}


# ── 确定性工具回执状态机（direct_reply）──────────────────────────────────

# 出现这些词视为 LLM 声称"已完成设置"，必须核对真实工具回执。
_CLAIM_MARKERS = (
    "已设置", "已提醒", "已安排", "已创建", "已预约", "已提醒你",
    "已为你设置", "提醒已设置", "set a reminder", "reminder set",
    "created", "scheduled", "安排了", "设置好了", "已生效",
)
# 只有当这些工具返回 status=created/exists 时才视为"有成功回执"。
_EVIDENCE_TOOLS = (
    "create_reminder",
    "create_scheduled_job",
    CREATE_TASK_TOOL_NAME,
    CREATE_TASKS_TOOL_NAME,
)

# HITL 写操作声称词：命中且没有对应草案回执 → 无条件打回。
_TASK_CLAIM_MARKERS = (
    "已更新", "已修改", "已改到", "已改期", "已删除", "任务已完成", "已标为完成",
    "已经把任务", "任务已更新", "任务已删除", "任务已标记完成", "已设置为完成",
    "task deleted", "task completed", "任务更新为", "deadline 已改",
)
# 提交了确认草案（status=pending + action_id）即视为"已提交，等待确认"。
_TASK_EVIDENCE_TOOLS = (PROPOSE_TASK_UPDATE_TOOL_NAME, PROPOSE_TASK_DELETE_TOOL_NAME)

_TASK_CONFIRM_RETRY_PROMPT = (
    "注意：你刚刚声称已更新/删除/完成任务，但系统并未获得用户确认，任务实际上**没有被修改**。"
    "请立刻改写回复：先用 list_user_tasks 找到目标任务 id，"
    "再调用 propose_task_update（改截止时间改 due_at、标完成用 status='done'）"
    "或 propose_task_delete 提交确认草案，然后告诉用户草稿已准备好，等待其点击确认按钮。"
    "禁止在用户确认前声称已更新/已删除/已完成。"
)

_FORCE_RETRY_SYSTEM_PROMPT = (
    "你刚才声称已设置提醒/安排，但系统未检测到成功的工具执行记录。"
    "请立刻调用 `create_reminder` 工具（或 `create_scheduled_job`，如果是一次性则用 create_reminder）"
    "传入解析好的 remind_at 和标题；或者在回复中向用户明确承认失败、不要假装已设置。"
    "禁止凭空捏造成功。"
)

_HITL_SEND_RETRY_PROMPT = (
    "注意：你刚刚声称邮件已经发送，但系统并没有执行任何发送——你没有发信工具，"
    "发送只会在用户点击确认卡片上的「确认执行」后由后端执行。"
    "用户打字说「ok」「可以」「确认」不等于点击了卡片，不能作为发送成功的证据。"
    "请立刻改写回复：说明草稿仍待确认（或提示用户先让我准备草稿），"
    "并请用户在确认卡片上点击「确认执行」。禁止声称已发送/已回复。"
)


def _parse_tool_receipt(output: str) -> dict | None:
    """解析工具 JSON 回执；非 JSON 或解析失败返回 None。"""
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_successful_receipt(payload: dict | None) -> bool:
    """status=created 或 exists 且带回执 id 才计为成功落库。"""
    if payload is None:
        return False
    status = payload.get("status")
    if status not in ("created", "exists"):
        return False
    task_ids = payload.get("task_ids")
    has_batch_receipt = isinstance(task_ids, list) and any(task_ids)
    return bool(
        payload.get("reminder_id")
        or payload.get("job_id")
        or payload.get("task_id")
        or has_batch_receipt
    )


def _contains_claim(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _CLAIM_MARKERS)


def _has_writing_evidence(tool_evidence: dict[str, str]) -> bool:
    return any(tool in tool_evidence for tool in _EVIDENCE_TOOLS)


def _contains_task_claim(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _TASK_CLAIM_MARKERS)


def _has_task_pending_receipt(tool_evidence: dict[str, str]) -> bool:
    return any(tool in tool_evidence for tool in _TASK_EVIDENCE_TOOLS)


def _is_pending_receipt(payload: dict | None) -> bool:
    """HITL draft receipts: status=pending + action_id."""
    if payload is None:
        return False
    if payload.get("status") != "pending":
        return False
    return bool(payload.get("action_id"))


def _needs_forced_reminder(task: dict[str, Any], intent: str) -> bool:
    """是否该走"强制建提醒"兜底。

    触发条件二选一：
      1. planner 已解析出 `due_at` —— 时间词可能已被剥出 intent，但时间意图依然存在；
      2. intent 文本里仍残留时间词（planner 没走规划、或 LLM 把时间留在了动作里）。
    """
    if task.get("due_at"):
        return True
    return bool(intent) and _has_temporal_clue([HumanMessage(content=intent)])


async def _force_planned_reminder(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Any,
    intent: str,
    tool_evidence: dict[str, str],
    due_at: Any = None,
) -> dict[str, str] | None:
    """时间词代码兜底：LLM 把"一会出发吃饭""后天参加大会"当闲聊、没调 create_reminder 时，
    强制按模糊时间规则推断绝对时间（一会=+1h、后天无钟点=09:00）并落库 reminder+task。

    `due_at` 优先：planner 已把时间词剥进 due_at 时（intent 里已无时间词），直接用它，
    不再依赖 intent 文本里是否还剩时间词。

    返回确认文案；无法推断时间或标题剥空时返回 None（不强制）。
    """
    title = _strip_temporal_prefix(intent)
    if not title:
        return None
    tz_name = tz_name_of(settings)
    now_local = datetime.now(ZoneInfo(tz_name))
    due_utc = resolve_local_datetime(due_at, tz_name) if due_at else None
    inferred = False
    if due_utc is None or due_utc <= now_local.astimezone(due_utc.tzinfo):
        # 没有可用的 due_at（或已过期）→ 退回按 intent 文本里的模糊时间词推断。
        due_local = _infer_due_at_local(intent, now_local, tz_name)
        if due_local is None:
            return None
        due_utc = resolve_local_datetime(due_local, tz_name)
        inferred = True
    if due_utc is None:
        return None
    try:
        tool = make_create_reminder_tool(session, user_id, settings)
        output = await tool.ainvoke({"title": title, "remind_at": due_utc.isoformat()})
    except Exception as exc:
        logger.warning("Force-planned reminder failed (%s): %s", intent, exc)
        return None
    payload = _parse_tool_receipt(output)
    if _is_successful_receipt(payload):
        tool_evidence["create_reminder"] = output
        local_display = due_utc.astimezone(ZoneInfo(tz_name)).strftime("%m-%d %H:%M")
        if inferred:
            # 模糊时间（"待会儿""稍后"）只能猜一个大概时刻，必须把假设说出来
            # 并主动邀请纠正，而不是当成精确时间默默落库。
            confirm = (
                f"我按「大概这个点」把「{title}」记在 {local_display}（你的本地时间），"
                f"到点提醒你。如果想换成更准确的时间，直接告诉我。"
            )
        else:
            confirm = f"已把「{title}」安排到 {local_display}（你的本地时间），到点我会提醒你。"
        return {"confirm": confirm, "receipt": output, "inferred": inferred}
    return None


def _reminder_confirm_from_evidence(raw: str, tz_name: str) -> str | None:
    """从 create_reminder 的成功回执里拼一句确定性的确认语。

    真实 badcase：提醒确实落库了，但模型（flash 档）写最终回复时跑题成"明天的天气"，
    对刚创建的提醒只字未提——用户看到"提醒已设置但回答驴唇不对马嘴"。
    """
    payload = _parse_tool_receipt(raw)
    if not _is_successful_receipt(payload):
        return None
    title = str(payload.get("title") or "提醒").strip()
    local_display = ""
    remind_at = payload.get("remind_at")
    if remind_at:
        try:
            when = datetime.fromisoformat(str(remind_at))
            if when.tzinfo is None:
                when = when.replace(tzinfo=ZoneInfo(tz_name))
            local_display = when.astimezone(ZoneInfo(tz_name)).strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            local_display = ""
    time_part = f" @ {local_display}" if local_display else ""
    return f"已设置提醒：「{title}」{time_part}，到点我会提醒你。"


def _append_reminder_confirm(text: str, tool_evidence: dict[str, str], tz_name: str) -> str:
    """模型已成功调 create_reminder 但回复没提提醒时，确定性补一句确认。"""
    raw = tool_evidence.get("create_reminder")
    if not raw:
        return text
    payload = _parse_tool_receipt(raw)
    if not _is_successful_receipt(payload):
        return text
    title = str(payload.get("title") or "").strip()
    if "提醒" in text or (title and title in text):
        # 模型已经自己提到了提醒，不重复追加。
        return text
    confirm = _reminder_confirm_from_evidence(raw, tz_name)
    if not confirm:
        return text
    return f"{text}\n\n{confirm}" if text else confirm


def _format_approve_confirmation(action: Any, result: dict[str, Any]) -> str:
    """HITL approve 后的确定性回复：如实汇报执行结果，不渲染内部细节。"""
    try:
        action_type = getattr(action, "action_type", None)
        type_label = str(getattr(action_type, "value", action_type) or "操作")
    except Exception:
        type_label = "操作"
    title = str(getattr(action, "title", "") or "").strip()
    parts = [f"已执行「{title}」。" if title else f"已执行该{type_label}。"]
    result_text = (result or {}).get("result")
    if isinstance(result_text, dict):
        if result_text.get("deleted"):
            parts.append("任务已删除。")
        elif result_text.get("status"):
            parts.append(f"任务状态已更新为「{result_text.get('status')}」。")
    elif isinstance(result_text, str) and result_text.strip():
        parts.append(result_text.strip())
    return "".join(parts)


async def direct_reply_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict:
    """基于对话回答；如果用户需要后台自动化，则使用调度工具。

    确定性状态机：只有拿到 create_reminder / create_scheduled_job 的成功回执
    （status=created/exists 且含 id）才允许 LLM 声称"已设置"。否则拦截假回复，
    注入 SystemMessage 强制重试或要求承认失败。
    """
    task = state.get("current_task") or {}
    intent = task.get("intent", "")
    messages: list[BaseMessage] = list(state.get("messages", []))
    meta = result_meta(task)
    await mark_task_in_progress_if_persisted(state, task, runtime)

    # HITL 状态机执行点：orchestrate_node 识别到用户"同意/确认/拒绝"后锁定
    # direct 路由并在 current_task.hitl_reply 上做标记。这里不再让 LLM 触碰，
    # 直接用最新一条待确认草案的 id 执行或拒绝，写操作才有确定性结果。
    hitl_reply = task.get("hitl_reply")
    if hitl_reply in ("approve", "reject"):
        db_session_raw = runtime_value(state, runtime, "db_session")
        hitl_session = db_session_raw if isinstance(db_session_raw, AsyncSession) else None
        hitl_user_id = state.get("user_id")
        if hitl_session is not None and isinstance(hitl_user_id, uuid.UUID):
            from services.pending_action_service import (
                approve_and_execute,
                list_pending_actions,
                reject_pending_action,
            )
            from lib.enums import PendingActionStatus
            try:
                actions = await list_pending_actions(
                    hitl_session,
                    hitl_user_id,
                    status=PendingActionStatus.PENDING,
                    limit=1,
                )
            except Exception as exc:
                logger.warning("HITL reply handling failed: %s", exc)
                actions = []

            if not actions:
                # 没有待确认草案 ≠ 用户没说话。典型场景：上一轮问「需要我把行程
                # 记成待办吗？」，用户回「好的，帮我加上」——这里必须继续走正常
                # 工具循环去 create_tasks，而不是回一句"没有待决操作"把请求吞掉。
                logger.info(
                    "HITL reply %r has no pending action; handling as a normal turn", hitl_reply
                )
            else:
                if hitl_reply == "approve":
                    try:
                        _action, result = await approve_and_execute(
                            hitl_session, hitl_user_id, actions[0].id,
                            settings=runtime_value(state, runtime, "settings"),
                        )
                        text = _format_approve_confirmation(actions[0], result)
                    except Exception as exc:
                        text = f"执行失败：{exc}"
                else:
                    try:
                        action = await reject_pending_action(hitl_session, hitl_user_id, actions[0].id)
                        text = f"已取消「{getattr(action, 'title', '该操作')}」。"
                    except Exception as exc:
                        text = f"取消失败：{exc}"
                return {
                    "agent_results": [{
                        "route": "direct", "intent": intent, "result": text,
                        **meta, **task_status_meta(TASK_STATUS_SUCCESS),
                    }],
                    **step_result_update(task, text, TASK_STATUS_SUCCESS),
                }

    is_voice = state.get("is_voice_request", False)
    settings = runtime_value(state, runtime, "settings")
    fmt = _response_format_instruction(is_voice)

    core = state.get("core_intent")
    core_block = render_core_intent(core)
    # 模块六：State 事实（core_intent / step_results / pending_actions）结构化注入，
    # 模型读事实而不是去历史 messages 里考古。
    state_facts = render_state_facts(state)
    facts_block = f"\n{state_facts.content}" if state_facts is not None else ""
    system_text = (
        "你是一位友好、简洁的个人助手，名叫 Chief of Staff。"
        "请基于对话回答，做到有帮助且简短。"
        "你可以记住用户分享的信息（姓名、偏好、目标），系统会自动保存。"
        "当用户要求一次性定时提醒（例如\"明天早上八点提醒我改简历\"、"
        "\"下午三点提醒我开会\"）时，必须调用 `create_reminder` 工具，"
        "传入解析好的 remind_at（ISO-8601）和简短标题；绝不要在未调用工具的情况下声称已设置提醒。"
        "当用户想要定时或基于时间的后台任务（每日摘要、每周汇总、定时报告）时，"
        "请使用 `create_scheduled_job` 工具，并传入清晰的标题、完整指令、IANA 时区、"
        "`schedule_starts_at` 和 `schedule_ends_at`（ISO-8601 UTC；绝不能省略结束时间）、"
        "5 字段 cron 表达式，以及至少一个 `actions` 条目。"
        "要暂停计划请使用 `stop_scheduled_job`；要删除请使用 `delete_scheduled_job`。"
        "【严禁把普通对话/记忆当作定时任务】记住或记录用户信息（姓名、偏好、习惯、"
        "喜欢喝什么等）**不需要任何工具**，系统会自动保存到长期记忆；只有用户明确说了"
        "循环周期（\"每天\"\"每周\"\"每月\"\"每日\"\"daily\"\"weekly\"\"monthly\"等）才允许调用"
        "`create_scheduled_job`。缺少明确循环周期却创建定时任务是被禁止的错误行为。"
        "【日常沟通也能回答生活服务查询】：用户问天气、地图、周边生活服务时，"
        "必须调用 `get_weather` / `get_map_context` 工具获取真实数据后再回答；"
        "**绝不允许在没有工具结果的情况下回答「没有可用的天气工具」或「无法查询」。**"
        "地图查询必须传 `city` 限定城市；需要估算两地通行时间时调用 "
        "`get_route_planning`（步行/公交地铁/驾车），不要说自己没有路线规划能力。"
        f"{NUMERIC_RANGE_RULE}"
        f"{CITY_CONSISTENCY_RULE}"
        f"{ROUTE_ORIGIN_RULE}"
        f"{NO_REPEAT_RULE}"
        "【行程/多条安排落库】用户确认一份行程时（\"好的，帮我加上\"\"记到待办\""
        "\"加到日程里\"），调用 `create_tasks` 一次性把每个节点落成独立任务："
        "每个节点给 title 和 ISO-8601 `due_at`（如 {\"title\":\"天安门广场\","
        "\"due_at\":\"2026-11-07T09:00:00+08:00\"}）。"
        "相对日期（周六/明天/下周一/两日游的第二天）必须结合下面给的\"今天\"换算成"
        "绝对日期；每个条目都要带时间，不要把多个景点塞进同一条任务的描述里。"
        "只有拿到 status=created 的回执才能声称已添加；未调用工具时禁止说\"已经加好了\"。"
        "【修改/删除任务必须走确认草案】：当用户要求修改任务的截止时间、标记完成或删除任务时："
        "先用 `list_user_tasks` 找到目标任务 id，再用 `propose_task_update`（改截止时间用 due_at、"
        "标记完成用 status='done'）或 `propose_task_delete` 提交确认草案。"
        "这些工具只保存草稿等待用户确认，**不会真正修改或删除任务**。"
        "即使用户说\"直接改\"\"不用确认\"也必须先提交草案。"
        "在用户点击确认前，禁止声称\"已更新\"\"已删除\"\"已完成\"，"
        "只能说\"草稿已准备好，等待你确认后执行\"。"
        "【禁止声称已发送邮件】你没有发送邮件的工具：发送只能由用户在确认卡片上点击"
        "「确认执行」后发生，本轮绝不可能发生。即使用户刚打字说\"ok/可以/确认\"，"
        "也不代表卡片被点击，更不能当作发送成功的证据。"
        + HITL_VERBAL_CONFIRM_RULE
        + (f"\n{core_block}" if core_block else "")
        + facts_block
        + CONTEXT_ISOLATION_RULE
        + f"\n{fmt}\n"
        + render_time_reference(tz_name_of(settings))
    )
    upstream_context = str(state.get("upstream_context") or "")
    if upstream_context:
        system_text = f"{system_text}\n{upstream_context}\n"
    system = SystemMessage(content=system_text)

    db_session_raw = runtime_value(state, runtime, "db_session")
    db_session = db_session_raw if isinstance(db_session_raw, AsyncSession) else None
    user_id = state.get("user_id")

    work_messages: list[BaseMessage] = [system, *messages]
    if intent:
        work_messages.append(HumanMessage(content=intent))

    if db_session is None or user_id is None or not isinstance(user_id, uuid.UUID):
        response = await astream_invoke(llm, work_messages)
        text = response.content if isinstance(response.content, str) else ""
        # 无数据库时没有工具可用，也就永远没有发送证据；同样必须拦下"已发送"。
        if contains_send_claim(text):
            text = (
                "我没有发送任何邮件：发送只能在确认卡片上点击「确认执行」后由系统执行。"
                "聊天里回复「ok」不算确认，请先在卡片上确认，或让我先准备好草稿。"
            )
        return {
            "agent_results": [{
                "route": "direct", "intent": intent, "result": text,
                **meta, **task_status_meta(TASK_STATUS_SUCCESS),
            }],
            **step_result_update(task, text, TASK_STATUS_SUCCESS),
        }

    schedule_tools = [
        make_create_reminder_tool(db_session, user_id, settings),
        make_create_scheduled_job_tool(db_session, user_id),
        make_stop_scheduled_job_tool(db_session, user_id),
        make_delete_scheduled_job_tool(db_session, user_id),
    ]
    # 天气/地图兜底：短追问"那后天呢？"被继承路由后可能落到 direct_reply，
    # 若这里没有 life 工具，模型只能回"没有可用的天气工具"。挂上 LifeTool
    # 让 direct 也能完成生活服务查询。
    from agents.tools.life_tool import LifeTool

    # 跨轮追问（"去成都" → "酒店定在太古里"）时本轮 intent 没有城市，
    # 由 core_intent 已确认的目的地兜底，避免同名地点落到别的城市。
    session_destination = ((core or {}).get("entities") or {}).get("destination")
    default_city = session_destination.strip() if isinstance(session_destination, str) else None
    life_toolbox = (
        LifeTool(db_session, settings, default_city=default_city)
        if db_session is not None
        else None
    )
    life_tools = life_toolbox.tools() if life_toolbox is not None else []
    all_tools = [
        *schedule_tools,
        *make_task_action_tools(db_session, user_id, settings),
        *life_tools,
    ]
    tools_by_name = {t.name: t for t in all_tools}
    llm_with_tools = llm.bind_tools(all_tools)


    # 防同一节点内重复执行同一工具（LLM 偶发在后续轮次重复发相同 tool_call）。
    called_tools: set[str] = set()
    # 本轮成功的写库回执：tool_name -> 回执原文（含 reminder_id/job_id）。
    tool_evidence: dict[str, str] = {}
    # 本轮提交的 HITL 确认草案摘要，供 runner 发 SSE。
    pending_actions: list[dict] = []

    for _ in range(MAX_DIRECT_TOOL_ROUNDS):
        response = await astream_invoke(llm_with_tools, work_messages)

        if isinstance(response, AIMessage) and response.tool_calls:
            work_messages.append(response)
            for call in response.tool_calls:
                tool_name = call.get("name", "")
                call_id = call.get("id", "")
                args = call.get("args") or {}
                target = tools_by_name.get(tool_name)
                if tool_name in called_tools:
                    output = json.dumps(
                        {"status": "skipped", "message": "该工具本请求已调用过，已跳过重复调用。"},
                        ensure_ascii=False,
                    )
                else:
                    if tool_name:
                        called_tools.add(tool_name)
                    output = await target.ainvoke(args) if target else f"未知工具：{tool_name}"
                parsed_receipt = _parse_tool_receipt(output)
                if tool_name in _EVIDENCE_TOOLS and _is_successful_receipt(parsed_receipt):
                    tool_evidence[tool_name] = output
                if tool_name in _TASK_EVIDENCE_TOOLS and _is_pending_receipt(parsed_receipt):
                    tool_evidence[tool_name] = output
                    pending_actions.append({
                        "action_id": parsed_receipt.get("action_id"),
                        "action_type": parsed_receipt.get("action_type"),
                        "status": "pending",
                        "task_id": parsed_receipt.get("task_id"),
                    })
                work_messages.append(
                    ToolMessage(
                        content=output if isinstance(output, str) else str(output),
                        tool_call_id=call_id,
                    )
                )
            continue

        # 无工具调用 → LLM 直接给出最终文字。核对是否谎报"已设置"、任务写操作或"已发送"。
        text = response.content if isinstance(response.content, str) else ""
        if (
            (_contains_claim(text) and not _has_writing_evidence(tool_evidence))
            or (_contains_task_claim(text) and not _has_task_pending_receipt(tool_evidence))
            # 发送声称：direct 分支根本没有发信工具，本轮永远没有发送证据。
            # 用户一句"ok"被模型当成授权就宣告"已发送"——这是最严重的越权幻觉。
            or contains_send_claim(text)
        ):
            # 拦截假回复：注入强制重试提示，回到循环让 LLM 重新走工具。
            logger.warning(
                "Blocked unbacked setup/task/send claim in direct_reply (intent=%s): %.120s",
                intent,
                text,
            )
            if contains_send_claim(text):
                work_messages.append(SystemMessage(content=_HITL_SEND_RETRY_PROMPT))
            elif _contains_task_claim(text):
                work_messages.append(SystemMessage(content=_TASK_CONFIRM_RETRY_PROMPT))
            else:
                work_messages.append(SystemMessage(content=_FORCE_RETRY_SYSTEM_PROMPT))
            continue

        # 时间词代码兜底：LLM 把"一会出发吃饭""后天参加大会"这类含模糊时间词的请求
        # 当普通闲聊回复、没调 create_reminder 时，强制按规则推断绝对时间并落库，
        # 保证时间词必转绝对时间，不依赖 LLM 主动性。
        # 注意：planner 已把时间词剥进 task["due_at"] 的情况下 intent 里没有时间词，
        # 因此判定必须同时看 due_at（见 _needs_forced_reminder）。
        if "create_reminder" not in tool_evidence and _needs_forced_reminder(task, intent):
            forced = await _force_planned_reminder(
                db_session, user_id, settings, intent, tool_evidence, due_at=task.get("due_at")
            )
            if forced is not None:
                sep = "\n\n" if text else ""
                text = f"{text}{sep}{forced['confirm']}"
        elif "create_reminder" in tool_evidence:
            # 模型自调工具已落库：若最终文本没提提醒，确定性补确认句，防止跑题。
            text = _append_reminder_confirm(text, tool_evidence, tz_name_of(settings))

        # 时间兜底成功落库也算完成；只有需要提醒却完全没落库才判失败。
        needs_reminder = task.get("due_at") or _has_temporal_clue([HumanMessage(content=intent)])
        unmet = bool(needs_reminder) and "create_reminder" not in tool_evidence
        return {
            "agent_results": [{
                "route": "direct", "intent": intent, "result": text,
                **meta,
                **(
                    task_status_meta(TASK_STATUS_FAILED, "提醒未能落库")
                    if unmet
                    else task_status_meta(TASK_STATUS_SUCCESS)
                ),
            }],
            "pending_actions": pending_actions,
            **step_result_update(
                task, text,
                TASK_STATUS_FAILED if unmet else TASK_STATUS_SUCCESS,
            ),
        }

    # 循环耗尽仍未成功落库且 LLM 反复谎报 → 回退为诚实的失败说明。
    # 同时拦截"已更新/已删除/已完成"任务写声称与"已发送"声称，防止无回执的幻觉透过。
    fallback = await astream_invoke(llm, [system, *messages])
    text = fallback.content if isinstance(fallback.content, str) else ""
    if not text or _contains_claim(text) or _contains_task_claim(text):
        text = "抱歉，我尝试设置提醒但没有成功完成，请稍后再试或换个说法。"
    if contains_send_claim(text):
        text = (
            "我没有发送任何邮件：发送只能在确认卡片上点击「确认执行」后由系统执行。"
            "聊天里回复「ok」不算确认，请先在卡片上确认，或让我先准备好草稿。"
        )

    # 循环耗尽同样执行时间词兜底（避免重试后仍未落库）。
    if "create_reminder" not in tool_evidence and _needs_forced_reminder(task, intent):
        forced = await _force_planned_reminder(
            db_session, user_id, settings, intent, tool_evidence, due_at=task.get("due_at")
        )
        if forced is not None:
            text = f"{text}\n\n{forced['confirm']}" if text else forced["confirm"]
    elif "create_reminder" in tool_evidence:
        text = _append_reminder_confirm(text, tool_evidence, tz_name_of(settings))

    needs_reminder = task.get("due_at") or _has_temporal_clue([HumanMessage(content=intent)])
    unmet = bool(needs_reminder) and "create_reminder" not in tool_evidence
    return {
        "agent_results": [{
            "route": "direct", "intent": intent, "result": text,
            **meta,
            **(
                task_status_meta(TASK_STATUS_FAILED, "提醒未能落库")
                if unmet
                else task_status_meta(TASK_STATUS_SUCCESS)
            ),
        }],
        "pending_actions": pending_actions,
        **step_result_update(
            task, text,
            TASK_STATUS_FAILED if unmet else TASK_STATUS_SUCCESS,
        ),
    }


_STATUS_LABELS = {
    TASK_STATUS_SUCCESS: "success",
    TASK_STATUS_PARTIAL: "partial",
    TASK_STATUS_FAILED: "failed",
}


def _latest_result_per_task(
    results: list[dict],
    tasks: list[dict] | None = None,
) -> list[dict]:
    """每个任务只保留最后一次尝试的结果（重规划会留下多轮结果）。

    `agent_results` 的 reducer 是 operator.add，同一任务重试后会有多条；
    全部塞给合并 LLM 会让它复述已作废的失败，也会把成功项重复一遍。
    重规划后旧步骤已从 `tasks` 移除，其失败结果同样不能再汇报。
    """
    live_keys = {
        str(task.get("task_key") or task.get("persisted_task_id") or task.get("id") or "")
        for task in (tasks or [])
    }
    live_keys.discard("")

    by_key: dict[str, dict] = {}
    for result in results:
        key = str(
            result.get("task_key")
            or result.get("persisted_task_id")
            or f"{result.get('route')}::{result.get('intent')}"
        )
        if live_keys and result.get("task_key") and key not in live_keys:
            continue
        previous = by_key.get(key)
        if previous is None or int(result.get("attempt") or 0) >= int(previous.get("attempt") or 0):
            by_key[key] = result
    return list(by_key.values())


def _status_of(result: dict) -> str:
    status = result.get("task_status")
    return _STATUS_LABELS.get(str(status), "unknown")


def _status_footer(results: list[dict]) -> str:
    """确定性状态清单：LLM 合并时可能弱化或漏掉失败分支，这里兜底补齐。"""
    failures = [
        r for r in results
        if _status_of(r) == "failed"
    ]
    if not failures:
        return ""
    lines = []
    for result in failures:
        intent = str(result.get("intent") or result.get("route") or "该步骤").strip()
        reason = str(result.get("error") or result.get("result") or "").strip()
        reason = " ".join(reason.split())
        if len(reason) > 160:
            reason = reason[:160] + "…"
        lines.append(f"- 「{intent}」没有成功" + (f"：{reason}" if reason else "。"))
    return "\n\n**未能完成的部分：**\n" + "\n".join(lines)


def _conflict_footer(state: AgentState) -> str:
    """确定性追加时段冲突提示：这类提示绝不能因 LLM 概述而被吞掉。"""
    notices = [notice for notice in (state.get("schedule_conflicts") or []) if notice]
    if not notices:
        return ""
    return "\n\n**日程提醒：**\n" + "\n".join(f"- {notice}" for notice in notices)


def _blocked_footer(state: AgentState) -> str:
    """上游失败导致下游被跳过时的确定性提示（含"是否取消"）。"""
    notices: list[str] = []
    for notice in state.get("blocked_step_notices") or []:
        if notice and notice not in notices:
            notices.append(notice)
    if not notices:
        return ""
    return "\n\n**已暂停的步骤：**\n" + "\n".join(f"- {notice}" for notice in notices)


def _footers(state: AgentState) -> str:
    return f"{_blocked_footer(state)}{_conflict_footer(state)}"


def _is_travel_turn(state: AgentState) -> bool:
    return is_travel_request(_latest_user_text(list(state.get("messages", []))))


def _travel_footer(state: AgentState, results: list[dict]) -> str:
    """旅行规划的确定性页脚：保证「带伞/穿衣」提醒不会因模型疏忽而消失。"""
    if not _is_travel_turn(state):
        return ""
    return render_travel_advice_footer(results)


def _comfort_prefix(state: AgentState) -> str:
    """情绪化输入的确定性安抚开场白。

    真实 badcase：用户说"今天心情崩溃"，最终回复是干巴巴的天气播报。
    合并节点是最终出口，安抚必须在这里兜底，且不能由 LLM 决定要不要给。
    """
    return emotional_comfort_prefix(_latest_user_text(list(state.get("messages", []))))


async def merge_results_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict:
    """把所有并行代理结果合并为一个最终回答。

    局部失败绝不能掩盖整体成功：合并提示词要求逐项如实汇报，末尾再按
    结构化 `task_status` 追加确定性失败清单，避免 LLM 只讲成功项。
    时段冲突同理，以确定性页脚追加；情绪化输入则先加一句安抚。
    """
    results: list[dict] = _latest_result_per_task(
        list(state.get("agent_results") or []),
        list(state.get("tasks") or []),
    )
    is_voice = state.get("is_voice_request", False)
    footers = _footers(state)
    comfort = _comfort_prefix(state)
    travel_footer = _travel_footer(state, results)

    if not results:
        return {"final_answer": f"{comfort}抱歉，我暂时无法处理你的请求。{footers}"}

    if len(results) == 1:
        answer = results[0].get("result", "没有结果。")
        if _status_of(results[0]) == "failed":
            answer = f"{answer}{_status_footer(results)}"
        return {"final_answer": f"{comfort}{answer}{travel_footer}{footers}"}

    parts = [
        f"--- 分支 {i}：{r.get('intent', '未知')}（状态：{_status_of(r)}）---\n"
        f"{r.get('result', '')}"
        + (f"\n失败原因：{r.get('error')}" if r.get("error") else "")
        for i, r in enumerate(results, 1)
    ]
    combined = "\n\n".join(parts)
    fmt = _response_format_instruction(is_voice)
    # 旅行规划走深度合成：把天气/景点/交通合成为可执行行程，而不是罗列。
    synthesis_rules = TRAVEL_SYNTHESIS_RULES if travel_footer or _is_travel_turn(state) else ""

    messages: list[BaseMessage] = [
        SystemMessage(content=_MERGE_SYSTEM_PROMPT_TMPL.format(fmt=fmt) + synthesis_rules),
        *list(state.get("messages", [])),
        HumanMessage(content=f"需要合并的分支结果：\n\n{combined}"),
    ]

    response = await astream_invoke(llm, messages)
    text = response.content if isinstance(response.content, str) else ""
    footer = _status_footer(results)
    if footer and not any(
        marker in text for marker in ("没有成功", "失败", "未能完成")
    ):
        text = f"{text}{footer}"
    if travel_footer and "出行提醒" in text:
        # 模型已经自己写了提醒，避免重复追加同一句。
        travel_footer = ""
    return {"final_answer": f"{comfort}{text}{travel_footer}{footers}"}
