"""163 邮箱 IMAP/SMTP 客户端 — 授权码登录、读信、发信。

所有阻塞 IMAP/SMTP 调用都通过 `asyncio.to_thread` 执行，避免卡住 FastAPI
事件循环。授权码只存在于内存中的 `Mail163Config`，不会写入日志或工具 schema。
"""

import asyncio
import base64
import email
import imaplib
import logging
import re
import smtplib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid, parsedate_to_datetime
from typing import Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)

IMAP_HOST_DEFAULT = "imap.163.com"
SMTP_HOST_DEFAULT = "smtp.163.com"
IMAP_PORT_DEFAULT = 993
SMTP_PORT_DEFAULT = 465
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10

MAX_LIST_RESULTS = 50
MAX_LIST_SNIPPETS = 10
_LIST_SNIPPET_MAX_CHARS = 150
MAX_BODY_CHARS = 5000
_BODY_TRUNCATION_MARKER = "\n[内容过长，已截断...]"

# 163 会主动重置来源 IP 的新连接（表现为 WinError 10054 / Connection reset）。
# 一次瞬断不应该让整轮后台扫描失败，所以连接阶段重试一次。
CONNECT_ATTEMPTS = 2
CONNECT_RETRY_DELAY_SECONDS = 1.0

# 163 SMTP 在短时间连续发信时会用 535 拒绝登录（看起来像授权码错误），
# 隔两秒重试一次通常就过了。
SMTP_LOGIN_ATTEMPTS = 2
SMTP_LOGIN_RETRY_DELAY_SECONDS = 2.0

# 163 常用归档/回收站文件夹名，按顺序探测。
_ARCHIVE_FOLDERS = ("[Gmail]/All Mail", "INBOX.Archive", "Archive")
_TRASH_FOLDERS = ("[Gmail]/Trash", "INBOX.Trash", "Trash", "Deleted Messages")
_DRAFT_FOLDERS = ("[Gmail]/Drafts", "INBOX.Drafts", "Drafts")

# 用邮箱主题/地址中可能出现的分隔符和编码，尽量把中文 header 转成 readable string。
_HEADER_DECODE_RE = re.compile(r"=\?[^?]+\?[QqBb]\?[^?]+\?=")


@dataclass(frozen=True)
class Mail163Config:
    email: str
    auth_code: str
    smtp_host: str = SMTP_HOST_DEFAULT
    smtp_port: int = SMTP_PORT_DEFAULT
    imap_host: str = IMAP_HOST_DEFAULT
    imap_port: int = IMAP_PORT_DEFAULT
    timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS


class Mail163Error(Exception):
    """Raised when 163 mail IMAP/SMTP returns an error."""

    def __init__(self, message: str, code: int = 502, *, retryable: bool = False) -> None:
        self.code = code
        self.message = message
        # 网易在频率过高时也会返回 535（看起来像授权码错误）。这类错误值得重试，
        # 调用方据此保留待确认操作，让用户再点一次发送而不是重新起草。
        self.retryable = retryable
        super().__init__(message)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _clean_subject(value: str | None) -> str:
    return _decode_header(value).replace("\r", " ").replace("\n", " ").strip()


def _message_id_to_bytes(message_id: str) -> bytes:
    return message_id.encode("ascii", "ignore")


def _select_folder(mail: imaplib.IMAP4_SSL, folder: str) -> None:
    typ, data = mail.select(f'"{folder}"')
    response_text = " ".join(str(item) for item in (data or []))
    if typ != "OK":
        if "Unsafe Login" in response_text or "UnsafeLogin" in response_text:
            raise Mail163Error(_MAIL163_UNSAFE_LOGIN_TROUBLESHOOTING, 503)
        if "LOGIN fail" in response_text or "authentication" in response_text.lower():
            raise Mail163Error("163 邮箱登录验证失败，请检查授权码。", 401)
        raise Mail163Error(f"无法打开邮箱文件夹 {folder}：{response_text}", 502)


def _uid_store(mail: imaplib.IMAP4_SSL, uid: str, flags: str) -> None:
    typ, _data = mail.uid("STORE", _message_id_to_bytes(uid), flags, r"(\Seen)")
    if typ != "OK":
        raise Mail163Error("更新邮件标记失败", 502)


