"""Shared LangGraph invocation for chat and voice endpoints."""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from fastapi import BackgroundTasks
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.common import CONTEXT_ISOLATION_RULE as _SHARED_CONTEXT_ISOLATION_RULE
from agents.compiled_graph_cache import get_compiled_agent_graph
from agents.context import is_tool_payload
from agents.core_intent import build_core_intent
from agents.entity_resolution import resolve_core_intent_destination
from agents.state import AgentState
from agents.stream_collector import (
    TokenCollector,
    reset_active_collector,
    set_active_collector,
)
from config import Settings, has_mail163_credentials
from models.background_job import BackgroundJob
from services.calendar_client import get_valid_access_token as get_valid_calendar_token
from services.calendar_integration_service import get_calendar_integration
from services.gmail_client import get_valid_access_token as get_valid_gmail_token
from services.gmail_integration_service import get_gmail_integration
from services.mail163_client import Mail163Config
from services.memory_service import (
    build_memory_prompt,
    check_or_start_session,
    get_user_bio,
    retrieve_memory,
)
from services.langfuse_tracing import langfuse_trace_scope
from services.langsmith_tracing import (
    build_agent_graph_run_config,
    flush_tracing_after_run,
)
from services.notion_integration_service import get_notion_integration
from services.pending_action_service import get_pending_action_for_user
from services.conversation_service import upsert_conversation_by_client_id
from services.conversation_title_service import (
    is_placeholder_title,
    truncate_title_from_text,
)
from services.post_chat_memory_extraction import (
    run_post_chat_memory_extraction,
    run_post_chat_memory_extraction_deferred,
)
from services.redis_client import get_redis

logger = logging.getLogger(__name__)

_MSG_MAP = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}

# 执行隔离死命令——注入每个工具的 System Prompt：历史里的写操作不得重复执行，
# 但历史里陈述的持续性事实（目的地、所在地、偏好）必须作为上下文沿用。
# 单一来源在 agents.common（orchestrator/planner 也从那里取）。
CONTEXT_ISOLATION_RULE = _SHARED_CONTEXT_ISOLATION_RULE


def _fresh_thread_id(client_conversation_id: str | None) -> str:
    """One checkpointer thread per turn — never reuse the conversation id.

    The graph is compiled with a checkpointer, so a repeated `thread_id` restores
    the previous run's terminal state. `agent_results` and `pending_actions` use
    `operator.add`, so that restore *appends* last turn's agent outputs to this
    turn's: `merge_results_node` then folds yesterday's email digest into a
    weather answer or even "1+1", and the model dutifully recaps it. The message
    window in `_build_timeline_messages` cannot undo that because it only
    controls `messages`.

    Nothing in this app interrupts/resumes a graph run — HITL goes through the
    PendingAction table — so per-turn threads lose no capability. Conversation
    history is reloaded from the client payload instead.
    """
    suffix = uuid.uuid4().hex[:8]
    if client_conversation_id:
        return f"{client_conversation_id}-{suffix}"
    return f"chat-{uuid.uuid4()}"


def _timestamp_label(item: dict, index: int, total: int) -> str:
    """为一条消息构造时间标签。

    - 最后一条用户消息标为 [最新请求]，帮助 LLM 锁定执行目标；
    - 其余带真实 timestamp 的历史消息用 [HH:MM] 标注；
    - 无 timestamp 的历史消息统一标 [本会话背景]。

    历史消息是**上下文**而不是执行目标：标签只区分"本轮要执行什么"，
    不能暗示历史内容可以忽略（旧标签 [历史] 曾让模型把"我要去上海"也丢掉）。
    """
    raw_ts = item.get("timestamp")
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return f"[{dt.strftime('%m-%d %H:%M')}]"
    role = item.get("role", "")
    is_last_user = role == "user" and index == total - 1
    if is_last_user:
        return "[最新请求]"
    return "[本会话背景]"


# 只保留最近 N 轮（一轮 = 一条用户消息及其后的助手回复）的完整对话。
# 更早的历史会被压成一条摘要系统消息：既不丢背景，又不会让模型把几天前的
# 邮件/天气/待确认草案当成这一轮的执行目标（实测会导致答非所问、回复冗长）。
CONTEXT_WINDOW_ROUNDS = 5
_SUMMARY_MAX_ITEMS = 8
_SUMMARY_ITEM_CHARS = 70


def _split_recent_window(
    message_payloads: list[dict],
    rounds: int = CONTEXT_WINDOW_ROUNDS,
) -> tuple[list[dict], list[dict]]:
    """把 payload 切成 (更早的历史, 最近 N 轮完整对话)。"""
    user_indices = [
        index
        for index, item in enumerate(message_payloads)
        if item.get("role") == "user"
    ]
    if len(user_indices) <= rounds:
        return [], list(message_payloads)
    window_start = user_indices[-rounds]
    return list(message_payloads[:window_start]), list(message_payloads[window_start:])


def _summarize_older_history(older: list[dict]) -> SystemMessage | None:
    """把更早的对话压成一条可读摘要（确定性截断，不额外调用 LLM）。"""
    lines: list[str] = []
    for item in older:
        role = item.get("role", "")
        content = item.get("content", "")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = " ".join(content.split())
        if not text:
            continue
        if len(text) > _SUMMARY_ITEM_CHARS:
            text = text[:_SUMMARY_ITEM_CHARS] + "…"
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"- {speaker}：{text}")
    if not lines:
        return None
    # 只留最近的若干条，越早的越不重要，避免摘要本身又变成噪音。
    lines = lines[-_SUMMARY_MAX_ITEMS:]
    return SystemMessage(
        content=(
            "【更早的对话摘要 — 仅作背景】以下内容发生在较早的轮次，已经处理完毕，"
            "其中的任务、提醒、邮件草案、天气查询都不得在本轮重复执行或再次确认；"
            "如与本轮用户消息冲突，一律以本轮为准。\n" + "\n".join(lines)
        )
    )


