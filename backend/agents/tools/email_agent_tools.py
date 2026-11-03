"""Email agent tools — thin wrappers around Gmail REST or 163 IMAP/SMTP clients.

The legacy path closes over a Gmail access_token. The 163 path closes over a
``Mail163Config`` built from ``backend/.env``; the auth code never appears in tool
schemas, logs, or LangSmith traces.
"""

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import tool

from config import get_settings
from services.gmail_client import (
    GMAIL_API_MAX_RESULTS_PER_REQUEST,
    archive_email as _archive_email,
    create_draft as _create_draft,
    get_attachment as _get_attachment,
    get_email as _get_email,
    get_user_profile as _get_user_profile,
    list_emails as _list_emails,
    list_labels as _list_labels,
    mark_as_read as _mark_as_read,
    mark_as_unread as _mark_as_unread,
    reply_to_email as _reply_to_email,
    send_email as _send_email,
    trash_email as _trash_email,
)
from services.mail163_client import (
    Mail163Config,
    archive_email as _mail163_archive_email,
    create_draft as _mail163_create_draft,
    get_attachment as _mail163_get_attachment,
    get_email as _mail163_get_email,
    get_user_profile as _mail163_get_user_profile,
    list_emails as _mail163_list_emails,
    list_labels as _mail163_list_labels,
    mark_as_read as _mail163_mark_as_read,
    mark_as_unread as _mail163_mark_as_unread,
    reply_to_email as _mail163_reply_to_email,
    send_email as _mail163_send_email,
    trash_email as _mail163_trash_email,
)

logger = logging.getLogger(__name__)


# ── Standalone tool functions (kept for backward compat) ─────────────────


async def tool_get_email(access_token: str, message_id: str) -> dict[str, Any]:
    """Fetch a full email message by its ID."""
    return await _get_email(access_token, message_id)


async def tool_list_emails(
    access_token: str,
    *,
    query: str = "",
    max_results: int | None = None,
    page_token: str | None = None,
) -> dict[str, Any]:
    """Search / list emails. `query` uses Gmail search syntax (from:, subject:, label:, etc.)."""
    return await _list_emails(
        access_token,
        query=query,
        max_results=max_results,
        page_token=page_token,
    )


