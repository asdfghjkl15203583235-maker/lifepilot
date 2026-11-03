"""用 LLM 从自由格式邮件里识别「需要用户到场的事件」和「需要用户动手的事」。

会议邀请有固定模板（腾讯会议/飞书），确定性正则就能解析；但「周六一起吃个饭？」
这类朋友邀约没有任何关键词，只能靠语义理解。本模块只负责一件事：给一段邮件
正文，返回结构化信号（是否事件 / 类型 / 绝对开始时间 / 是否需要用户行动）。

保守原则：解析不出**绝对开始时间**就不算事件——后台任务要建的是带截止时间的
待办，猜一个时间比不建更糟。

`action_required` 目前只写日志（shadow），不建任务：先用真实邮件跑一段时间，
确认误判率可接受再打开自动建卡。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from services.structured_output import invoke_structured_json

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
MAX_TEXT_CHARS = 3500

_INBOX_EVENT_SYSTEM_PROMPT = """\
你是一位邮件助理。做两件独立的判断：

一、这封邮件里是否包含需要用户**亲自到场或参加**的事件，并抽取结构化信息。

算事件的情况（必须能从正文推断出具体开始时间）：
- 会议 / 面试 / 评审 / 面试邀请（线上或线下）
- 朋友、同事、家人的约饭、聚会、生日、婚礼、看演出等邀约
- 预约（体检、牙医、看房、取件等有明确时间的安排）

不算事件（is_event=false）：
- 营销、订阅、账单、通知类邮件，快递发货提醒
- 只有「有空聊聊」却没有具体时间的客套话
- 需要用户自己挑时间的模糊提议（没有落到某一天某一时刻）

二、这封邮件是否要求用户**采取行动**（action_required）。

算需要行动：对方明确要求你回复、提交材料、付款、确认行程、在某个期限前完成某事。
不算需要行动：纯告知（订单已发货、账单已出、系统维护通知）、营销推广、自动回执。
一封邮件可以不是事件但需要行动（例如「请周五前把报表发我」），也可以既是事件
又需要确认出席。

规则：
- `starts_at` 必须输出带时区的 ISO-8601（用户在中国，默认 +08:00）。只给了日期
  没给钟点时，会议默认 09:00，社交邀约默认 18:00，并在 reason 里注明是默认值。
- 年份缺失时使用当年；日期已过去则顺延到下一年。
- `title` 用一句话说清是什么事（如「和张伟吃饭」「产品评审会」），不要整段主题照搬。
- 绝不编造正文里没有出现的时间、地点或人名。

只回复 JSON：
{"is_event": true/false,
 "kind": "meeting" | "social" | "appointment" | "none",
 "title": "...",
 "starts_at": "2026-11-07T18:00:00+08:00" | null,
 "ends_at": null,
 "location": null,
 "reason": "一句话依据",
 "action_required": true/false,
 "action_reason": "一句话依据"}
"""


class InboxEvent(BaseModel):
    """Structured result of one email event extraction."""

    is_event: bool = Field(default=False)
    kind: Literal["meeting", "social", "appointment", "none"] = "none"
    title: str | None = Field(default=None, max_length=200)
    starts_at: str | None = None
    ends_at: str | None = None
    location: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=300)
    action_required: bool = Field(default=False)
    action_reason: str | None = Field(default=None, max_length=300)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CN_TZ)
    return parsed


def normalize_event(event: InboxEvent) -> dict[str, Any] | None:
    """Convert a model result into a task-ready dict, or None when unusable."""
    if not event.is_event:
        return None
    starts_at = _parse_iso(event.starts_at)
    title = " ".join((event.title or "").split())
    if starts_at is None or not title:
        return None
    return {
        "kind": event.kind if event.kind != "none" else "social",
        "title": title[:200],
        "starts_at": starts_at,
        "ends_at": _parse_iso(event.ends_at),
        "location": (event.location or "").strip()[:200] or None,
        "reason": (event.reason or "").strip()[:300],
    }


def normalize_signal(signal: InboxEvent) -> dict[str, Any]:
    """Keep both the event (maybe None) and the action-required hint."""
    return {
        "is_event": signal.is_event,
        "event": normalize_event(signal),
        "action_required": signal.action_required,
        "action_reason": (signal.action_reason or "").strip()[:300] or None,
    }


async def extract_inbox_signal_with_llm(
    text: str,
    llm: ChatOpenAI,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the full signal for one email; None when the call itself failed."""
    if not (text or "").strip():
        return None
    current = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    # DeepSeek 不支持 json_schema，统一走 provider 感知的结构化调用。
    response = await invoke_structured_json(
        llm,
        InboxEvent,
        system_prompt=_INBOX_EVENT_SYSTEM_PROMPT,
        human_prompt=(
            f"当前时间：{current.strftime('%Y-%m-%d %H:%M %A')}（+08:00）\n\n"
            f"邮件内容：\n{text[:MAX_TEXT_CHARS]}"
        ),
    )
    if response is None:
        return None
    return normalize_signal(response)


async def extract_inbox_event_with_llm(
    text: str,
    llm: ChatOpenAI,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Extract an actionable event from email text; None when nothing usable."""
    signal = await extract_inbox_signal_with_llm(text, llm, now=now)
    if signal is None:
        return None
    return signal["event"]