# 持续性事实：出差/旅行目的地、所在地。它们定义"当前在讨论什么"，一旦被窗口
# 裁剪，模型就会把"我要去上海"忘掉，回头反问用户"要看你去哪儿"。
_CONTEXT_FACT_PATTERNS = (
    re.compile(
        r"(?:我要|我将|我打算|我准备|我计划|计划|打算|准备|下周|明天|后天)?"
        r"(?:去|到|飞往|前往|出差去|搬到|搬去)\s*([一-龥A-Za-z]{2,10})"
    ),
    re.compile(r"(?:我|本人)(?:现在|目前|已经)?(?:住在|身处|人在)\s*([一-龥]{2,10})"),
    re.compile(r"(?:出差|旅行|旅游|度假)(?:的)?(?:目的地|地点)?(?:是|在)\s*([一-龥A-Za-z]{2,10})"),
)
# 命中的"地点"其实是动作/泛称时丢弃：这些词不是目的地。
_CONTEXT_FACT_NON_PLACES = (
    "吃饭", "上班", "下班", "睡觉", "开会", "公司", "学校", "医院", "超市", "商场",
    "厕所", "健身房", "家里", "外面", "哪里", "哪儿", "地方", "这边", "那边",
    "看一下", "看看", "查一下", "一趟", "出差", "旅行", "旅游", "度假",
)
_CONTEXT_FACT_BLOCK_MAX = 6


def _conversation_context_facts(message_payloads: list[dict]) -> list[str]:
    """从**全部**历史（含被窗口裁掉的部分）里提取用户陈述的持续性事实。

    滑动窗口只保留最近几轮完整对话，更早的内容被压成截断摘要，"我要去上海"
    很容易被截在摘要之外。这里用确定性规则兜住这类事实，作为独立上下文块
    注入，保证它不会因为窗口裁剪而消失。
    """
    facts: list[str] = []
    for item in message_payloads:
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        for sentence in re.split(r"[。！？!?\n；;]", content):
            sentence = sentence.strip()
            if not sentence:
                continue
            for pattern in _CONTEXT_FACT_PATTERNS:
                match = pattern.search(sentence)
                if match is None:
                    continue
                place = match.group(1).strip("的了呢啊吧 ，,、")
                if not place or place in _CONTEXT_FACT_NON_PLACES:
                    continue
                fact = f"用户提到：{sentence[:60]}"
                if fact not in facts:
                    facts.append(fact)
                break
    return facts[-_CONTEXT_FACT_BLOCK_MAX:]


def _conversation_context_block(facts: list[str]) -> SystemMessage | None:
    if not facts:
        return None
    return SystemMessage(
        content=(
            "【本会话已确立的上下文 — 必须沿用】以下信息由用户在本会话中亲自说明，"
            "属于当前对话的前提条件，不是待执行的旧任务。回答本轮问题时必须直接使用"
            "这些信息（例如默认地点就是这里提到的目的地），不得反问用户已经说过的事：\n"
            + "\n".join(f"- {fact}" for fact in facts)
        )
    )


def purge_terminal_pending_action_context(
    message_payloads: list[dict],
    message_payloads_existing: list[dict] | None = None,
) -> list[dict]:
    """模块五：把已终结（Approved/Rejected/Expired/Failed）的 PendingAction 从
    本轮上下文里彻底剔除，防止大模型复述已被处理的确认操作。

    前端用卡片按钮触发 approve/reject，消息文本本身不含 action 状态。所以这里
    用确定性命中词净化：任何包含已被处理草案的"标题/摘要"字样的历史助手消息
    都从注入给模型的 payload 里移除——被拒绝的邮件草案不应再让模型重提
    "你刚才拒绝了一个操作"。
    """
    from lib.enums import PendingActionStatus

    terminal = {
        PendingActionStatus.APPROVED,
        PendingActionStatus.REJECTED,
        PendingActionStatus.EXPIRED,
        PendingActionStatus.FAILED,
    }

    # 调用方注入已终结的 action 文本；没有则什么都不清（不产生错误行为）。
    terminal_texts = message_payloads_existing or []
    blocked: list[str] = []
    for item in terminal_texts:
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        status = item.get("status")
        if status in terminal:
            if title and len(title) >= 2:
                blocked.append(title)
            if summary and len(summary) >= 4:
                blocked.append(summary[:200])

    if not blocked:
        return list(message_payloads)

    def _mentions(text: str) -> bool:
        return any(b in text for b in blocked if b)

    cleaned: list[dict] = []
    for item in message_payloads:
        role = item.get("role", "")
        content = item.get("content", "")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            # 用户最新消息不回退：用户本轮刚输入的必须保留。
            if role == "user" and item is message_payloads[-1]:
                cleaned.append(item)
                continue
            if _mentions(content):
                logger.info(
                    "Purged terminal pending-action context from %s message: %.80s",
                    role, " ".join(content.split())[:80],
                )
                continue
        cleaned.append(item)
    return cleaned