async def verify_login(config: Mail163Config) -> str:
    """通过 IMAP 登录验证授权码，返回邮箱地址。"""
    def _verify() -> str:
        mail = _connect_imap(config)
        try:
            typ, data = mail.select("INBOX")
            response_text = " ".join(str(item) for item in (data or []))
            if typ != "OK":
                if "Unsafe Login" in response_text or "UnsafeLogin" in response_text:
                    raise Mail163Error(_MAIL163_UNSAFE_LOGIN_TROUBLESHOOTING, 503)
                if "LOGIN fail" in response_text or "authentication" in response_text.lower():
                    raise Mail163Error("163 邮箱登录验证失败，请检查授权码。", 401)
                raise Mail163Error(
                    "163 邮箱通过了账号验证，但无法打开收件箱。"
                    + _MAIL163_SELECT_NO_RESPONSE_SUFFIX,
                    503,
                )
        finally:
            _safe_logout(mail)
        return config.email

    return await asyncio.to_thread(_verify)


def _safe_logout(mail: imaplib.IMAP4_SSL | None) -> None:
    if mail is None:
        return
    try:
        mail.logout()
    except Exception:
        pass


_MAIL163_LOGIN_TROUBLESHOOTING = (
    "163 邮箱 IMAP 登录失败。请依次确认："
    "1) backend/.env 中的 MAIL163_EMAIL 是正确的 163 邮箱地址；"
    "2) 已在 163 邮箱网页版设置中开启 IMAP/SMTP 服务；"
    "3) MAIL163_EMAIL_AUTH_CODE 填的是独立授权码，不是 163 密码；"
    "4) 若刚才反复登录触发频率限制，请等待几分钟后再试。"
    "修改后保存 backend/.env 并重启后端。"
)

_MAIL163_UNSAFE_LOGIN_TROUBLESHOOTING = (
    "网易邮箱已阻止客户端访问（Unsafe Login）。"
    "请登录网易邮箱网页版，进入设置确认 IMAP/SMTP 服务已开启，"
    "并生成一个新的独立客户端授权码，替换 backend/.env 中的 "
    "MAIL163_EMAIL_AUTH_CODE 后重启后端。"
    "如果连续尝试仍被拦截，请过几分钟再试，或联系网易邮箱客服 kefu@188.com。"
)

_MAIL163_SELECT_NO_RESPONSE_SUFFIX = (
    " 请确认 backend/.env 中的 MAIL163_EMAIL_AUTH_CODE 是网易邮箱的独立客户端授权码，"
    "不是 QQ 邮箱或其他邮箱的授权码，也不是 163 邮箱登录密码；"
    "修改后重启后端。"
)


def _open_imap_socket(config: Mail163Config) -> imaplib.IMAP4_SSL:
    """Open the TLS socket, tolerating one transient reset from the server."""
    last_error: Exception | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return imaplib.IMAP4_SSL(
                config.imap_host, config.imap_port, timeout=config.timeout_seconds
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "163 IMAP connect attempt %d/%d failed for %s: %s",
                attempt,
                CONNECT_ATTEMPTS,
                config.imap_host,
                exc,
            )
            if attempt < CONNECT_ATTEMPTS:
                time.sleep(CONNECT_RETRY_DELAY_SECONDS)
    raise Mail163Error(f"无法连接 163 IMAP：{last_error}", 502) from last_error


def _connect_imap(config: Mail163Config) -> imaplib.IMAP4_SSL:
    mail = _open_imap_socket(config)

    try:
        typ, _data = mail.login(config.email, config.auth_code)
        if typ != "OK":
            _safe_logout(mail)
            raise Mail163Error(_MAIL163_LOGIN_TROUBLESHOOTING, 401)
    except Mail163Error:
        _safe_logout(mail)
        raise
    except Exception as exc:
        _safe_logout(mail)
        logger.warning("163 IMAP login failed: %s", exc)
        raise Mail163Error(_MAIL163_LOGIN_TROUBLESHOOTING, 401) from exc

    try:
        # 163 会检查客户端标识；不使用 ID 时可能直接拒绝后续文件夹操作。
        imaplib.Commands.setdefault("ID", ("AUTH",))
        quoted_args = ' '.join(f'"{key}" "{value}"' for key, value in (
            ("name", "LifePilot"),
            ("version", "1.0"),
        ))
        mail._simple_command("ID", f"({quoted_args})")
    except Exception as exc:
        _safe_logout(mail)
        logger.warning("163 IMAP ID command failed: %s", exc)
        raise Mail163Error(
            "163 邮箱连接成功但客户端身份声明失败，请检查网络并稍后重试。", 502
        ) from exc

    return mail


