"""邮件代理节点 — 基于 163 邮箱/IMAP（兼容旧 Gmail）的工具调用循环。

含 L1-L4 反幻觉门禁：
- L1 证据链：read_results（成功读到的正文） / raw_errors（工具原始报错）。
- L2 答复前门禁：凡是"要求看邮件内容"的请求，必须先有正文证据才能终答；
  模型试图无证据终答时由代码强制注入 list/get，拿不到证据就走固定错误模板。
- L3 止语辞检测：终答含"网易拦截/IMAP设置/授权码"等敷衍词且未包含已读正文 → 打回重写。
- L4 工具错误如实转述：错误原文进 raw_errors，兜底模板原样呈现，不给模型改写空间。
"""

import asyncio
import json
import logging
import re
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime

from agents.common import (
    HITL_VERBAL_CONFIRM_RULE,
    SEND_CLAIM_MARKERS,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_SUCCESS,
    contains_send_claim,
    mark_task_in_progress_if_persisted,
    result_meta,
    runtime_value,
    step_result_update,
    task_status_meta,
    tz_name_of,
)
from agents.state import AgentState
from agents.stream_collector import astream_invoke
from agents.tools.email_agent_tools import (
    make_email_tools,
    make_mail163_email_tools,
    make_meeting_invite_tools,
    make_propose_email_tools,
)

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
MAX_FORCED_ROUNDS = 3
MAX_FORCED_GET = 3
_MAX_TOOL_OUTPUT_CHARS = 30_000
_MAX_EVIDENCE_BODY_CHARS = 5_000
_BODY_TRUNCATION_MARKER = "\n[内容过长，已截断...]"

EMAIL_SYSTEM_PROMPT = """\
你是一位可以完整访问用户 163 邮箱账户的邮件助手。请使用可用工具完整完成用户的请求。

读取规则（重要）：
- list_emails 只返回每封邮件的标头和最多前 10 封邮件的短摘要 snippet，【不含完整正文】。
- 当用户要求查看/阅读邮件的内容、正文、原文，或想知道某封"邮件说了什么"时：
  必须调用 get_email(message_id) 拉取该封邮件的完整正文，然后把正文原样展示给用户，
  包括发件人、日期、署名、落款。
- 展示内容时直接引用 get_email 返回的 body 字段原文，不要只给标头、摘要或自行转述。
- 只有当用户明确只要列表/主题时，才只给发件人、主题、日期、摘要。
- 【列表查询不要展开正文】用户问"有没有新邮件""最近几天有几封""收件箱有什么"时，
  只回答数量 + 发件人/主题/日期。不要调用 get_email，也不要把正文、验证码、
  邮件签名或引用历史贴出来；除非用户明确要"正文/原文/内容/说了什么"。
- 【不要展示工具原始输出】不要把工具返回的 JSON、字段名、`---` 分隔线、
  "以下是已读取到的邮件正文"之类的内部标记写进回复，用自然语言转述。

其他规则：
- 查询邮件时：使用 list_emails，并支持 from:、subject:、is:unread、is:read、newer_than:7d 等查询语法
- 【发送/回复必须走确认，绝对禁止直接声称已发送】：
  - 发送邮件必须调用 propose_email_draft，回复邮件必须调用 propose_email_reply，
    这两个工具只保存草稿等待用户确认，**不会真正发送**。
  - 即使用户说"直接发""不用确认""马上发"，也必须先提交确认草案，绝不能真的发出。
  - 在用户点击确认前，回复中**禁止**出现"已发送""已回复""发送成功"等字样，
    只能说"草稿已准备好，等待你确认后发送"。
  - 【用户打字说"可以/ok/确认"不代表已发送】那只是同意这么做，并不会替你点卡片；
    此时你只能重新给出/确认草案并请用户点击卡片，绝不能回答"已发送"。
- 【历史草案视为已处理】：对话历史中出现过的邮件草案、收件人或主题，
  都算**上一轮已经完成**的工作。只执行本次 `intent` 描述的新指令，
  绝不能让历史草案把当前请求重新拉回发邮件/写草稿。
- 【会议邀请进任务】：用户要求“看看邮箱有没有会议邀请”“把会议邀请加到任务/日程”时，
  调用 scan_meeting_invites 扫描收件箱。按返回的 items 逐条列出会议标题与时间，
  并说明新建/更新了几个任务；若一项都没有，直说没有找到，不要编造会议。
- 【上游步骤结果】如果本轮输入里附带了"上游步骤…的结果"，那是对应前置步骤
  已经查到的真实数据；写邮件正文时必须直接引用其中的具体事实（数字、天气、日期等），
  不要重新查询，也不要用占位符或泛泛而谈。
- 始终以清晰、友好、易于阅读的方式呈现结果
- 不要编造信息，只报告工具返回的内容
- 严禁用"网易安全拦截""Unsafe Login""请检查IMAP设置""需要授权码"之类的借口敷衍用户：
  只有工具真的调用并报错，才解释原始错误；如果工具没报错，就必须把读到的正文直接展示出来
- 如果工具报错包含 Unsafe Login、授权码、IMAP/SMTP 登录、文件夹打开失败等关键词，
  请原样转述这段错误信息，再给出处理建议（如检查 IMAP/SMTP 服务、更换授权码）
{format_instruction}
今天：{today}
"""

