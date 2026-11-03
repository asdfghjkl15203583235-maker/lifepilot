"""Request-scoped streaming helpers for final agent replies."""

import asyncio
import contextvars
import uuid
from collections.abc import Sequence
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

_active_collector: contextvars.ContextVar["TokenCollector | None"] = contextvars.ContextVar(
    "active_token_collector",
    default=None,
)


def set_active_collector(collector: "TokenCollector") -> contextvars.Token:
    return _active_collector.set(collector)


def reset_active_collector(token: contextvars.Token) -> None:
    _active_collector.reset(token)


def get_active_collector() -> "TokenCollector | None":
    return _active_collector.get()


class TokenCollector:
    """Collect streamed text chunks while discarding tool-call-only responses."""

    def __init__(self, on_token: Callable[[str], None] | None = None) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._on_token = on_token
        self._discarding = False

    def begin_response(self) -> None:
        self._discarding = False

    def observe(self, chunk: Any) -> None:
        if _has_tool_calls(chunk):
            self._discarding = True
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            return
        if self._discarding:
            return
        text = _chunk_text(chunk)
        if not text:
            return
        self._queue.put_nowait(text)
        if self._on_token is not None:
            self._on_token(text)

    def drain_nowait(self) -> list[str]:
        tokens: list[str] = []
        while True:
            try:
                tokens.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return tokens

    async def drain(self) -> str:
        return "".join(self.drain_nowait())


def _chunk_text(chunk: Any) -> str:
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _has_tool_calls(chunk: Any) -> bool:
    if getattr(chunk, "tool_call_chunks", None):
        return True
    tool_calls = getattr(chunk, "tool_calls", None)
    return bool(tool_calls)


async def astream_invoke(
    llm: BaseChatModel,
    messages: Sequence[BaseMessage],
    *,
    stream: bool = True,
    **kwargs: Any,
) -> AIMessage:
    """Stream an LLM call while still returning the complete AIMessage.

    When a request-scoped TokenCollector is active, text deltas are queued for
    the SSE router. Tool-call responses are dropped automatically so internal
    tool arguments never leak into the visible transcript.
    """
    collector = get_active_collector() if stream else None
    if collector is None:
        return await llm.ainvoke(messages, **kwargs)

    collector.begin_response()
    try:
        chunks = [chunk async for chunk in llm.astream(messages, **kwargs)]
    except (NotImplementedError, TypeError, ValueError):
        return await llm.ainvoke(messages, **kwargs)

    for chunk in chunks:
        collector.observe(chunk)

    if not chunks:
        return await llm.ainvoke(messages, **kwargs)

    merged = chunks[0]
    for chunk in chunks[1:]:
        merged = merged + chunk

    if isinstance(merged, AIMessage):
        return merged
    return AIMessage(content=_chunk_text(merged), id=str(uuid.uuid4()))