def _connect_smtp(config: Mail163Config) -> smtplib.SMTP:
    last_error: Exception | None = None
    for attempt in range(1, SMTP_LOGIN_ATTEMPTS + 1):
        try:
            smtp = smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, timeout=config.timeout_seconds
            )
        except Exception as exc:
            raise Mail163Error(f"无法连接 163 SMTP：{exc}", 502, retryable=True) from exc

        try:
            smtp.login(config.email, config.auth_code)
            return smtp
        except Exception as exc:
            last_error = exc
            try:
                smtp.close()
            except Exception:
                pass
            logger.warning(
                "163 SMTP login attempt %d/%d failed: %s", attempt, SMTP_LOGIN_ATTEMPTS, exc
            )
            if attempt < SMTP_LOGIN_ATTEMPTS:
                time.sleep(SMTP_LOGIN_RETRY_DELAY_SECONDS)

    raise Mail163Error(
        "163 邮箱暂时拒绝了登录（发送过于频繁时也会这样），请稍后重试；"
        "如果一直失败，请在邮箱设置里重新生成授权码。",
        502,
        retryable=True,
    ) from last_error


def _parse_search_query(query: str) -> list[str]:
    """把常见 Gmail 搜索语法翻译成 IMAP SEARCH 条件。

    只翻译可控子集；无法识别的条件忽略（回退到 ALL）。支持：
      from:, subject:, is:unread, is:read, newer_than:Nd
    """
    if not query or not query.strip():
        return []

    raw = query.strip()
    terms: list[str] = []

    def add_simple(prefix: str, value: str, imap_key: str) -> None:
        terms.append(f"{imap_key} {value}")

    for part in re.split(r"\s+", raw):
        lowered = part.lower()
        if lowered == "is:unread":
            terms.append("UNSEEN")
        elif lowered == "is:read":
            terms.append("SEEN")
        elif lowered.startswith("from:") and len(part) > 5:
            add_simple("from", part[5:].strip('"'), "FROM")
        elif lowered.startswith("subject:") and len(part) > 8:
            add_simple("subject", part[8:].strip('"'), "SUBJECT")
        elif lowered.startswith("newer_than:"):
            match = re.fullmatch(r"newer_than:(\d+)[dD]", lowered)
            if match:
                days = int(match.group(1))
                since = datetime.now(timezone.utc) - timedelta(days=days)
                terms.append(f'SINCE "{since.strftime("%d-%b-%Y")}"')
        # 其他 Gmail 高级语法不翻译，避免无效命令。

    return terms


def _search_uid_list(mail: imaplib.IMAP4_SSL, query: str, max_results: int) -> list[str]:
    terms = _parse_search_query(query)
    search_args = [*terms]
    # IMAP SEARCH 没有 LIMIT，这里请求后自己截断。
    typ, data = mail.uid("SEARCH", None, *search_args) if search_args else mail.uid("SEARCH", "ALL")
    if typ != "OK":
        raise Mail163Error("搜索邮件失败", 502)
    raw_uids = (data or [None])[0]
    if not raw_uids:
        return []
    uids = raw_uids.split()[:max_results]
    return [u.decode("ascii", "ignore") for u in uids]


def _fetch_headers(mail: imaplib.IMAP4_SSL, uid: str) -> dict[str, Any]:
    message = _fetch_message(mail, uid, include_body=False)
    return _message_to_summary(message, uid)


def _fetch_message(mail: imaplib.IMAP4_SSL, uid: str, include_body: bool = True) -> email.message.Message:
    typ, data = mail.uid("FETCH", _message_id_to_bytes(uid), "(RFC822)" if include_body else "(BODY.PEEK[HEADER])")
    if typ != "OK" or not data or data[0] is None:
        raise Mail163Error(f"邮件不存在：{uid}", 404)

    raw = data[0][1] if isinstance(data[0], tuple) else data[0]
    return email.message_from_bytes(raw)