MARKDOWN_INSTRUCTION = "请用简洁的 Markdown 格式回复。"
PLAIN_TEXT_INSTRUCTION = (
    "用户正在通过语音播报收听回复。请只使用纯文本，"
    "不要使用 Markdown、项目符号星号或标题，使用简短自然的句子。"
)

# L3 — 敷衍/回避用词。命中且终答不含已读正文 → 打回重写。
_EVASION_RE = re.compile(
    r"(安全拦截|Unsafe Login|IMAP设置|IMAP 设置|授权码|被拦截|无法访问|无法获取|无法查看"
    r"|需要授权|登录失败|权限不足|暂时无法|出于隐私|隐私保护|有限制|受限)",
    re.IGNORECASE,
)

# 需要"读正文"的用户意图词；命中写操作词时不算"要求看内容"。
_CONTENT_HINTS = (
    "看", "查看", "读", "内容", "原文", "全文", "正文", "展示", "详情",
    "说了什么", "写了", "讲了", "什么", "总结", "检查", "介绍", "看看",
)
_ACTION_HINTS = (
    "回复", "发送", "草稿", "删除", "归档", "标记", "已读", "未读",
    "转发", "起草", "写封", "回复内容",
)

# 列表/计数类查询：只关心"有没有""几封""哪些"，不需要正文。
# 注意不要收录"最近一周的邮件""看看最近的邮件"——那是要看内容，仍须读正文。
_LIST_ONLY_RE = re.compile(
    r"(有没有|是否有|多少封|几封|收件箱里有|新邮件|未读|邮件列表|有哪些邮件|哪些邮件)"
)
# 明确要正文的说法——即使句子里同时出现"有没有"，也要读原文。
_STRONG_CONTENT_RE = re.compile(
    r"(正文|原文|全文|内容|说了什么|写了什么|讲了什么|详细.{0,3}(看|说|介绍))"
)

# 写入类意图（发送/回复）必须走确认草案；出现这些词即视为写意图，禁用流式，
# 避免被打回的假"已发送"文字以 token 形式提前泄漏给前端。
_WRITE_HINTS = (
    "发送", "发一封", "发给", "写封", "写一封", "寄给", "寄出", "发出",
    "回复", "回信", "回他", "回她", "答复", "send", "reply", "发给",
)

# 发送/回复"成功声称"词。单一来源在 agents.common，direct 分支共用同一份判定，
# 避免两套词表各自漏掉"已帮你发送"这类变体。
_SEND_CLAIM_MARKERS = SEND_CLAIM_MARKERS

# 图内绝不暴露真实发送工具。发送只允许发生在 /approve 里（用户点卡片之后）；
# 模型若能直接调 send_email，就会把用户一句口头"ok"当成授权真发出去——页面上
# 没有卡片，事后也无从核对。剔除后模型只剩 propose_*，声称"已发送"必无证据。
_SEND_CAPABLE_TOOL_NAMES = {"send_email", "reply_to_email"}

_CONFIRM_RETRY_PROMPT = (
    "注意：你刚刚声称邮件已经发送/回复成功，但系统并未获得用户确认，"
    "邮件实际上**没有发出**（发送必须在用户点击确认卡片后才能执行）。"
    "用户打字说「可以」「ok」「确认」**不算**确认，网页卡片没有被点击。"
    "请立刻改写你的回复：明确告诉用户草稿已经准备好（给出收件人、主题、正文要点），"
    "并提示用户点击上方的确认按钮后邮件才会真正发送。"
    "禁止在用户确认前声称「已发送」「已回复」。"
)

