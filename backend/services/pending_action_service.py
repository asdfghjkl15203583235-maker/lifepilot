"""HITL write operations: draft creation and confirmed execution.

The draft phase only freezes structured payload parameters. The execute phase
reads *only* the stored payload and never re-parses conversation text, so an
email body cannot inject agent commands.
"""

import logging
import re
import uuid
from collections.abc import Sequence
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from lib.enums import PendingActionStatus, PendingActionType
from models.pending_action import PendingAction
from schemas.task import TaskUpdateRequest
from services.task_service import delete_task, get_task_for_user, update_task

logger = logging.getLogger(__name__)

# 本轮对话的 client conversation id。propose_* 工具在很深的调用栈里（Tool →
# service），逐层往下传参数会把签名全改一遍；用 ContextVar 让 create_pending_action
# 自动落 conversation_id，草案才能和对话绑定，切页/刷新后才能被找回来。
CURRENT_CONVERSATION_ID: ContextVar[str | None] = ContextVar(
    "pending_action_conversation_id", default=None,
)

DEFAULT_EXPIRES_DAYS = 7

# 同一轮对话里模型可能重复提交草稿（并发 tool_calls、被打回重写、用户重试）。
# 只在这个窗口内做合并：超时说明用户可能真的想再发一封同主题邮件。
DUPLICATE_DRAFT_WINDOW_MINUTES = 15
MAX_DUPLICATE_CANDIDATES = 20
SUBJECT_SIMILARITY_THRESHOLD = 0.85
# 一个主题完整包含另一个时视为同一意图（"青岛团建天气提醒" vs
# "青岛团建天气提醒（含小贴士）"）。门槛防住"团建"这类过短的公共片段。
MIN_CONTAINED_SUBJECT_LENGTH = 6

_WHITESPACE_RE = re.compile(r"\s+")
_SUBJECT_NOISE_RE = re.compile(r"[\s\W_]+")


class PendingActionNotFoundError(ValueError):
    """No pending action for this user and id."""