def _message_to_summary(
    message: email.message.Message,
    uid: str,
    *,
    include_body: bool = False,
    snippet_only: bool = False,
) -> dict[str, Any]:
    subject = _clean_subject(str(message.get("Subject", "")))
    from_header = _decode_header(str(message.get("From", "")))
    to_header = _decode_header(str(message.get("To", "")))
    date_raw = message.get("Date")
    try:
        date_iso = parsedate_to_datetime(date_raw).astimezone(timezone.utc).isoformat() if date_raw else None
    except Exception:
        date_iso = None

    has_attachments = any(
        part.get_filename() for part in message.walk() if part.get_content_maintype() != "multipart"
    ) if message.is_multipart() else bool(message.get_filename())

    summary: dict[str, Any] = {
        "id": uid,
        "threadId": uid,
        # IMAP UID 只在当前邮箱/文件夹内稳定，换服务器或重新同步就会变。
        # Message-ID 才是全局幂等键（后台监听用它去重）。
        "messageId": _decode_header(str(message.get("Message-ID", ""))).strip(),
        "subject": subject,
        "from": from_header,
        "to": to_header,
        "date": date_iso,
        "has_attachments": has_attachments,
        "snippet": "",
        "internalDate": date_iso,
    }

    if snippet_only:
        summary["snippet"] = _summary_snippet(message, max_chars=_LIST_SNIPPET_MAX_CHARS)
    elif include_body:
        body_text, body_html = _body_text(message)
        summary["body"] = body_text
        summary["bodyHtml"] = body_html
        summary["snippet"] = _summary_snippet(message)

    return summary


def _html_to_plain_text(html: str) -> str:
    """Extract readable text from HTML, keeping line structure and dropping tags."""
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            stripped = data.strip()
            if stripped:
                self.parts.append(stripped)

        def handle_endtag(self, tag: str) -> None:
            # 块级标签后加换行，避免整段挤在一起。
            if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"}:
                self.parts.append("\n")

    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception:
        return html
    return "\n".join(part for part in extractor.parts if part != "\n").strip()


def _body_text(message: email.message.Message) -> tuple[str, str]:
    """返回 (plain_text, html_text)；无纯文本时从 HTML 提取可读文本。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            plain_parts.append(text)
        elif ctype == "text/html":
            html_parts.append(text)
    plain = "\n".join(plain_parts)
    html = "\n".join(html_parts)
    if plain:
        return plain, html
    if html:
        return _html_to_plain_text(html), html
    return "", ""


def _summary_snippet(message: email.message.Message, max_chars: int = 500) -> str:
    plain, _html = _body_text(message)
    return plain.strip()[:max_chars]


def _truncate_body(text: str, max_chars: int = MAX_BODY_CHARS) -> str:
    """Truncate a long email body, marking the cut so readers know it is partial."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + _BODY_TRUNCATION_MARKER


def _extract_attachments(message: email.message.Message) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        filename_decoded = _decode_header(filename)
        payload = part.get_payload(decode=True) or b""
        out.append({
            "attachmentId": filename_decoded,
            "filename": filename_decoded,
            "mimeType": part.get_content_type(),
            "size": len(payload),
            "data_base64": base64.b64encode(payload).decode("ascii"),
            "data": base64.b64encode(payload).decode("ascii"),
        })
    return out


def _list_messages(
    mail: imaplib.IMAP4_SSL,
    *,
    folder: str,
    query: str,
    max_results: int,
) -> dict[str, Any]:
    """List headers (plus snippets for the first few) on an already-open connection."""
    _select_folder(mail, folder)
    cap = max(1, min(int(max_results), MAX_LIST_RESULTS))
    uids = _search_uid_list(mail, query or "", cap)
    messages: list[dict[str, Any]] = [{} for _ in uids]
    snippet_count = 0
    for idx, uid in enumerate(uids):
        if snippet_count < MAX_LIST_SNIPPETS:
            messages[idx] = _message_to_summary(
                _fetch_message(mail, uid), uid, snippet_only=True
            )
            snippet_count += 1
        else:
            messages[idx] = _fetch_headers(mail, uid)
    return {
        "messages": messages,
        "resultSizeEstimate": len(uids),
        "fetched_message_count": len(messages),
        "nextPageToken": None,
        "folder": folder,
        "list_is_summary_only": True,
        "snippet_count": snippet_count,
        "snippet_omitted_count": max(0, len(uids) - snippet_count),
        "note": "列表仅含标头与短摘要；查看完整正文请调用 get_email(message_id)。",
    }


