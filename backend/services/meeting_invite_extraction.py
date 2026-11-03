"""Service for extracting meeting details from email bodies, invitations, and calendar text.

Uses a small deterministic extraction layer first (common invite formats), then falls
back to a compact LLM parser when no structured fields are found. Both paths keep the
raw text in `extra` so auditability and personalization are preserved.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from services.structured_output import invoke_structured_json

logger = logging.getLogger(__name__)

DATE_TIME_PATTERN = re.compile(
    r"(?P<date>\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)"
    r"(?:\s+(?:at\s+)?(?P<time>\d{1,2}:\d{2}\s*[APap]\.?[Mm]\.?)|"
    r"\s+from\s+(?P<time_from>\d{1,2}:\d{2}\s*[APap]\.?[Mm]\.?))",
)

MEETING_NUMBER_PATTERN = re.compile(r"((?:meeting|会议|房间)\s*(?:id|number|no|号|编号)?[:：]?\s*(\d{9,12}))", re.I)
MEETING_URL_PATTERN = re.compile(r"(https?://[^\s]+)", re.I)

# 中文邀请（腾讯会议/飞书模板）常见：2026年10月20日 14:30-15:30。
CN_DATE_PATTERN = re.compile(
    r"(?P<date>(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日(?:\s*(?:周|星期)[一二三四五六日天])?)"
)
CN_TIME_RANGE_PATTERN = re.compile(
    r"(?P<period>上午|下午|晚上|中午|凌晨)?\s*"
    r"(?P<start_h>\d{1,2})\s*[:：]\s*(?P<start_m>\d{2})"
    r"(?:\s*[-–—~～至到]\s*(?P<end_h>\d{1,2})\s*[:：]\s*(?P<end_m>\d{2}))?"
)
# ISO 风格：2026-10-20 14:30 / 2026-10-20T14:30
ISO_DATETIME_PATTERN = re.compile(
    r"(?P<date>\d{4}-\d{1,2}-\d{1,2})[ T](?P<time>\d{1,2}:\d{2})"
)

# 中文邮件里的时刻默认按东八区解释，否则 14:30 会被当成 UTC 14:30。
CN_TZ = timezone(timedelta(hours=8))


def _parse_time(value: str) -> datetime:
    stripped = value.strip().replace(".", "").lower()
    formats = ("%I:%M %p", "%H:%M")
    for fmt in formats:
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue
    return datetime.strptime(stripped, "%H:%M")


def _resolve_date_if_year_absent(text: str, matched_date: str) -> datetime:
    """Minimal resolution for invites without a year (assume current/recent year)."""
    cleaned = matched_date.strip().rstrip(",")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    year = datetime.now(timezone.utc).year
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            parsed = datetime.strptime(f"{cleaned} {year}", fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError("不支持的日期格式")


def _extract_iso_datetime(text: str) -> tuple[datetime, datetime] | None:
    match = ISO_DATETIME_PATTERN.search(text)
    if match is None:
        return None
    try:
        start = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=CN_TZ)
    except ValueError:
        return None
    return start, start + timedelta(hours=1)


def _extract_chinese_datetime(text: str) -> tuple[datetime, datetime] | None:
    date_match = CN_DATE_PATTERN.search(text)
    if date_match is None:
        return None
    parts = [int(value) for value in re.findall(r"\d+", date_match.group("date"))]
    if len(parts) == 3:
        year, month, day = parts
    elif len(parts) == 2:
        year = datetime.now(CN_TZ).year
        month, day = parts
    else:
        return None
    # 时间应该紧跟在日期后面，否则容易抓到正文里无关的「第 3 页 12:00」。
    window = text[date_match.end(): date_match.end() + 40]
    time_match = CN_TIME_RANGE_PATTERN.search(window)
    if time_match is None:
        return None
    try:
        period = time_match.group("period") or ""
        start_hour = int(time_match.group("start_h"))
        if period in ("下午", "晚上") and start_hour < 12:
            start_hour += 12
        start = datetime(
            year, month, day, start_hour, int(time_match.group("start_m")), tzinfo=CN_TZ
        )
        end_raw_hour = time_match.group("end_h")
        if end_raw_hour:
            end_hour = int(end_raw_hour)
            if period in ("下午", "晚上") and end_hour < 12:
                end_hour += 12
            end = datetime(
                year, month, day, end_hour, int(time_match.group("end_m")), tzinfo=CN_TZ
            )
            if end <= start:
                end += timedelta(days=1)
        else:
            end = start + timedelta(hours=1)
    except ValueError:
        return None
    return start, end


def _extract_start_end(text: str) -> tuple[datetime, datetime] | None:
    date_match = DATE_TIME_PATTERN.search(text)
    if date_match:
        try:
            start = _resolve_date_if_year_absent(text, date_match.group("date"))
            time_part = date_match.group("time") or date_match.group("time_from")
            if time_part:
                start = start.replace(
                    hour=_parse_time(time_part).hour,
                    minute=_parse_time(time_part).minute,
                    second=0,
                    microsecond=0,
                )
            return start, start + timedelta(hours=1)
        except ValueError as exc:
            logger.info("Could not resolve invite date from %r: %s", text[:120], exc)
    return _extract_iso_datetime(text) or _extract_chinese_datetime(text)


def extract_meeting_invite(text: str, source: str = "manual") -> dict[str, Any]:
    """Extract structured meeting invite fields from arbitrary text (idempotent helper)."""
    result: dict[str, Any] = {"source": source, "extra": {}}
    if source == "email":
        result["found_in_email"] = True

    matches = MEETING_NUMBER_PATTERN.search(text)
    if matches:
        result["meeting_number"] = matches.group(2)

    url_match = MEETING_URL_PATTERN.search(text)
    if url_match:
        result["join_url"] = url_match.group(0)

    resolved = _extract_start_end(text)
    if resolved is not None:
        result["starts_at"], result["ends_at"] = resolved

    result["extra"]["source_text_snippet"] = text.strip()[:2000]
    return result


class _InviteLlmSchema(BaseModel):
    """Structured LLM output for free-form invites."""

    title: str | None = Field(default=None, max_length=500)
    starts_at: str | None = None
    ends_at: str | None = None
    meeting_number: str | None = None
    join_url: str | None = None
    description: str | None = None
    found_in_email: bool = False


_LLM_EXTRACT_SYSTEM = """\
You extract meeting/event invitation details from user-provided text or email content.
Return JSON only: {"title","starts_at","ends_at","meeting_number","join_url","description","found_in_email"}.
Use ISO-8601 with an offset for absolute times; use the current year when only month/day is given.
Be conservative: null fields you cannot infer. Do not invent a meeting number or URL.
"""


async def extract_meeting_invite_with_llm(
    text: str,
    source: str,
    llm: ChatOpenAI,
) -> dict[str, Any]:
    """LLM fallback parser; retains deterministic fields where they were found."""
    base = extract_meeting_invite(text, source=source)
    # DeepSeek 不支持 json_schema，统一走 provider 感知的结构化调用。
    response = await invoke_structured_json(
        llm,
        _InviteLlmSchema,
        system_prompt=_LLM_EXTRACT_SYSTEM,
        human_prompt=f"Extract invite from:\n\n{text[:4000]}",
    )
    if response is None:
        logger.warning("LLM invite extraction returned nothing, using deterministic result")
        return base

    updated: dict[str, Any] = {**base}
    for field in ("title", "meeting_number", "join_url", "description"):
        value = getattr(response, field)
        if value:
            updated[field] = value
    for field, target in (("starts_at", "starts_at"), ("ends_at", "ends_at")):
        value = getattr(response, target)
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                updated[field] = parsed
            except ValueError:
                logger.warning("LLM returned invalid ISO time %r", value)
    if response.found_in_email:
        updated["found_in_email"] = True
    return updated