# 草案工具共享同一个 AsyncSession：并发 gather 会同时写入同一事务，且互相
# 看不到对方刚落库的行，导致同一意图出现两张确认卡片。必须串行执行。
_PROPOSE_TOOL_NAMES = ("propose_email_draft", "propose_email_reply")

# 任何会写同一个 session 的工具都必须串行；扫描会建邀请和任务。
_SERIAL_SESSION_TOOL_NAMES = (*_PROPOSE_TOOL_NAMES, "scan_meeting_invites")


def _contains_send_claim(text: str) -> bool:
    return contains_send_claim(text)


# ── L1 证据链 ──────────────────────────────────────────────────────────────


def _parse_json(text: str) -> dict:
    try:
        obj = json.loads(text or "")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _truncate_body(text: str) -> str:
    """Cap an email body that will be injected into the LLM context.

    The 163 client already truncates at MAX_BODY_CHARS; this guards the legacy
    Gmail path and anything that passes through here, and avoids double-marking
    when the body already ends with the truncation marker.
    """
    text = text or ""
    if text.endswith(_BODY_TRUNCATION_MARKER):
        return text
    if len(text) <= _MAX_EVIDENCE_BODY_CHARS:
        return text
    return text[:_MAX_EVIDENCE_BODY_CHARS].rstrip() + _BODY_TRUNCATION_MARKER


def _body_tokens(text: str) -> set[str]:
    """Extract distinctive tokens (ASCII words + CJK bigrams) for overlap checks."""
    tokens: set[str] = set()
    tokens.update(re.findall(r"[A-Za-z0-9_]{2,}", text.lower()))
    stripped = re.sub(r"[^一-鿿]", "", text or "")
    tokens.update(stripped[i:i + 2] for i in range(len(stripped) - 1))
    return tokens


def _evidence_summary(read_results: list[str], limit: int = 3, per: int = 160) -> str:
    lines = []
    for i, body in enumerate(read_results[:limit], 1):
        t = " ".join(body.split())
        lines.append(f"{i}. {t[:per]}{'…' if len(t) > per else ''}")
    return "\n".join(lines) or "（无正文）"


def _evidence_template(read_results: list[str]) -> str:
    """Last-resort answer when the model refuses to summarise the bodies.

    Uses the same truncated, numbered summary the retry prompt sees rather than
    dumping raw tool output into the user-visible reply — the raw bodies include
    MIME headers and quoted history the user never asked for.
    """
    return "已读取到的邮件内容摘要：\n" + _evidence_summary(read_results)


def _first_error(raw_errors: list[str], fallback: str) -> str:
    for item in raw_errors:
        if item and item.strip():
            return item.strip()[:300]
    return fallback


def _read_failure_template(raw_errors: list[str]) -> str:
    if not raw_errors:
        return "抱歉，未能读取到邮件内容。请稍后重试。"
    joined = "\n".join(raw_errors[:5])
    return (
        "抱歉，读取邮件内容失败。以下是工具返回的原始错误信息，请根据它处理：\n\n"
        f"{joined}\n\n"
        "如果问题持续，请检查 163 邮箱的 IMAP/SMTP 客户端授权设置（"
        "backend/.env 中的 MAIL163_EMAIL_AUTH_CODE）后重试。"
    )


def _contains_evidence(result: str, read_results: list[str]) -> bool:
    """True if the answer paraphrases enough of an already-read body.

    Uses distinctive-token overlap instead of exact substring so a faithful
    reformulation of the body still keeps L3 happy, while a bare excuse doesn't.
    """
    result_tokens = _body_tokens(result)
    if not result_tokens:
        return False
    for body in read_results:
        body_tokens = _body_tokens(body)
        if not body_tokens:
            continue
        overlap = len(body_tokens & result_tokens) / len(body_tokens)
        if overlap >= 0.5:
            return True
    return False


def _is_evasive(text: str) -> bool:
    return bool(_EVASION_RE.search(text or ""))


def _requires_email_content(intent: str) -> bool:
    text = intent or ""
    # 「看看最近三天有没有新邮件」只是列表/计数查询："看" 命中了 _CONTENT_HINTS，
    # 但它并不要求正文。若按内容请求处理，就会强制 get_email 并把整段原始正文
    # （含 MIME 头、验证码、引用历史）倒给用户。
    if _LIST_ONLY_RE.search(text) and not _STRONG_CONTENT_RE.search(text):
        return False
    has_content = any(h in text for h in _CONTENT_HINTS)
    has_action = any(h in text for h in _ACTION_HINTS)
    return has_content and not has_action