def _fetch_email_dict(mail: imaplib.IMAP4_SSL, message_id: str) -> dict[str, Any]:
    """Read one full message on an already-open connection."""
    _select_folder(mail, "INBOX")
    message = _fetch_message(mail, message_id)
    summary = _message_to_summary(message, message_id)
    body_text, body_html = _body_text(message)
    summary["body"] = _truncate_body(body_text)
    summary["bodyHtml"] = _truncate_body(body_html)
    summary["snippet"] = _summary_snippet(message)
    summary["attachments"] = _extract_attachments(message)
    return summary


def _search_uid_by_message_id(mail: imaplib.IMAP4_SSL, message_id: str) -> str | None:
    """Look a message up by its `Message-ID` header (used when only that is stored)."""
    typ, data = mail.uid("SEARCH", None, "HEADER", "Message-ID", f'"{message_id}"')
    if typ != "OK" or not data or not data[0]:
        return None
    uids = data[0].split()
    return uids[-1].decode() if uids else None


def _fetch_email_by_message_id(
    mail: imaplib.IMAP4_SSL, message_id: str
) -> dict[str, Any] | None:
    _select_folder(mail, "INBOX")
    uid = _search_uid_by_message_id(mail, message_id)
    if uid is None:
        return None
    return _fetch_email_dict(mail, uid)


class Mail163Session:
    """把一轮后台扫描的 IMAP 操作收敛到同一条连接上。

    每 5 分钟一轮如果为每封邮件都重新登录，短时间内会开出几十条连接，
    163 会直接重置新连接（WinError 10054）。整轮复用一条连接既快也避开风控。
    连接中途被对端断开时自动重连重试一次。
    """

    def __init__(self, config: Mail163Config) -> None:
        self._config = config
        self._mail: imaplib.IMAP4_SSL | None = None

    def _connection(self) -> imaplib.IMAP4_SSL:
        if self._mail is None:
            self._mail = _connect_imap(self._config)
        return self._mail

    def _drop(self) -> None:
        _safe_logout(self._mail)
        self._mail = None

    def _run(self, operation: Any) -> Any:
        try:
            return operation(self._connection())
        except (imaplib.IMAP4.abort, OSError) as exc:
            logger.warning("163 IMAP connection dropped mid-session, reconnecting: %s", exc)
            self._drop()
            return operation(self._connection())

    def list_messages(
        self,
        *,
        folder: str = "INBOX",
        query: str = "",
        max_results: int = 20,
    ) -> dict[str, Any]:
        return self._run(
            lambda mail: _list_messages(
                mail, folder=folder, query=query, max_results=max_results
            )
        )

    def fetch_email(self, message_id: str) -> dict[str, Any]:
        return self._run(lambda mail: _fetch_email_dict(mail, message_id))

    def fetch_email_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        return self._run(lambda mail: _fetch_email_by_message_id(mail, message_id))

    def close(self) -> None:
        self._drop()


async def list_emails(
    config: Mail163Config,
    *,
    folder: str = "INBOX",
    query: str = "",
    max_results: int = 20,
    page_token: str | None = None,
    session: "Mail163Session | None" = None,
) -> dict[str, Any]:
    """列出邮箱，缺省 INBOX；返回的结构尽量兼容现有邮件工具消费者。

    按需召回：只返回标头 + 前 MAX_LIST_SNIPPETS 封邮件的短摘要 snippet，
    不再返回完整 body。要看完整正文必须调用 get_email(message_id)。

    传入 `session` 时复用后台轮询的同一条 IMAP 连接。
    """
    if session is not None:
        return await asyncio.to_thread(
            session.list_messages, folder=folder, query=query, max_results=max_results
        )

    def _list() -> dict[str, Any]:
        mail = _connect_imap(config)
        try:
            return _list_messages(mail, folder=folder, query=query, max_results=max_results)
        finally:
            _safe_logout(mail)

    return await asyncio.to_thread(_list)