async def _load_terminal_pending_action_texts(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[dict]:
    """返回最近已终结（Approved/Rejected/Expired/Failed）的 PendingAction 的
    标题/摘要，供上下文净化使用。查库失败时安全降级为空列表。
    """
    if not isinstance(session, AsyncSession):
        return []
    try:
        from lib.enums import PendingActionStatus
        from services.pending_action_service import list_pending_actions

        terminal = {
            PendingActionStatus.APPROVED,
            PendingActionStatus.REJECTED,
            PendingActionStatus.EXPIRED,
            PendingActionStatus.FAILED,
        }
        actions = await list_pending_actions(session, user_id, limit=20)
        return [
            {
                "title": getattr(a, "title", None),
                "summary": getattr(a, "summary", None),
                "status": getattr(a, "status", None),
                "action_type": getattr(
                    getattr(a, "action_type", None), "value", getattr(a, "action_type", None)
                ),
            }
            for a in actions
            if getattr(a, "status", None) in terminal
        ]
    except Exception as exc:
        logger.warning("Failed to load terminal pending actions for purge: %s", exc)
        return []


def _build_timeline_messages(message_payloads: list[dict]) -> list[BaseMessage]:
    """把 payload 组装为带时间隔离标签的 LangChain 消息。

    只保留最近 `CONTEXT_WINDOW_ROUNDS` 轮的完整对话，更早的历史压缩成一条
    摘要系统消息；每条用户/助手消息再前缀 `[时间]`/`[最新请求]`/`[本会话背景]`，
    让 LLM 明确分辨"当前请求" vs "会话背景"。另外单独注入一条确定性的
    "本会话已确立的上下文"块，确保目的地这类持续性事实不会被窗口裁剪掉。
    """
    older, recent = _split_recent_window(message_payloads)
    total = len(recent)
    messages: list[BaseMessage] = []
    summary = _summarize_older_history(older)
    if summary is not None:
        messages.append(summary)
    context_block = _conversation_context_block(
        _conversation_context_facts(message_payloads)
    )
    if context_block is not None:
        messages.append(context_block)
    for index, item in enumerate(recent):
        role = item.get("role", "")
        content = item.get("content", "")
        cls = _MSG_MAP.get(role)
        if cls is None:
            continue
        # 模块六：只保留对话文本流。工具调用的原始长 JSON（高德完整返回等）
        # 绝不进历史——否则 Token 暴涨，且模型会去历史里"考古"被上一轮旧
        # JSON 带偏。工具的结论已由 agent 以自然语言写进最终回复。
        if role in ("user", "assistant") and is_tool_payload(
            content if isinstance(content, str) else str(content)
        ):
            continue
        label = _timestamp_label(item, index, total)
        body = content if isinstance(content, str) else str(content)
        if role in ("user", "assistant") and body.strip():
            body = f"{label} {role}: {body}"
        messages.append(cls(content=body))
    return messages

# Redis TTLs for cached OAuth tokens
_TOKEN_TTL_GOOGLE = 3300   # 55 min — Google tokens expire in 60 min
_TOKEN_TTL_NOTION = 86400  # 24 h  — Notion tokens are long-lived


def has_database(settings: Settings) -> bool:
    return bool(settings.database_url)


def has_google_credentials(settings: Settings) -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def build_mail163_config(settings: Settings) -> Mail163Config | None:
    if not has_mail163_credentials(settings):
        return None
    return Mail163Config(
        email=settings.mail163_email or "",
        auth_code=settings.mail163_email_auth_code or "",
        smtp_host=settings.mail163_smtp_host,
        smtp_port=settings.mail163_smtp_port,
        imap_host=settings.mail163_imap_host,
        imap_port=settings.mail163_imap_port,
        timeout_seconds=settings.mail163_email_timeout_seconds,
    )


def has_memory_system(settings: Settings) -> bool:
    return bool(settings.database_url and settings.voyageai_api_key)


async def load_memory_context(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    latest_user_message: str,
) -> str:
    """Fetch bio + memory chunks and format for system prompt injection."""
    await check_or_start_session(user_id)

    if latest_user_message.strip():
        bio, chunks = await asyncio.gather(
            get_user_bio(session, user_id),
            retrieve_memory(session, settings, user_id, latest_user_message),
        )
    else:
        bio = await get_user_bio(session, user_id)
        chunks = []

    return build_memory_prompt(bio, chunks)


def build_scheduled_job_user_payload(job: BackgroundJob) -> list[dict[str, Any]]:
    """Synthetic chat messages for a scheduler-triggered graph run."""
    actions_text = json.dumps(list(job.actions or []), ensure_ascii=False)
    return [
        {
            "role": "user",
            "content": (
                f"[Scheduled background job: {job.title}]\n\n{job.instruction}\n\n"
                f"Planned actions (chips; context only): {actions_text}"
            ),
        },
    ]


def extract_latest_user_message(payloads: list[dict[str, Any]]) -> str:
    for message in reversed(payloads):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


# ── Token helpers with Redis caching ─────────────────────────────────────


async def _get_cached_token(
    cache_key: str,
    ttl: int,
    fetch_fn,
) -> str | None:
    """Return a cached token from Redis, falling back to DB fetch on miss."""
    redis = get_redis()
    if redis:
        try:
            cached = await redis.get(cache_key)
            if cached:
                logger.debug("Token cache hit: %s", cache_key)
                return cached  # decode_responses=True returns str directly
        except Exception as exc:
            logger.warning("Redis token cache read failed (%s): %s", cache_key, exc)

    token = await fetch_fn()

    if token and redis:
        try:
            await redis.setex(cache_key, ttl, token)
            logger.debug("Token cached: %s (TTL %ds)", cache_key, ttl)
        except Exception as exc:
            logger.warning("Redis token cache write failed (%s): %s", cache_key, exc)

    return token


async def _fetch_gmail_access_token(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
) -> str | None:
    if not has_google_credentials(settings):
        return None
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    if not client_id or not client_secret:
        return None
    try:
        integration = await get_gmail_integration(session, user_id)
        if integration and integration.is_connected:
            return await get_valid_gmail_token(session, integration, client_id, client_secret)
    except Exception as exc:
        logger.warning("Could not fetch Gmail token for user %s: %s", user_id, exc)
    return None


async def _fetch_calendar_access_token(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
) -> str | None:
    if not has_google_credentials(settings):
        return None
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    if not client_id or not client_secret:
        return None
    try:
        integration = await get_calendar_integration(session, user_id)
        if integration and integration.is_connected:
            return await get_valid_calendar_token(session, integration, client_id, client_secret)
    except Exception as exc:
        logger.warning("Could not fetch Calendar token for user %s: %s", user_id, exc)
    return None


async def _fetch_notion_access_token(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> str | None:
    try:
        integration = await get_notion_integration(session, user_id)
        if integration and integration.is_connected:
            return integration.access_token
    except Exception as exc:
        logger.warning("Could not fetch Notion token for user %s: %s", user_id, exc)
    return None


async def get_all_tokens(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
) -> tuple[str | None, str | None, str | None]:
    """Fetch gmail, calendar, and notion tokens in parallel with Redis caching."""
    uid = str(user_id)
    gmail_key = f"cos:token:gmail:{uid}"
    calendar_key = f"cos:token:calendar:{uid}"
    notion_key = f"cos:token:notion:{uid}"

    gmail_token, calendar_token, notion_token = await asyncio.gather(
        _get_cached_token(
            gmail_key,
            _TOKEN_TTL_GOOGLE,
            lambda: _fetch_gmail_access_token(session, settings, user_id),
        ),
        _get_cached_token(
            calendar_key,
            _TOKEN_TTL_GOOGLE,
            lambda: _fetch_calendar_access_token(session, settings, user_id),
        ),
        _get_cached_token(
            notion_key,
            _TOKEN_TTL_NOTION,
            lambda: _fetch_notion_access_token(session, user_id),
        ),
    )
    # Fall back to the static internal-integration token (NOTION_API_KEY) when the
    # user has not connected Notion via OAuth. Deliberately not cached in Redis so
    # the OAuth token immediately takes precedence once the user connects.
    if not notion_token:
        notion_token = settings.notion_api_key or None
    return gmail_token, calendar_token, notion_token


async def invalidate_token_cache(user_id: uuid.UUID, provider: str) -> None:
    """Delete a cached token when an integration is disconnected."""
    redis = get_redis()
    if not redis:
        return
    key = f"cos:token:{provider}:{user_id}"
    try:
        await redis.delete(key)
        logger.info("Invalidated token cache: %s", key)
    except Exception as exc:
        logger.warning("Could not invalidate token cache %s: %s", key, exc)


# ── Graph invocation ──────────────────────────────────────────────────────


async def invoke_chat_agent_state(
    *,
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
    message_payloads: list[dict[str, Any]],
    model: str | None = None,
    is_voice_request: bool = False,
    inline_memory_extraction: bool = False,
    trace_tags: list[str] | None = None,
    trace_metadata: dict[str, Any] | None = None,
    client_conversation_id: str | None = None,
) -> AgentState:
    """Build initial state, run the compiled graph, return terminal state.
    模块五/六：净化终结 pending-action 上下文，构建核心意图（业务状态），
    messages 只保留对话文本流。
    """
    resolved_model = model or settings.openrouter_default_model
    thread_id = _fresh_thread_id(client_conversation_id)

    terminal_actions = (
        await _load_terminal_pending_action_texts(session, user_id)
        if has_database(settings)
        else []
    )
    clean_payloads = purge_terminal_pending_action_context(
        message_payloads, terminal_actions,
    )
    messages = _build_timeline_messages(clean_payloads)
    # 混合路由：正则抽到的地名可能只是时间词（「周二」），低置信度时用 LLM
    # 结合历史目的地做二次推理，再作为强先验注入所有 agent 的 prompt。
    core_intent = build_core_intent(clean_payloads)
    core_intent = await resolve_core_intent_destination(settings, core_intent) or core_intent

    memory_context = ""
    if has_memory_system(settings):
        try:
            latest_user_text = extract_latest_user_message(clean_payloads)
            memory_context = await load_memory_context(session, settings, user_id, latest_user_text)
            if memory_context:
                messages.insert(0, SystemMessage(content=memory_context))
        except Exception as exc:
            logger.warning("Memory injection failed for user %s: %s", user_id, exc)

    gmail_access_token: str | None = None
    calendar_access_token: str | None = None
    notion_access_token: str | None = None

    if has_database(settings):
        gmail_access_token, calendar_access_token, notion_access_token = await get_all_tokens(
            session, settings, user_id
        )

    db_for_state = session if has_database(settings) else None
    settings_for_state = settings if has_database(settings) else None

    initial_state: AgentState = {
        "user_id": user_id,
        "messages": messages,
        "is_voice_request": is_voice_request,
        "memory_context": memory_context,
        "core_intent": core_intent,
    }

    runtime_context = {
        "db_session": db_for_state,
        "settings": settings_for_state,
        "email_credentials": build_mail163_config(settings),
        "gmail_access_token": gmail_access_token,
        "calendar_access_token": calendar_access_token,
        "notion_access_token": notion_access_token,
    }

    graph = await get_compiled_agent_graph(resolved_model)
    run_name = "voice-turn" if is_voice_request else "chat-turn"
    # Sessions group traces of one conversation; a throwaway thread id is not one.
    session_id = client_conversation_id
    run_config = build_agent_graph_run_config(
        settings,
        user_id=user_id,
        trace_tags=trace_tags,
        trace_metadata=trace_metadata,
        thread_id=thread_id,
        session_id=session_id,
        run_name=run_name,
    )
    try:
        with langfuse_trace_scope(
            settings,
            user_id=user_id,
            session_id=session_id,
            trace_name=run_name,
            trace_input=extract_latest_user_message(message_payloads),
            tags=trace_tags,
        ) as trace_scope:
            result_state = await graph.ainvoke(
                initial_state, run_config, context=runtime_context
            )
            trace_scope.set_output(
                result_state.get("final_answer") or result_state.get("error")
            )
    finally:
        flush_tracing_after_run(run_config)

    if inline_memory_extraction and has_memory_system(settings) and has_database(settings):
        final_text = result_state.get("final_answer", "")
        if final_text.strip():
            await run_post_chat_memory_extraction(
                session,
                settings,
                user_id,
                message_payloads,
                final_text,
            )

    return result_state


async def run_chat_agent(
    *,
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
    message_payloads: list[dict[str, Any]],
    model: str | None = None,
    is_voice_request: bool = False,
    client_conversation_id: str | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> str:
    """Run the Chief of Staff graph and return the assistant reply text."""
    if client_conversation_id:
        logger.info(
            "Starting chat agent run user=%s client_conversation_id=%s voice=%s",
            user_id,
            client_conversation_id,
            is_voice_request,
        )
    trace_tags = ["voice"] if is_voice_request else ["chat"]
    trace_metadata: dict[str, Any] | None = None
    if client_conversation_id:
        trace_metadata = {"client_conversation_id": client_conversation_id}

    result_state = await invoke_chat_agent_state(
        settings=settings,
        session=session,
        user_id=user_id,
        message_payloads=message_payloads,
        model=model,
        is_voice_request=is_voice_request,
        inline_memory_extraction=False,
        trace_tags=trace_tags,
        trace_metadata=trace_metadata,
        client_conversation_id=client_conversation_id,
    )

    final_answer = result_state.get("final_answer", "")
    if not final_answer:
        final_answer = result_state.get("error", "Sorry, something went wrong.")

    if (
        background_tasks is not None
        and has_memory_system(settings)
        and has_database(settings)
        and final_answer.strip()
    ):
        background_tasks.add_task(
            run_post_chat_memory_extraction_deferred,
            user_id,
            message_payloads,
            final_answer,
        )

    return final_answer


# ── SSE streaming ─────────────────────────────────────────────────────────

_NODE_STATUS_MESSAGES: dict[str, str] = {
    "orchestrate": "Figuring out what you need...",
    "planner": "Breaking this into a plan...",
    "email_agent": "Fetching your emails...",
    "calendar_agent": "Checking your calendar...",
    "notion_agent": "Searching Notion...",
    "direct_reply": "Composing a reply...",
    "life_agent": "Checking weather and locations...",
    "supervisor": "Reviewing execution progress...",
    "merge_results": "Putting it all together...",
}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _complete_transcript(
    message_payloads: list[dict[str, Any]],
    final_answer: str,
) -> list[dict[str, str]]:
    """Full user+assistant transcript to persist (drops empty/short messages).

    Preserves the per-message `timestamp` when present so restored history can
    still be time-isolated on later turns (prevents yesterday's request leaking
    into today's execution target).
    """
    messages: list[dict[str, str]] = []
    for m in message_payloads:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip():
            item: dict[str, str] = {"role": m["role"], "content": m["content"]}
            raw_ts = m.get("timestamp")
            if isinstance(raw_ts, str) and raw_ts.strip():
                item["timestamp"] = raw_ts
            messages.append(item)
    if final_answer.strip():
        messages.append({"role": "assistant", "content": final_answer.strip()})
    return messages


async def persist_chat_transcript(
    *,
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    client_conversation_id: str | None,
    message_payloads: list[dict[str, Any]],
    final_answer: str,
) -> None:
    """Atomically upsert the run's transcript keyed by the client conversation id.

    Runs *before* the SSE "done" event and commits explicitly so the chat is
    durable even if the client aborts the stream mid-way (route switch, etc.).
    Never raises: any failure is logged and the stream continues.
    """
    if not client_conversation_id or not has_database(settings):
        return
    transcript = _complete_transcript(message_payloads, final_answer)
    if not transcript:
        return
    try:
        first_user_text = next(
            (m["content"] for m in transcript if m["role"] == "user"),
            "",
        )
        await upsert_conversation_by_client_id(
            session,
            user_id=user_id,
            client_conversation_id=client_conversation_id,
            messages=transcript,
            settings=settings,
            title=truncate_title_from_text(first_user_text),
        )
        await session.commit()
        logger.info(
            "Persisted chat transcript for user=%s client_conversation_id=%s messages=%d",
            user_id,
            client_conversation_id,
            len(transcript),
        )
    except Exception:
        logger.warning(
            "Failed to persist chat transcript for user=%s conversation=%s",
            user_id,
            client_conversation_id,
            exc_info=True,
        )


# 后台任务（标题精修、记忆提取）与整轮生产任务的强引用：只被事件循环弱引用
# 的任务随时可能被 GC 回收，兜底等于没做。
_DETACHED_TASKS: set[asyncio.Task] = set()


def _schedule_detached(coro) -> asyncio.Task | None:
    """Run a coroutine in a task that outlives the current request."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    task = loop.create_task(coro)
    _DETACHED_TASKS.add(task)
    task.add_done_callback(_DETACHED_TASKS.discard)
    return task


async def refine_conversation_title_deferred(
    user_id: uuid.UUID,
    client_conversation_id: str,
    first_user_text: str,
) -> None:
    """Background task: upgrade a truncation title to an LLM-concise one."""
    try:
        from config import get_settings
        from db.session import get_async_session_maker
        from services.conversation_service import _conversation_by_client_id_for_user

        settings = get_settings()
        if not settings.database_url or not settings.openrouter_api_key:
            return
        session_maker = get_async_session_maker()
        if session_maker is None:
            return
        async with session_maker() as session:
            conversation = await _conversation_by_client_id_for_user(
                session,
                user_id=user_id,
                client_conversation_id=client_conversation_id,
            )
            if conversation is None:
                return
            from services.conversation_title_service import (
                generate_conversation_title,
                is_placeholder_title,
                truncate_title_from_text,
            )

            current = conversation.title
            truncated = truncate_title_from_text(first_user_text)
            # Keep an explicit user custom title; only upgrade a placeholder or a
            # still-truncated (never-LLM-refined) title.
            if current is not None and not is_placeholder_title(current) and current != truncated:
                return
            llm_title = await generate_conversation_title(settings, first_user_text)
            if not llm_title:
                return
            conversation.title = llm_title[:500]
            await session.commit()
            logger.info(
                "Refined conversation title for user=%s conversation=%s -> %s",
                user_id,
                client_conversation_id,
                llm_title,
            )
    except Exception:
        logger.warning(
            "Conversation title refinement failed for user=%s conversation=%s",
            user_id,
            client_conversation_id,
            exc_info=True,
        )


def _refine_first_message_text(message_payloads: list[dict[str, Any]]) -> str:
    for message in message_payloads:
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip().splitlines()[0][:80]
    return ""


_STREAM_END = object()

# 断线续传：每个会话一个事件缓冲。SSE 每条事件带自增 id，最近 N 条留在缓冲里。
# 客户端（尤其是 fetch 流式读取，非 EventSource）重连时带上 `Last-Event-ID`，
# 后端据此把缺失的事件补发出去——只补 id 大于该值的事件，不重放、不遗漏。
# 用进程内 deque 实现：无需 Redis 也能工作；Redis 只是可选的外部持久层。
STREAM_BUFFER_MAX = 100


class _ConversationStream:
    """A conversation's live event buffer + subscriber fan-out.

    生产端（`_produce_chat_turn`）把事件 `publish` 到这个对象；
    每个订阅者（一次 HTTP SSE 连接）拿到自己的队列，互不影响。
    事件按发布顺序带单调递增 id，缓冲最后 `STREAM_BUFFER_MAX` 条以支持重连补发。
    """

    def __init__(self) -> None:
        self.events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=STREAM_BUFFER_MAX)
        self._next_id = 1
        self._subscribers: list[asyncio.Queue] = []
        self.producer_task: asyncio.Task | None = None
        self.done = False

    def publish(self, payload: dict[str, Any]) -> None:
        event_id = self._next_id
        self._next_id += 1
        self.events.append((event_id, payload))
        for queue in list(self._subscribers):
            queue.put_nowait((event_id, payload))
        if payload.get("type") in ("done", "error"):
            self.done = True

    def subscribe(self) -> tuple[asyncio.Queue, list[tuple[int, dict]]]:
        """注册订阅者并返回 (队列, 当前缓冲快照)。

        缓冲快照与注册之间没有 await，因此在 asyncio 单线程模型下不会与
        `publish` 交错：快照里的事件一定早于注册点，之后的事件只会进队列，
        不会既进快照又进队列。
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue, list(self.events)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def replay_after(self, last_event_id: int | None) -> list[tuple[int, dict]]:
        if last_event_id is None:
            return []
        return [(eid, payload) for eid, payload in self.events if eid > last_event_id]

    def subscriber_count(self) -> int:
        """当前还有几条 SSE 连接挂着——用来判断这一轮是否已经没人要了。"""
        return len(self._subscribers)

    def close(self) -> None:
        self.done = True
        for queue in list(self._subscribers):
            queue.put_nowait(_STREAM_END)


# conversation_id -> _ConversationStream。生产端跑完不清除：这样客户端在
# 生产端已结束后重连，仍能从缓冲补齐最后一小段事件。
# 但会话数量会随使用无限增长，这里给已完成会话设上限，按插入顺序淘汰最旧的。
_STREAM_REGISTRY: dict[str, _ConversationStream] = {}
_STREAM_REGISTRY_MAX_COMPLETED = 200


def _evict_finished_streams() -> None:
    """Drop the oldest finished streams so the registry cannot grow unbounded."""
    finished = [
        key for key, stream in _STREAM_REGISTRY.items() if stream.done
    ]
    overflow = len(finished) - _STREAM_REGISTRY_MAX_COMPLETED
    for key in finished[: max(0, overflow)]:
        _STREAM_REGISTRY.pop(key, None)


def reset_stream_registry() -> None:
    """测试用：清空会话事件缓冲。"""
    _STREAM_REGISTRY.clear()


# 客户端断开后留给"断线续传"的宽限窗口：前端自动重连（600ms 退避）通常在这
# 之内完成。窗口内没人重新订阅，说明用户按了停止或彻底走了，这一轮就该取消，
# 否则 LangGraph 会继续跑、连接和 token 白烧。
DISCONNECT_CANCEL_GRACE_SECONDS = 3.0


def cancel_conversation_turn(
    conversation_key: str,
    *,
    reason: str = "已停止生成。",
) -> bool:
    """取消一个会话正在跑的这一轮，返回是否真的取消了。

    只取消生产端 task：它抛 `CancelledError` → 事务回滚 → `finally` 里关流，
    数据库连接和 LangGraph 的执行栈随之释放。缓冲同时从注册表摘掉，避免
    客户端之后带 Last-Event-ID 重连到一条已经作废的流。
    """
    stream = _STREAM_REGISTRY.pop(conversation_key, None)
    if stream is None:
        return False

    task = stream.producer_task
    is_running = task is not None and not task.done()
    if is_running:
        task.cancel()
    stream.publish({"type": "cancelled", "message": reason})
    stream.close()
    return is_running


async def _cancel_turn_if_silent(
    stream: _ConversationStream,
    conversation_key: str,
) -> None:
    """断线宽限期结束后仍无订阅者 → 取消生产端。"""
    await asyncio.sleep(DISCONNECT_CANCEL_GRACE_SECONDS)
    if stream.done or stream.subscriber_count() > 0:
        return
    # 期间可能有新的一轮覆盖了同名 key，别误杀。
    if _STREAM_REGISTRY.get(conversation_key) is not stream:
        return
    logger.info(
        "Cancelling turn after %.1fs without subscribers (conversation=%s)",
        DISCONNECT_CANCEL_GRACE_SECONDS,
        conversation_key,
    )
    cancel_conversation_turn(conversation_key, reason="连接已断开，生成已停止。")


def _coerce_last_event_id(raw: Any) -> int | None:
    """把 Last-Event-ID（HTTP 头是字符串）解析为整数，非法值当作无。"""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _sse(event_id: int, payload: dict) -> str:
    """SSE 帧：带自增 `id:` 行，浏览器 / 客户端据此在重连时回传 Last-Event-ID。"""
    return f"id: {event_id}\ndata: {json.dumps(payload)}\n\n"


async def _subscribe_stream(
    stream: _ConversationStream,
    last_event_id: int | None,
):
    """把一条 SSE 连接接入某个会话流：先补发缺失事件，再持续转发新事件。

    重连场景下只补 id > last_event_id 的事件，绝不从头重放。
    """
    queue, snapshot = stream.subscribe()
    try:
        if last_event_id is None:
            backlog = snapshot
        else:
            backlog = [(eid, payload) for eid, payload in snapshot if eid > last_event_id]
        for event_id, payload in backlog:
            yield _sse(event_id, payload)
        # 缓冲已经覆盖到收尾事件 → 无需再挂队列（`done`/`error` 已在快照里）。
        if any(payload.get("type") in ("done", "error") for _, payload in backlog):
            return
        while True:
            item = await queue.get()
            if item is _STREAM_END:
                return
            event_id, payload = item
            yield _sse(event_id, payload)
    finally:
        stream.unsubscribe(queue)


async def stream_chat_agent(
    *,
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
    message_payloads: list[dict[str, Any]],
    model: str | None = None,
    client_conversation_id: str | None = None,
    last_event_id: int | str | None = None,
) -> AsyncGenerator[str, None]:
    """Yield SSE events (with monotonic ids) while the turn runs in a detached producer.

    事件形状（每条都带自增 `id:`）：
      {"type": "status", "message": "..."}      node progress updates
      {"type": "token",  "content": "..."}      incremental reply token deltas
      {"type": "done",   "content": "...", "elapsed": <seconds>}  final answer
      {"type": "error",  "message": "..."}

    断线续传：传 `last_event_id`（来自客户端 `Last-Event-ID` 头）时，本连接
    作为**重连订阅者**接入已有会话流，只补发 id 大于它的事件。没有它时才新建
    生产端（说明这是这一轮的首连）。

    这一层只是**订阅者**。真正的生成（图执行 + 落库）跑在独立 task、独立数据库
    会话里，所以单条连接的断开不会把事务一起回滚。

    停止语义：用户点「停止」走 `POST /chat/cancel` 立刻取消；仅连接断开时给
    `DISCONNECT_CANCEL_GRACE_SECONDS` 的宽限窗口，等待自动重连续上，超时
    仍无人订阅才取消，避免切页/断网白烧一轮生成。
    """
    conv_key = client_conversation_id or f"anon-{uuid.uuid4().hex}"
    parsed_last_id = _coerce_last_event_id(last_event_id)

    stream = _STREAM_REGISTRY.get(conv_key)
    if parsed_last_id is not None:
        if stream is not None:
            # 重连：接入既有会话流，只补 id > last_event_id 的事件。
            async for chunk in _subscribe_stream(stream, parsed_last_id):
                yield chunk
            return
        # 带 Last-Event-ID 却没有流（进程重启/缓冲被淘汰）：这一轮大概率已经
        # 跑完并落库了。绝不能在这里新建生产端，否则会重复执行侧写操作
        # （再建一遍日程/提醒）。让客户端回服务端拉取权威记录。
        logger.info(
            "Reconnect for unknown stream (conversation=%s); instructing client to resync",
            client_conversation_id,
        )
        yield _sse(
            0,
            {
                "type": "error",
                "message": "连接已过期，请刷新页面查看已保存的回复。",
                "resync": True,
            },
        )
        return

    # 首连：新建生产端。先注册缓冲、再启动生产，避免开头几条事件丢失。
    _evict_finished_streams()
    stream = _ConversationStream()
    _STREAM_REGISTRY[conv_key] = stream
    stream.producer_task = _schedule_detached(
        _produce_chat_turn(
            settings=settings,
            session=session,
            user_id=user_id,
            message_payloads=list(message_payloads),
            model=model,
            client_conversation_id=client_conversation_id,
            publish=stream.publish,
            finish=stream.close,
        )
    )

    try:
        async for chunk in _subscribe_stream(stream, None):
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        logger.info(
            "SSE consumer aborted (conversation=%s); grace-cancelling if nobody reconnects",
            client_conversation_id,
        )
        # 断开信号在 Starlette 取消本生成器时到达。先等一个宽限窗口：前端自动
        # 重连会在窗口内重新订阅，此时不该杀掉这一轮；窗口过后仍无人订阅，
        # 才取消生产端并把连接/执行栈一起释放。
        _schedule_detached(_cancel_turn_if_silent(stream, conv_key))
        raise


def _get_session_maker_for_turn():
    """App-wide session factory for the detached producer task (None without DB)."""
    from db.session import get_async_session_maker

    return get_async_session_maker()


async def _produce_chat_turn(
    *,
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
    message_payloads: list[dict[str, Any]],
    model: str | None,
    client_conversation_id: str | None,
    publish,
    finish,
) -> None:
    """Run one turn to completion in a task that outlives the HTTP request.

    必须用**独立会话**：请求级会话在客户端断开时会被 `get_session` 回滚，
    连日程、任务这些副作用都会一起消失。这里自己开事务，跑完统一提交。
    事件通过 `publish` 发到会话缓冲（带自增 id，可重连补发）。
    """
    session_maker = _get_session_maker_for_turn()
    # 本轮草案要记到自己所属的对话上：切页/刷新后前端只能按 conversation_id
    # 找回未确认的卡片。propose_* 工具在节点深处执行，拿不到 state，用 ContextVar
    # 传递，避免把 conversation_id 一路加进每个工具签名。
    from services.pending_action_service import CURRENT_CONVERSATION_ID

    conversation_token = CURRENT_CONVERSATION_ID.set(client_conversation_id)
    try:
        if session_maker is None:
            await _run_chat_turn(
                settings=settings,
                session=session,
                user_id=user_id,
                message_payloads=message_payloads,
                model=model,
                client_conversation_id=client_conversation_id,
                publish=publish,
            )
            return
        async with session_maker() as own_session:
            try:
                await _run_chat_turn(
                    settings=settings,
                    session=own_session,
                    user_id=user_id,
                    message_payloads=message_payloads,
                    model=model,
                    client_conversation_id=client_conversation_id,
                    publish=publish,
                )
                await own_session.commit()
            except BaseException:
                await own_session.rollback()
                raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Chat turn failed for user=%s conversation=%s", user_id, client_conversation_id
        )
        publish({"type": "error", "message": "发生错误，请重试。"})
    finally:
        CURRENT_CONVERSATION_ID.reset(conversation_token)
        finish()


async def _run_chat_turn(
    *,
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
    message_payloads: list[dict[str, Any]],
    model: str | None,
    client_conversation_id: str | None,
    publish,
) -> None:
    start_time = time.monotonic()

    if client_conversation_id:
        logger.info(
            "Starting streaming chat run user=%s conversation=%s",
            user_id,
            client_conversation_id,
        )

    # 先把用户这一轮的问题落库：即使随后生成失败，用户也不会觉得
    # "我明明发了消息却没了"。
    await persist_chat_transcript(
        session=session,
        settings=settings,
        user_id=user_id,
        client_conversation_id=client_conversation_id,
        message_payloads=message_payloads,
        final_answer="",
    )

    publish({"type": "status", "message": "Thinking..."})

    trace_tags = ["chat"]
    trace_metadata: dict[str, Any] | None = (
        {"client_conversation_id": client_conversation_id} if client_conversation_id else None
    )

    resolved_model = model or settings.openrouter_default_model
    thread_id = _fresh_thread_id(client_conversation_id)

    # 模块五/六：净化终结 pending-action 上下文，业务状态（core_intent）进
    # State 的一等公民字段，messages 只留对话文本流。
    terminal_actions = (
        await _load_terminal_pending_action_texts(session, user_id)
        if has_database(settings)
        else []
    )
    clean_payloads = purge_terminal_pending_action_context(
        message_payloads, terminal_actions,
    )
    messages = _build_timeline_messages(clean_payloads)
    # 同非流式路径：低置信度地名先做 LLM 二次推理再进图，图内流转不变。
    core_intent = build_core_intent(clean_payloads)
    core_intent = await resolve_core_intent_destination(settings, core_intent) or core_intent

    memory_context = ""
    if has_memory_system(settings):
        try:
            latest_user_text = extract_latest_user_message(clean_payloads)
            memory_context = await load_memory_context(session, settings, user_id, latest_user_text)
            if memory_context:
                messages.insert(0, SystemMessage(content=memory_context))
        except Exception as exc:
            logger.warning("Memory injection failed for user %s: %s", user_id, exc)

    gmail_access_token = calendar_access_token = notion_access_token = None
    if has_database(settings):
        gmail_access_token, calendar_access_token, notion_access_token = await get_all_tokens(
            session, settings, user_id
        )

    db_for_state = session if has_database(settings) else None
    settings_for_state = settings if has_database(settings) else None

    initial_state: AgentState = {
        "user_id": user_id,
        "messages": messages,
        "is_voice_request": False,
        "memory_context": memory_context,
        "core_intent": core_intent,
    }

    runtime_context = {
        "db_session": db_for_state,
        "settings": settings_for_state,
        "email_credentials": build_mail163_config(settings),
        "gmail_access_token": gmail_access_token,
        "calendar_access_token": calendar_access_token,
        "notion_access_token": notion_access_token,
    }

    graph = await get_compiled_agent_graph(resolved_model)
    run_config = build_agent_graph_run_config(
        settings,
        user_id=user_id,
        trace_tags=trace_tags,
        trace_metadata=trace_metadata,
        thread_id=thread_id,
        session_id=client_conversation_id,
        run_name="chat-turn",
    )

    final_answer = ""
    seen_nodes: set[str] = set()
    # 出错时用它拼出已经生成的部分回复，避免整轮对话丢失。
    streamed_chunks: list[str] = []
    collector = TokenCollector(on_token=streamed_chunks.append)
    cv_token = set_active_collector(collector)
    stream_failed = False
    # HITL 草案引用：action_id -> 简略摘要（去重，供后续拉全量发 SSE）。
    pending_action_refs: dict[str, dict] = {}

    def _emit_tokens() -> None:
        for token in collector.drain_nowait():
            publish({"type": "token", "content": token})

    try:
        with langfuse_trace_scope(
            settings,
            user_id=user_id,
            session_id=client_conversation_id,
            trace_name="chat-turn",
            trace_input=extract_latest_user_message(message_payloads),
            tags=trace_tags,
        ) as trace_scope:
            async for event in graph.astream_events(
                initial_state,
                run_config,
                version="v2",
                context=runtime_context,
            ):
                ev_type = event.get("event", "")
                node = event.get("metadata", {}).get("langgraph_node", "")

                if (
                    ev_type == "on_chain_start"
                    and node in _NODE_STATUS_MESSAGES
                    and node not in seen_nodes
                ):
                    seen_nodes.add(node)
                    publish({"type": "status", "message": _NODE_STATUS_MESSAGES[node]})

                if ev_type == "on_chain_end":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict):
                        answer = output.get("final_answer")
                        if answer:
                            final_answer = answer
                        for item in output.get("pending_actions") or []:
                            action_id = item.get("action_id")
                            if action_id:
                                pending_action_refs[str(action_id)] = item

                _emit_tokens()
            trace_scope.set_output(final_answer or None)
    except Exception as exc:
        logger.error("Streaming agent error for user %s: %s", user_id, exc)
        stream_failed = True
    finally:
        reset_active_collector(cv_token)
        flush_tracing_after_run(run_config)

    _emit_tokens()

    if stream_failed:
        # 出错也不能让这一轮凭空消失：把用户问题 + 已生成的部分回复落库。
        await persist_chat_transcript(
            session=session,
            settings=settings,
            user_id=user_id,
            client_conversation_id=client_conversation_id,
            message_payloads=message_payloads,
            final_answer=final_answer or "".join(streamed_chunks),
        )
        publish({"type": "error", "message": "发生错误，请重试。"})
        return

    if not final_answer:
        final_answer = "抱歉，处理过程中发生了错误。"

    elapsed = round(time.monotonic() - start_time)

    # 完整对话在这里原子化落库：与 SSE 连接完全解耦，客户端断线也不影响。
    await persist_chat_transcript(
        session=session,
        settings=settings,
        user_id=user_id,
        client_conversation_id=client_conversation_id,
        message_payloads=message_payloads,
        final_answer=final_answer,
    )

    # Emit HITL confirmation drafts before "done" so the frontend can render
    # confirm cards under the assistant message. Data is sourced from the
    # freshly-persisted PendingAction rows (single source of truth).
    for ref in pending_action_refs.values():
        try:
            action_row = await get_pending_action_for_user(
                session,
                user_id=user_id,
                action_id=uuid.UUID(str(ref.get("action_id"))),
            )
        except (ValueError, TypeError):
            continue
        if action_row is None:
            continue
        publish({
            "type": "pending_action",
            "action": {
                "id": str(action_row.id),
                "actionType": action_row.action_type.value,
                "status": action_row.status.value,
                "title": action_row.title,
                "summary": action_row.summary,
                "payload": action_row.payload,
                "createdAt": (
                    action_row.created_at.isoformat() if action_row.created_at else None
                ),
                "expiresAt": (
                    action_row.expires_at.isoformat() if action_row.expires_at else None
                ),
            },
        })

    publish({"type": "done", "content": final_answer, "elapsed": elapsed})

    if client_conversation_id and final_answer.strip():
        first_user_text = _refine_first_message_text(message_payloads)
        if first_user_text:
            _schedule_detached(
                refine_conversation_title_deferred(
                    user_id,
                    client_conversation_id,
                    first_user_text,
                )
            )

    if has_memory_system(settings) and has_database(settings) and final_answer.strip():
        _schedule_detached(
            run_post_chat_memory_extraction_deferred(
                user_id,
                message_payloads,
                final_answer,
            )
        )