async def tool_send_email(
    access_token: str,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """Compose and send an email."""
    return await _send_email(access_token, to, subject, body)


async def tool_reply_to_email(
    access_token: str,
    thread_id: str,
    message_id: str,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """Reply to an existing email thread."""
    return await _reply_to_email(access_token, thread_id, message_id, to, subject, body)


async def tool_create_draft(
    access_token: str,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """Save an email as a draft without sending."""
    return await _create_draft(access_token, to, subject, body)


async def tool_get_attachment(
    access_token: str,
    message_id: str,
    attachment_id: str,
) -> dict[str, Any]:
    """Download an attachment from a message. Returns base64-encoded data."""
    return await _get_attachment(access_token, message_id, attachment_id)


async def tool_mark_as_read(access_token: str, message_id: str) -> dict[str, Any]:
    """Mark an email as read (remove UNREAD label)."""
    return await _mark_as_read(access_token, message_id)


async def tool_mark_as_unread(access_token: str, message_id: str) -> dict[str, Any]:
    """Mark an email as unread (add UNREAD label)."""
    return await _mark_as_unread(access_token, message_id)


async def tool_archive_email(access_token: str, message_id: str) -> dict[str, Any]:
    """Archive an email (remove from INBOX)."""
    return await _archive_email(access_token, message_id)


async def tool_trash_email(access_token: str, message_id: str) -> dict[str, Any]:
    """Move an email to trash."""
    return await _trash_email(access_token, message_id)


async def tool_list_labels(access_token: str) -> dict[str, Any]:
    """List all Gmail labels (folders) for the authenticated user."""
    return await _list_labels(access_token)


async def tool_get_user_profile(access_token: str) -> dict[str, Any]:
    """Get the Gmail profile (email address, total messages, threads count)."""
    return await _get_user_profile(access_token)


# ── Pagination + enrichment helpers ──────────────────────────────────────


async def _paginate_emails(
    access_token: str,
    query: str,
    per_page: int,
    max_total: int,
) -> dict[str, Any]:
    """Fetch all pages of email list results up to max_total."""
    next_token: str | None = None
    all_stubs: list[dict[str, Any]] = []
    last_batch: dict[str, Any] = {}
    result_size_estimate: int | None = None

    while len(all_stubs) < max_total:
        page_size = min(per_page, GMAIL_API_MAX_RESULTS_PER_REQUEST, max_total - len(all_stubs))
        if page_size < 1:
            break
        last_batch = await _list_emails(
            access_token,
            query=query,
            max_results=page_size,
            page_token=next_token,
        )
        if result_size_estimate is None:
            result_size_estimate = last_batch.get("resultSizeEstimate")
        stubs = last_batch.get("messages", [])
        all_stubs.extend(stubs)
        next_token = last_batch.get("nextPageToken")
        if not next_token or not stubs:
            break

    merged: dict[str, Any] = {**last_batch} if last_batch else {}
    merged["messages"] = all_stubs
    merged["fetched_message_count"] = len(all_stubs)
    if result_size_estimate is not None:
        merged["resultSizeEstimate"] = result_size_estimate
    merged["nextPageToken"] = next_token
    if next_token:
        merged["list_truncated_at_message_cap"] = True
    return merged


# ── HITL propose tools (draft-only, never sends) ───────────────────────────


def make_propose_email_tools(
    session: Any,
    user_id: Any,
    email_credentials: Mail163Config | None = None,
) -> list:
    """Return LangChain tools that freeze an email draft into a PendingAction.

    These tools NEVER send. They persist a structured payload; the user must
    explicitly confirm it in the frontend before /approve executes the send.
    """
    prefix = "mail163" if email_credentials is not None else "gmail"

    @tool
    async def propose_email_draft(to: str, subject: str, body: str) -> str:
        """Propose sending an email to the specified recipient.

        This does NOT send the email. Call it to prepare a confirmation draft:
        it saves the recipient/subject/body and asks the user to confirm before
        the email is actually sent."""
        from lib.enums import PendingActionType
        from services.pending_action_service import create_pending_action

        to = (to or "").strip()
        subject = (subject or "").strip()
        body = (body or "").strip()
        if not to:
            return json.dumps({"status": "error", "message": "缺少收件人 to。"}, ensure_ascii=False)
        if not subject and "@" in to:
            subject = "（无主题）"
        row = await create_pending_action(
            session,
            user_id=user_id,
            action_type=PendingActionType.EMAIL_SEND,
            title=f"发送邮件给 {to[:120]}",
            summary=f"收件人：{to}\n主题：{subject}\n\n{body}",
            payload={
                "provider": prefix,
                "to": to,
                "subject": subject if subject else "",
                "body": body,
            },
        )
        return json.dumps(
            {
                "status": "pending",
                "action_id": str(row.id),
                "action_type": "email_send",
                "to": to,
                "subject": subject,
            },
            ensure_ascii=False,
        )

    @tool
    async def propose_email_reply(
        thread_id: str,
        message_id: str,
        to: str,
        subject: str,
        body: str,
    ) -> str:
        """Propose replying to an existing email.

        This does NOT send the reply. Call it to prepare a confirmation draft:
        it saves the thread/recipient/content and asks the user to confirm before
        the reply is actually sent."""
        from lib.enums import PendingActionType
        from services.pending_action_service import create_pending_action

        to = (to or "").strip()
        subject = (subject or "").strip()
        body = (body or "").strip()
        row = await create_pending_action(
            session,
            user_id=user_id,
            action_type=PendingActionType.EMAIL_REPLY,
            title=f"回复邮件给 {to[:120]}",
            summary=f"回复：{to}\n主题：{subject}\n\n{body}",
            payload={
                "provider": prefix,
                "thread_id": (thread_id or "").strip(),
                "message_id": (message_id or "").strip(),
                "to": to,
                "subject": subject,
                "body": body,
            },
        )
        return json.dumps(
            {
                "status": "pending",
                "action_id": str(row.id),
                "action_type": "email_reply",
                "to": to,
                "subject": subject,
            },
            ensure_ascii=False,
        )

    return [propose_email_draft, propose_email_reply]


# ── 163 邮箱 IMAP/SMTP tool factory ────────────────────────────────────────


def make_mail163_email_tools(config: Mail163Config) -> list:
    """Return LangChain tools backed by 163 IMAP/SMTP with the config closed over."""
    settings = get_settings()

    @tool
    async def list_emails(query: str = "", max_results: int = 20) -> str:
        """Search and list 163 mailbox emails. Supports from:, subject:, is:unread, is:read and newer_than:Nd.
        Each returned item includes headers (from/subject/date) and a short snippet for the first few emails —
        it does NOT include full body text. To read an email's complete body, you MUST call get_email(message_id)
        with that email's id. Keep max_results small (10-25)."""
        cap = min(max(1, max_results), settings.gmail_list_max_total_messages, 50)
        result = await _mail163_list_emails(
            config,
            folder="INBOX",
            query=query,
            max_results=cap,
        )
        return json.dumps(result, default=str)

    @tool
    async def get_email(message_id: str) -> str:
        """Fetch a full 163 email message by its ID."""
        return json.dumps(await _mail163_get_email(config, message_id), default=str)

    @tool
    async def send_email(to: str, subject: str, body: str) -> str:
        """Send an email to the specified recipient."""
        return json.dumps(await _mail163_send_email(config, to, subject, body), default=str)

    @tool
    async def reply_to_email(
        thread_id: str,
        message_id: str,
        to: str,
        subject: str,
        body: str,
    ) -> str:
        """Reply to an existing 163 email thread. thread_id is accepted for Gmail compatibility."""
        return json.dumps(
            await _mail163_reply_to_email(config, thread_id, message_id, to, subject, body),
            default=str,
        )

    @tool
    async def create_draft(to: str, subject: str, body: str) -> str:
        """Save a 163 email as a draft without sending."""
        return json.dumps(await _mail163_create_draft(config, to, subject, body), default=str)

    @tool
    async def get_attachment(message_id: str, attachment_id: str) -> str:
        """Download an email attachment by message ID and attachment ID. Returns base64-encoded data."""
        return json.dumps(
            await _mail163_get_attachment(config, message_id, attachment_id), default=str
        )

    @tool
    async def mark_as_read(message_id: str) -> str:
        """Mark a 163 email as read."""
        return json.dumps(await _mail163_mark_as_read(config, message_id), default=str)

    @tool
    async def mark_as_unread(message_id: str) -> str:
        """Mark a 163 email as unread."""
        return json.dumps(await _mail163_mark_as_unread(config, message_id), default=str)

    @tool
    async def archive_email(message_id: str) -> str:
        """Archive a 163 email (remove it from the inbox)."""
        return json.dumps(await _mail163_archive_email(config, message_id), default=str)

    @tool
    async def trash_email(message_id: str) -> str:
        """Move a 163 email to trash."""
        return json.dumps(await _mail163_trash_email(config, message_id), default=str)

    @tool
    async def list_labels() -> str:
        """List all 163 mailbox folders."""
        return json.dumps(await _mail163_list_labels(config), default=str)

    @tool
    async def get_user_profile() -> str:
        """Get the 163 mailbox profile: email address, IMAP host, and message counts."""
        return json.dumps(await _mail163_get_user_profile(config), default=str)

    return [
        list_emails,
        get_email,
        create_draft,
        get_attachment,
        mark_as_read,
        mark_as_unread,
        archive_email,
        trash_email,
        list_labels,
        get_user_profile,
    ]


# ── 会议邀请 → 任务看板工具 ────────────────────────────────────────────────


def make_meeting_invite_tools(
    session: Any,
    user_id: Any,
    config: Mail163Config,
    settings: Any = None,
) -> list:
    """Return a deterministic tool that projects meeting-invite emails onto tasks.

    Same service the 10-minute scheduler runs; the tool exists so a chat request
    like "看看邮箱里有没有会议邀请，加到任务里" can trigger it on demand. The
    session is NOT committed here — the chat turn commits after persisting the
    transcript, keeping the whole turn in one transaction.
    """

    @tool
    async def scan_meeting_invites(lookback_days: int = 3) -> str:
        """扫描最近的收件箱邮件，识别会议邀请并把它们加到任务看板。

        用户要求“看看邮箱里有没有会议邀请”“把会议邀请整理成任务”“从邮件里拉会议日程”时调用。
        按邮件 ID 幂等：重复扫描不会重复建任务。返回扫描到的会议标题与时间。"""
        from services.meeting_invite_scanner import scan_mail163_inbox_for_meeting_invites

        days = max(1, min(int(lookback_days or 3), 30))
        result = await scan_mail163_inbox_for_meeting_invites(
            session,
            user_id=user_id,
            config=config,
            settings=settings,
            lookback_days=days,
        )
        return json.dumps({"status": "ok", **result.as_dict()}, ensure_ascii=False, default=str)

    return [scan_meeting_invites]


# ── LangChain tool factory (legacy Gmail) ─────────────────────────────────


def make_email_tools(access_token: str) -> list:
    """Return LangChain tools for Gmail with the access token closed over."""
    settings = get_settings()

    @tool
    async def list_emails(query: str = "", max_results: int = 20) -> str:
        """Search and list emails. query uses Gmail search syntax (from:, subject:, is:unread, newer_than:7d, label:, etc.).
        Returns headers and short snippets only — does NOT include full body text. To read an email's complete body,
        you MUST call get_email(message_id) with that email's id. Keep max_results small (10-25)."""
        total_cap = min(max(1, max_results), settings.gmail_list_max_total_messages)
        per_page = min(total_cap, GMAIL_API_MAX_RESULTS_PER_REQUEST)
        result = await _paginate_emails(access_token, query, per_page, total_cap)
        return json.dumps(result, default=str)

    @tool
    async def get_email(message_id: str) -> str:
        """Fetch a full email message by its ID."""
        return json.dumps(await _get_email(access_token, message_id), default=str)

    @tool
    async def send_email(to: str, subject: str, body: str) -> str:
        """Send an email to the specified recipient."""
        return json.dumps(await _send_email(access_token, to, subject, body), default=str)

    @tool
    async def reply_to_email(
        thread_id: str,
        message_id: str,
        to: str,
        subject: str,
        body: str,
    ) -> str:
        """Reply to an existing email thread."""
        return json.dumps(
            await _reply_to_email(access_token, thread_id, message_id, to, subject, body),
            default=str,
        )

    @tool
    async def create_draft(to: str, subject: str, body: str) -> str:
        """Save an email as a draft without sending."""
        return json.dumps(await _create_draft(access_token, to, subject, body), default=str)

    @tool
    async def get_attachment(message_id: str, attachment_id: str) -> str:
        """Download an email attachment by message ID and attachment ID. Returns base64-encoded data."""
        return json.dumps(
            await _get_attachment(access_token, message_id, attachment_id), default=str
        )

    @tool
    async def mark_as_read(message_id: str) -> str:
        """Mark an email as read."""
        return json.dumps(await _mark_as_read(access_token, message_id), default=str)

    @tool
    async def mark_as_unread(message_id: str) -> str:
        """Mark an email as unread."""
        return json.dumps(await _mark_as_unread(access_token, message_id), default=str)

    @tool
    async def archive_email(message_id: str) -> str:
        """Archive an email (remove it from the inbox)."""
        return json.dumps(await _archive_email(access_token, message_id), default=str)

    @tool
    async def trash_email(message_id: str) -> str:
        """Move an email to trash."""
        return json.dumps(await _trash_email(access_token, message_id), default=str)

    @tool
    async def list_labels() -> str:
        """List all Gmail labels (folders) for the user."""
        return json.dumps(await _list_labels(access_token), default=str)

    @tool
    async def get_user_profile() -> str:
        """Get the user's Gmail profile: email address, message count, and thread count."""
        return json.dumps(await _get_user_profile(access_token), default=str)

    return [
        list_emails,
        get_email,
        create_draft,
        get_attachment,
        mark_as_read,
        mark_as_unread,
        archive_email,
        trash_email,
        list_labels,
        get_user_profile,
    ]