async def get_email(
    config: Mail163Config,
    message_id: str,
    *,
    session: "Mail163Session | None" = None,
) -> dict[str, Any]:
    if session is not None:
        return await asyncio.to_thread(session.fetch_email, message_id)

    def _get() -> dict[str, Any]:
        mail = _connect_imap(config)
        try:
            return _fetch_email_dict(mail, message_id)
        finally:
            _safe_logout(mail)

    return await asyncio.to_thread(_get)


async def get_email_by_message_id(
    config: Mail163Config,
    message_id: str,
    *,
    session: "Mail163Session | None" = None,
) -> dict[str, Any] | None:
    """Fetch a message by its `Message-ID` header; None when it is no longer in INBOX."""
    if session is not None:
        return await asyncio.to_thread(session.fetch_email_by_message_id, message_id)

    def _get() -> dict[str, Any] | None:
        mail = _connect_imap(config)
        try:
            return _fetch_email_by_message_id(mail, message_id)
        finally:
            _safe_logout(mail)

    return await asyncio.to_thread(_get)


async def send_email(config: Mail163Config, to: str, subject: str, body: str) -> dict[str, Any]:
    def _send() -> dict[str, Any]:
        smtp = _connect_smtp(config)
        try:
            msg = _build_email(config.email, to, subject, body, reply_to=None)
            smtp.sendmail(config.email, [to], msg.as_string())
        finally:
            smtp.close()
        return {"to": to, "subject": subject, "status": "sent"}

    return await asyncio.to_thread(_send)