# ── 工具执行 + 证据收集 ────────────────────────────────────────────────────


async def _execute_tool_calls(
    tools_by_name: dict,
    calls: list[dict],
) -> list[tuple[str, str, bool, ToolMessage]]:
    """执行一批 tool_calls。

    普通工具并发执行以省时间；草案工具必须串行——它们共享同一个 AsyncSession，
    并发时彼此看不到刚落库的 PendingAction，幂等校验失效，同一意图会落两行。
    """
    results: list[tuple[str, str, bool, ToolMessage] | None] = [None] * len(calls)
    concurrent_indexes = [
        index for index, call in enumerate(calls)
        if call.get("name") not in _SERIAL_SESSION_TOOL_NAMES
    ]
    serial_indexes = [
        index for index, call in enumerate(calls)
        if call.get("name") in _SERIAL_SESSION_TOOL_NAMES
    ]

    if concurrent_indexes:
        concurrent_results = await asyncio.gather(*[
            _execute_tool(tools_by_name, calls[index]) for index in concurrent_indexes
        ])
        for index, output in zip(concurrent_indexes, concurrent_results):
            results[index] = output

    for index in serial_indexes:
        results[index] = await _execute_tool(tools_by_name, calls[index])

    return [item for item in results if item is not None]


async def _execute_tool(
    tools_by_name: dict,
    call: dict,
    *,
    truncate: bool = True,
) -> tuple[str, str, bool, ToolMessage]:
    """返回 (name, content, ok, tool_message)。content 是截断后的工具返回文本。"""
    name = call.get("name", "")
    call_id = call.get("id", "")
    args = call.get("args") or {}
    target = tools_by_name.get(name)
    ok = True
    raw = ""
    try:
        raw = await target.ainvoke(args) if target else f"未知工具：{name}"
    except Exception as exc:
        logger.error("Email tool %s failed: %s", name, exc)
        raw = f"工具错误：{exc}"
        ok = False
    content = raw if isinstance(raw, str) else str(raw)
    if truncate and len(content) > _MAX_TOOL_OUTPUT_CHARS:
        logger.warning(
            "Email tool %s output truncated: %d → %d chars",
            name, len(content), _MAX_TOOL_OUTPUT_CHARS,
        )
        content = content[:_MAX_TOOL_OUTPUT_CHARS] + "\n...[输出已截断以适配上下文窗口]"
    return name, content, ok, ToolMessage(content=content, tool_call_id=call_id)


def _collect_evidence(
    name: str,
    content: str,
    ok: bool,
    read_results: list[str],
    raw_errors: list[str],
    known_ids: list[str],
) -> None:
    """按工具名把正文收进 read_results / 错误收进 raw_errors / 邮件 ID 收进 known_ids。"""
    txt = content or ""
    if name in ("get_email", "tool_get_email"):
        obj = _parse_json(txt)
        body = (obj.get("body") or "").strip()
        if body:
            read_results.append(_truncate_body(body))
            mid = obj.get("id") or obj.get("message_id")
            if mid:
                known_ids.append(str(mid))
            return
        raw_errors.append(obj.get("error") or txt or "get_email 返回了空内容")
        return
    if name in ("list_emails", "tool_list_emails"):
        obj = _parse_json(txt)
        for m in (obj.get("messages") or []):
            mid = m.get("id")
            if mid:
                known_ids.append(str(mid))
        if obj.get("error"):
            raw_errors.append(obj["error"])
        if not ok:
            raw_errors.append(txt)
        return
    if not ok:
        raw_errors.append(txt)


async def _build_injection(
    known_ids: list[str],
    tools_by_name: dict,
) -> AIMessage | None:
    """L2 强制注入：知道 ID 就直接 get_email；不知道就先 list 一次。"""
    if "list_emails" not in tools_by_name or "get_email" not in tools_by_name:
        return None
    if known_ids:
        mids = known_ids[:MAX_FORCED_GET]
        return AIMessage(content="", tool_calls=[
            {"name": "get_email", "args": {"message_id": mid}, "id": f"forced-get-{i}"}
            for i, mid in enumerate(mids)
        ])
    return AIMessage(content="", tool_calls=[
        {"name": "list_emails", "args": {"query": "", "max_results": 10}, "id": "forced-list"}
    ])


_SYSTEM_CORRECTION = """\
注意：你其实已经成功读取到了这些邮件的正文（见上文 get_email 的工具返回结果）。
不要用任何安全、权限、拦截、授权配置之类的借口回避。
请直接、完整地把下面的正文展示给用户：\n{evidence}"""