class PendingActionNotApprovableError(RuntimeError):
    """Action is not in a PENDING, unexpired state."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_recipient(value: str) -> str:
    return _WHITESPACE_RE.sub("", (value or "")).lower()


def _normalize_subject(value: str) -> str:
    return _SUBJECT_NOISE_RE.sub("", (value or "")).lower()


def is_same_email_intent(left_subject: str, right_subject: str) -> bool:
    """两个邮件主题是否指向同一件事（同主题、互相包含或高度相似）。"""
    left = _normalize_subject(left_subject)
    right = _normalize_subject(right_subject)
    if not left and not right:
        return True
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if len(shorter) >= MIN_CONTAINED_SUBJECT_LENGTH and shorter in longer:
        return True
    return SequenceMatcher(None, left, right).ratio() >= SUBJECT_SIMILARITY_THRESHOLD


async def _find_duplicate_email_draft(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    action_type: PendingActionType,
    to: str,
    subject: str,
    conversation_id: str | None = None,
) -> PendingAction | None:
    """近窗口内同一收件人 + 同主题的待确认草稿。

    模型重复调用 propose_*（并发 tool_calls、被打回重写、用户重试）会各自落库
    一条 PendingAction，前端就同时弹出两张确认卡片。命中即复用旧行，保证
    一次意图只有一份待确认操作。
    """
    cutoff = _now_utc() - timedelta(minutes=DUPLICATE_DRAFT_WINDOW_MINUTES)
    stmt = select(PendingAction).where(
        PendingAction.user_id == user_id,
        PendingAction.action_type == action_type,
        PendingAction.status == PendingActionStatus.PENDING,
        PendingAction.created_at >= cutoff,
    )
    # 只有同一段对话里的重复才算同一意图：两个标签页各自写的同主题邮件是
    # 两份独立草稿，不能互相吞掉。
    if conversation_id:
        stmt = stmt.where(PendingAction.conversation_id == conversation_id)
    result = await session.execute(
        stmt.order_by(PendingAction.created_at.desc()).limit(MAX_DUPLICATE_CANDIDATES)
    )
    recipient = _normalize_recipient(to)
    for row in result.scalars().all():
        existing = row.payload or {}
        if _normalize_recipient(str(existing.get("to") or "")) != recipient:
            continue
        if is_same_email_intent(str(existing.get("subject") or ""), subject):
            return row
    return None


async def create_pending_action(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    action_type: PendingActionType,
    title: str,
    summary: str | None,
    payload: dict[str, Any],
    conversation_id: str | None = None,
    expires_in_days: int = DEFAULT_EXPIRES_DAYS,
) -> PendingAction:
    """Persist a frozen draft. Caller owns the transaction (commit/rollback)."""
    # 调用方没显式给 id 时退回当前轮的 ContextVar：propose_* 工具在 agent 节点
    # 深处执行，拿不到 state，草案若不记 conversation_id 就彻底失去和对话的关联。
    conversation_id = conversation_id or CURRENT_CONVERSATION_ID.get()
    if action_type in (PendingActionType.EMAIL_SEND, PendingActionType.EMAIL_REPLY):
        duplicate = await _find_duplicate_email_draft(
            session,
            user_id=user_id,
            action_type=action_type,
            to=str(payload.get("to") or ""),
            subject=str(payload.get("subject") or ""),
            conversation_id=conversation_id,
        )
        # 用户在本轮把正文补全/改写了：刷新旧行，而不是再弹一张几乎一样的卡片。
        if duplicate is not None:
            logger.info(
                "Reusing pending action %s for duplicate email draft to %s",
                duplicate.id, payload.get("to"),
            )
            duplicate.title = title[:240]
            duplicate.summary = summary
            duplicate.payload = payload
            duplicate.updated_at = _now_utc()
            await session.flush()
            await session.refresh(duplicate)
            return duplicate

    row = PendingAction(
        user_id=user_id,
        conversation_id=conversation_id,
        action_type=action_type,
        status=PendingActionStatus.PENDING,
        title=title[:240],
        summary=summary,
        payload=payload,
        expires_at=_now_utc() + timedelta(days=expires_in_days),
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_pending_action_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    action_id: uuid.UUID,
) -> PendingAction | None:
    result = await session.execute(
        select(PendingAction).where(
            PendingAction.id == action_id,
            PendingAction.user_id == user_id,
        ),
    )
    return result.scalar_one_or_none()


async def list_pending_actions(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: PendingActionStatus | None = None,
    limit: int = 100,
) -> list[PendingAction]:
    stmt = (
        select(PendingAction)
        .where(PendingAction.user_id == user_id)
        .order_by(PendingAction.created_at.desc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(PendingAction.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_open_pending_actions_for_conversations(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_ids: Sequence[str],
) -> dict[str, list[PendingAction]]:
    """Restore unhandled drafts when the frontend re-fetches a transcript.

    切页/刷新后前端只会拉 `conversations.messages`，草案本身存在另一张表，
    不在这里回填卡片就永远消失了。按会话 id 分组返回，只取仍为 PENDING 且
    未过期的行——已确认/已拒绝的卡片不需要再弹。
    """
    ids = [str(cid) for cid in conversation_ids if cid]
    if not ids:
        return {}
    result = await session.execute(
        select(PendingAction)
        .where(
            PendingAction.user_id == user_id,
            PendingAction.conversation_id.in_(ids),
            PendingAction.status == PendingActionStatus.PENDING,
            or_(
                PendingAction.expires_at.is_(None),
                PendingAction.expires_at > _now_utc(),
            ),
        )
        .order_by(PendingAction.created_at.asc())
    )
    grouped: dict[str, list[PendingAction]] = {}
    for row in result.scalars().all():
        grouped.setdefault(str(row.conversation_id), []).append(row)
    return grouped


async def reject_pending_action(
    session: AsyncSession,
    user_id: uuid.UUID,
    action_id: uuid.UUID,
) -> PendingAction:
    row = await get_pending_action_for_user(session, user_id, action_id)
    if row is None:
        raise PendingActionNotFoundError("待确认操作不存在。")
    if row.status == PendingActionStatus.PENDING:
        row.status = PendingActionStatus.REJECTED
    row.updated_at = _now_utc()
    await session.flush()
    await session.refresh(row)
    return row


async def _resolve_email_credentials(
    settings: Settings,
    session: AsyncSession,
    user_id: uuid.UUID,
) -> tuple[Any, str | None]:
    """Return (mail163_config, gmail_access_token); exactly one is populated."""
    from services.agent_runner import build_mail163_config

    config = build_mail163_config(settings)
    if config is not None:
        return config, None
    from services.agent_runner import get_all_tokens

    try:
        gmail_token, _calendar, _notion = await get_all_tokens(session, settings, user_id)
    except Exception:
        gmail_token = None
    return None, gmail_token


async def _execute_email(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    action: PendingAction,
) -> dict[str, Any]:
    from services.mail163_client import reply_to_email as mail163_reply
    from services.mail163_client import send_email as mail163_send
    from services import gmail_client

    payload = action.payload
    to = payload.get("to") or ""
    subject = payload.get("subject") or ""
    body = payload.get("body") or ""
    config, gmail_token = await _resolve_email_credentials(settings, session, user_id)
    if config is not None:
        if action.action_type == PendingActionType.EMAIL_REPLY:
            return await mail163_reply(config, payload.get("thread_id") or "", payload.get("message_id") or "", to, subject, body)
        return await mail163_send(config, to, subject, body)
    if not gmail_token:
        raise RuntimeError("邮箱未连接：缺少 163 授权码或 Gmail access token。")
    if action.action_type == PendingActionType.EMAIL_REPLY:
        return await gmail_client.reply_to_email(
            gmail_token, payload.get("thread_id") or "", payload.get("message_id") or "", to, subject, body,
        )
    return await gmail_client.send_email(gmail_token, to, subject, body)


async def _execute_task_update(
    session: AsyncSession,
    user_id: uuid.UUID,
    action: PendingAction,
    settings: Settings,
) -> dict[str, Any]:
    payload = action.payload
    task_id_raw = payload.get("task_id")
    try:
        task_id = uuid.UUID(str(task_id_raw))
    except (ValueError, TypeError):
        raise PendingActionNotApprovableError("任务 id 无效。")
    task = await get_task_for_user(session, task_id=task_id, user_id=user_id)
    if task is None:
        raise PendingActionNotApprovableError("任务不存在或不属于你。")

    changes = dict(payload.get("changes") or {})
    allowed = {"title", "description", "status", "priority", "due_at", "project", "assigned_agent"}
    filtered = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if not filtered:
        raise PendingActionNotApprovableError("没有可执行的变更。")

    body = TaskUpdateRequest(**filtered)
    updated = await update_task(session, task_id=task_id, user_id=user_id, body=body)
    if updated is None:
        raise PendingActionNotApprovableError("任务不存在或不属于你。")

    if filtered.get("due_at") is not None:
        from services.reminder_service import schedule_deadline_reminder

        await schedule_deadline_reminder(
            session,
            user_id,
            task_id,
            updated.title,
            updated.due_at,
            minutes_before=settings.reminder_default_lead_minutes,
        )

    return {
        "task_id": str(task_id),
        "title": updated.title,
        "status": updated.status.value,
        "due_at": updated.due_at.isoformat() if updated.due_at else None,
    }


async def _execute_task_delete(
    session: AsyncSession,
    user_id: uuid.UUID,
    action: PendingAction,
) -> dict[str, Any]:
    payload = action.payload
    try:
        task_id = uuid.UUID(str(payload.get("task_id")))
    except (ValueError, TypeError):
        raise PendingActionNotApprovableError("任务 id 无效。")
    deleted = await delete_task(session, task_id=task_id, user_id=user_id)
    if not deleted:
        raise PendingActionNotApprovableError("任务不存在或不属于你。")
    return {"deleted": True, "task_id": str(task_id), "title": payload.get("task_title")}


async def approve_and_execute(
    session: AsyncSession,
    user_id: uuid.UUID,
    action_id: uuid.UUID,
    *,
    settings: Settings,
) -> tuple[PendingAction, dict[str, Any]]:
    """Execute a confirmed action from its frozen payload (never re-parses text)."""
    action = await get_pending_action_for_user(session, user_id, action_id)
    if action is None:
        raise PendingActionNotFoundError("待确认操作不存在。")
    if action.status == PendingActionStatus.APPROVED:
        # 幂等：双击/重试/流式重放都可能重复提交同一个确认。已经执行成功的
        # 操作必须直接返回既有结果，而不是抛"状态为 approved，无法确认"——
        # 那会让用户以为失败，实际邮件早就发出去了。
        logger.info("Pending action %s already approved; returning stored result", action.id)
        return action, action.result or {}
    if action.status != PendingActionStatus.PENDING:
        raise PendingActionNotApprovableError(f"该操作状态为 {action.status.value}，无法确认。")
    if action.expires_at is not None and action.expires_at <= _now_utc():
        action.status = PendingActionStatus.EXPIRED
        action.updated_at = _now_utc()
        raise PendingActionNotApprovableError("该确认操作已过期，请重新让助手生成。")

    try:
        if action.action_type in (PendingActionType.EMAIL_SEND, PendingActionType.EMAIL_REPLY):
            result = await _execute_email(session, settings, user_id, action)
        elif action.action_type == PendingActionType.TASK_UPDATE:
            result = await _execute_task_update(session, user_id, action, settings)
        elif action.action_type == PendingActionType.TASK_DELETE:
            result = await _execute_task_delete(session, user_id, action)
        else:
            raise PendingActionNotApprovableError(f"未知操作类型: {action.action_type}")
    except PendingActionNotApprovableError:
        action.status = PendingActionStatus.FAILED
        action.updated_at = _now_utc()
        await session.flush()
        raise
    except Exception as exc:
        logger.warning("Approved action %s failed for user %s: %s", action.id, user_id, exc)
        if getattr(exc, "retryable", False):
            # 供应商临时拒绝（限频/网络）：保持 PENDING，用户再点一次即可发送，
            # 不必重新让助手起草。
            action.status = PendingActionStatus.PENDING
            action.result = {"error": str(exc)[:2000], "retryable": True}
        else:
            action.status = PendingActionStatus.FAILED
            action.result = {"error": str(exc)[:2000]}
        action.updated_at = _now_utc()
        await session.flush()
        raise

    action.status = PendingActionStatus.APPROVED
    action.result = result
    action.executed_at = _now_utc()
    action.updated_at = _now_utc()
    await session.flush()
    return action, result