async def reply_to_email(
    config: Mail163Config,
    thread_id: str,
    message_id: str,
    to: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    def _reply() -> dict[str, Any]:
        smtp = _connect_smtp(config)
        try:
            original = get_email_owner_and_subject(config, message_id)
            msg = _build_email(
                config.email,
                to,
                subject or original["subject"],
                body,
                reply_to=original,
            )
            smtp.sendmail(config.email, [to], msg.as_string())
        finally:
            smtp.close()
        return {"to": to, "subject": subject or original["subject"], "status": "sent", "message_id": message_id}

    return await asyncio.to_thread(_reply)


def get_email_owner_and_subject(config: Mail163Config, message_id: str) -> dict[str, Any]:
    mail = _connect_imap(config)
    try:
        _select_folder(mail, "INBOX")
        message = _fetch_message(mail, message_id)
        return {
            "message_id": message.get("Message-ID"),
            "subject": _clean_subject(str(message.get("Subject", ""))),
            "from": _decode_header(str(message.get("From", ""))),
        }
    finally:
        mail.logout()


def _build_email(
    from_email: str,
    to: str,
    subject: str,
    body: str,
    *,
    reply_to: dict[str, Any] | None = None,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr(("", from_email))
    msg["To"] = to
    msg["Subject"] = subject if not subject.lower().startswith("re:") else subject
    msg["Message-ID"] = make_msgid(domain="lifepilot.local")
    if reply_to and reply_to.get("message_id"):
        msg["In-Reply-To"] = reply_to["message_id"]
        msg["References"] = reply_to.get("message_id", "")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(f"<pre>{body}</pre>", "html", "utf-8"))
    return msg


async def create_draft(config: Mail163Config, to: str, subject: str, body: str) -> dict[str, Any]:
    def _create() -> dict[str, Any]:
        mail = _connect_imap(config)
        try:
            for folder in _DRAFT_FOLDERS:
                typ, _data = mail.select(f'"{folder}"')
                if typ == "OK":
                    break
            else:
                raise Mail163Error("未找到草稿文件夹", 502)
            msg = _build_email(config.email, to, subject, body)
            typ, _data = mail.append(f'"{folder}"', r"(\Draft)", None, msg.as_bytes())
            if typ != "OK":
                raise Mail163Error("保存草稿失败", 502)
        finally:
            mail.logout()
        return {"to": to, "subject": subject, "status": "draft_saved"}

    return await asyncio.to_thread(_create)


async def get_attachment(config: Mail163Config, message_id: str, attachment_id: str) -> dict[str, Any]:
    def _get_attachment() -> dict[str, Any]:
        message = get_email_owner_and_subject(config, message_id) or {}
        # 重新读取完整邮件以确认附件。
        mail = _connect_imap(config)
        try:
            _select_folder(mail, "INBOX")
            full = _fetch_message(mail, message_id)
        finally:
            mail.logout()
        for att in _extract_attachments(full):
            if att["filename"] == attachment_id:
                return att
        raise Mail163Error(f"未找到附件：{attachment_id}", 404)

    return await asyncio.to_thread(_get_attachment)


async def mark_as_read(config: Mail163Config, message_id: str) -> dict[str, Any]:
    def _mark() -> dict[str, Any]:
        mail = _connect_imap(config)
        try:
            _select_folder(mail, "INBOX")
            _uid_store(mail, message_id, "+FLAGS")
        finally:
            mail.logout()
        return {"id": message_id, "status": "read"}

    return await asyncio.to_thread(_mark)


async def mark_as_unread(config: Mail163Config, message_id: str) -> dict[str, Any]:
    def _mark() -> dict[str, Any]:
        mail = _connect_imap(config)
        try:
            _select_folder(mail, "INBOX")
            _uid_store(mail, message_id, "-FLAGS")
        finally:
            mail.logout()
        return {"id": message_id, "status": "unread"}

    return await asyncio.to_thread(_mark)


def _move_to_folder(config: Mail163Config, message_id: str, candidates: tuple[str, ...], default_flag: str) -> str:
    mail = _connect_imap(config)
    try:
        _select_folder(mail, "INBOX")
        target = None
        for folder in candidates:
            typ, _data = mail.list()
            if typ == "OK":
                for line in _data:
                    if not line:
                        continue
                    line_text = line.decode("utf-8", "ignore")
                    if f'"{folder}"' in line_text or folder in line_text:
                        target = folder
                        break
            if target:
                break

        if target:
            typ, _data = mail.uid("COPY", _message_id_to_bytes(message_id), f'"{target}"')
            if typ != "OK":
                raise Mail163Error(f"移动到 {target} 失败", 502)
            mail.uid("STORE", _message_id_to_bytes(message_id), "+FLAGS", r"(\Deleted)")
            mail.expunge()
            return f"moved_to_{target}"
        mail.uid("STORE", _message_id_to_bytes(message_id), "+FLAGS", default_flag)
        return f"marked_{default_flag}"
    finally:
        mail.logout()


async def archive_email(config: Mail163Config, message_id: str) -> dict[str, Any]:
    def _archive() -> str:
        return _move_to_folder(config, message_id, _ARCHIVE_FOLDERS, r"(\Deleted)")

    result = await asyncio.to_thread(_archive)
    return {"id": message_id, "status": result}


async def trash_email(config: Mail163Config, message_id: str) -> dict[str, Any]:
    def _trash() -> str:
        return _move_to_folder(config, message_id, _TRASH_FOLDERS, r"(\Deleted)")

    result = await asyncio.to_thread(_trash)
    return {"id": message_id, "status": result}


async def list_labels(config: Mail163Config) -> dict[str, Any]:
    def _labels() -> dict[str, Any]:
        mail = _connect_imap(config)
        try:
            typ, data = mail.list()
            if typ != "OK":
                raise Mail163Error("读取邮箱文件夹失败", 502)
            folders: list[str] = []
            for line in data:
                if not line:
                    continue
                text = line.decode("utf-8", "ignore")
                parts = re.findall(r'"[^"]*"', text)
                if parts:
                    # 文件夹名通常是最后一个引号字符串，且不以 \ 开头
                    candidates = [unquote(p.strip('"')) for p in parts if not p.strip('"').startswith("\\")]
                    if candidates:
                        folders.append(candidates[-1])
            return {"labels": folders}
        finally:
            mail.logout()

    return await asyncio.to_thread(_labels)


async def get_user_profile(config: Mail163Config) -> dict[str, Any]:
    email_address = await verify_login(config)
    return {
        "emailAddress": email_address,
        "messagesTotal": 0,
        "threadsTotal": 0,
        "provider": "mail163",
        "imapHost": config.imap_host,
    }