def _text_of(message: AIMessage) -> str:
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


def _result_with(
    result: str,
    intent: str,
    meta: dict,
    pending_actions: list | None = None,
    status: str = TASK_STATUS_SUCCESS,
    error: str | None = None,
    task: dict | None = None,
) -> dict:
    return {
        "agent_results": [{
            "route": "email",
            "intent": intent,
            "result": result,
            **meta,
            **task_status_meta(status, error),
        }],
        "pending_actions": pending_actions or [],
        **step_result_update(task or {}, result, status),
    }


async def email_agent_node(
    state: AgentState,
    llm: ChatOpenAI,
    runtime: Runtime | None = None,
) -> dict:
    """运行工具调用循环来完成邮件任务。"""
    task = state.get("current_task") or {}
    intent = task.get("intent", "")
    email_credentials = runtime_value(state, runtime, "email_credentials")
    access_token = runtime_value(state, runtime, "gmail_access_token")
    meta = result_meta(task)
    await mark_task_in_progress_if_persisted(state, task, runtime)

    if not email_credentials and not access_token:
        return _result_with(
            "邮箱尚未连接。请先在 backend/.env 配置 MAIL163_EMAIL 和 MAIL163_EMAIL_AUTH_CODE。",
            intent,
            meta,
            status=TASK_STATUS_FAILED,
            error="邮箱未连接",
            task=task,
        )

    is_voice = state.get("is_voice_request", False)
    settings = runtime_value(state, runtime, "settings")
    today = datetime.now(ZoneInfo(tz_name_of(settings))).strftime("%A, %B %d, %Y %I:%M %p")
    fmt = PLAIN_TEXT_INSTRUCTION if is_voice else MARKDOWN_INSTRUCTION

    system_text = EMAIL_SYSTEM_PROMPT.format(format_instruction=fmt, today=today)
    system_text = f"{system_text}\n{HITL_VERBAL_CONFIRM_RULE}"
    upstream_context = str(state.get("upstream_context") or "")
    if upstream_context:
        system_text = f"{system_text}\n{upstream_context}\n"
    system = SystemMessage(content=system_text)

    # L2 — 要求看内容或写意图（发送/回复）时禁用流式，避免未过门禁的文字
    # 先以 token 形式泄漏给用户。
    require_content = _requires_email_content(intent)
    is_write_intent = any(h in intent for h in _WRITE_HINTS)
    stream_agent = not (require_content or is_write_intent)

    base_tools = (
        make_mail163_email_tools(email_credentials)
        if email_credentials
        else make_email_tools(access_token)
    )
    # 图内不提供真实发送能力：send_email / reply_to_email 只应经 /approve 由用户
    # 点卡片触发。保留它们模型就能绕过 HITL 直接发信，口头"ok"也会被当成授权。
    base_tools = [t for t in base_tools if t.name not in _SEND_CAPABLE_TOOL_NAMES]
    propose_tools = make_propose_email_tools(
        runtime_value(state, runtime, "db_session"),
        state.get("user_id"),
        email_credentials,
    )
    tools = [*base_tools, *propose_tools]
    # 会议邀请扫描是确定性服务，不依赖模型判断哪封邮件是邀请。
    if email_credentials and runtime_value(state, runtime, "db_session") is not None:
        tools.extend(make_meeting_invite_tools(
            runtime_value(state, runtime, "db_session"),
            state.get("user_id"),
            email_credentials,
            settings,
        ))
    agent = llm.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}

    pending_actions: list[dict] = []

    conversation = list(state.get("messages", []))
    work_messages = [system, *conversation, HumanMessage(content=intent)]

    # L1 — 证据链
    read_results: list[str] = []
    raw_errors: list[str] = []
    known_ids: list[str] = []
    forced_rounds = 0

    for round_num in range(MAX_TOOL_ROUNDS):
        response = await astream_invoke(agent, work_messages, stream=stream_agent)
        work_messages.append(response)

        if response.tool_calls:
            tool_outs = await _execute_tool_calls(tools_by_name, response.tool_calls)
            for name, content, ok, tool_message in tool_outs:
                work_messages.append(tool_message)
                _collect_evidence(name, content, ok, read_results, raw_errors, known_ids)
                if name in _PROPOSE_TOOL_NAMES:
                    receipt = _parse_json(content)
                    action_id = receipt.get("action_id")
                    # 同一 action_id 只上报一次，前端就不会渲染两张卡片。
                    if (
                        receipt.get("status") == "pending"
                        and action_id
                        and all(item["action_id"] != action_id for item in pending_actions)
                    ):
                        pending_actions.append({
                            "action_id": action_id,
                            "action_type": receipt.get("action_type"),
                            "status": "pending",
                            "title": receipt.get("to") or "",
                        })
            continue

        result = _text_of(response)

        # HITL 草案已落库但模型返回空文本时，用户会收到一条空气泡。用确定性
        # 文案兜底，明确告知「待确认」，绝不暗示已发送。
        if not result.strip() and pending_actions:
            result = (
                "邮件草稿已准备好（等待你确认后才会真正发送）。"
                "请在确认卡片中点击「确认执行」。"
            )

        # L2 — 要求看内容但没有正文证据，绝不允许终答。
        if require_content and not read_results:
            if forced_rounds >= MAX_FORCED_ROUNDS:
                return _result_with(
                    _read_failure_template(raw_errors),
                    intent,
                    meta,
                    status=TASK_STATUS_FAILED,
                    error=_first_error(raw_errors, "未能读取到邮件内容"),
                    task=task,
                )
            forced_rounds += 1
            injected = await _build_injection(known_ids, tools_by_name)
            if injected is None:
                return _result_with(
                    _read_failure_template(raw_errors),
                    intent,
                    meta,
                    status=TASK_STATUS_FAILED,
                    error=_first_error(raw_errors, "缺少读取邮件所需的工具"),
                    task=task,
                )
            work_messages.append(injected)
            tool_outs = await asyncio.gather(*[
                _execute_tool(tools_by_name, call) for call in injected.tool_calls
            ])
            for name, content, ok, tool_message in tool_outs:
                work_messages.append(tool_message)
                _collect_evidence(name, content, ok, read_results, raw_errors, known_ids)
            continue

        # L3 — 有证据但模型还在敷衍 → 打回重写。
        if require_content and read_results and _is_evasive(result) and not _contains_evidence(result, read_results):
            if forced_rounds < MAX_FORCED_ROUNDS:
                forced_rounds += 1
                work_messages.append(SystemMessage(content=_SYSTEM_CORRECTION.format(
                    evidence=_evidence_summary(read_results),
                )))
                continue
            result = _evidence_template(read_results)

        # HITL — 本轮永远没有"已发送"的证据。
        # 真实发送只发生在 /approve（用户点了网页卡片）之后，而那是另一个请求；
        # 本节点内无论有没有草案回执，都不可能拿到 approved 的执行结果。所以
        # 只要文字声称"已发送"，就是幻觉，无条件打回——不能再像旧逻辑那样
        # 因为"本轮提交过草案"就放行，否则模型跳过卡片直接宣告成功。
        if _contains_send_claim(result):
            if forced_rounds >= MAX_FORCED_ROUNDS:
                if pending_actions:
                    result = (
                        "草稿已准备好（等待你确认后才会真正发送）。"
                        "请在页面上出现的确认卡片中点击「确认执行」。"
                    )
                else:
                    result = (
                        "我没有发送任何邮件：发送必须在确认卡片上点击「确认执行」后才会执行。"
                        "请先让我准备好草稿，再在卡片里确认。"
                    )
                    return _result_with(
                        result,
                        intent,
                        meta,
                        pending_actions=pending_actions,
                        status=TASK_STATUS_FAILED,
                        error="未提交邮件草案",
                        task=task,
                    )
                return _result_with(result, intent, meta, pending_actions=pending_actions, task=task)
            forced_rounds += 1
            work_messages.append(SystemMessage(content=_CONFIRM_RETRY_PROMPT))
            continue

        logger.info("Email agent completed in %d round(s)", round_num + 1)
        return _result_with(result, intent, meta, pending_actions=pending_actions, task=task)

    last = work_messages[-1]
    result = _text_of(last) or "邮件请求已完成。"
    logger.warning("Email agent hit max tool rounds (%d)", MAX_TOOL_ROUNDS)
    # 轮次耗尽仍未收敛：有草案回执算部分完成（等用户确认），否则就是没做完。
    if pending_actions:
        return _result_with(
            result, intent, meta, pending_actions=pending_actions,
            status=TASK_STATUS_PARTIAL, error="工具轮次耗尽，仅完成部分步骤",
            task=task,
        )
    return _result_with(
        result, intent, meta,
        status=TASK_STATUS_FAILED, error="工具轮次耗尽仍未完成",
        task=task,
    )