"""Langfuse tracing for LangGraph runs.

The CallbackHandler records node spans, LLM generations (token usage) and tool
input/output as nested observations. A wrapper span around each graph run gives
the trace a readable name and a meaningful input/output instead of the raw graph
state, and propagates user/session attributes to every child observation.

Everything degrades to a no-op when the public/secret keys are not configured, so
local development never fails on a missing tracing backend.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from config import Settings

logger = logging.getLogger(__name__)

DEFAULT_TRACE_NAME = "chat-turn"


def is_langfuse_tracing_active(settings: Settings) -> bool:
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def _load_callback_handler() -> type[Any] | None:
    """Import CallbackHandler across langfuse v3+/v2 package layouts."""
    try:
        from langfuse.langchain import CallbackHandler  # langfuse >= 3
    except ImportError:
        try:
            from langfuse.callback import CallbackHandler  # langfuse 2.x
        except ImportError:
            return None
    return CallbackHandler


def _export_settings_to_env(settings: Settings) -> None:
    """Langfuse's client reads credentials from the environment.

    Settings are loaded from .env into a Pydantic object, which does not populate
    os.environ; bridge only the values that are missing so an explicitly exported
    environment variable always wins.
    """
    for env_name, value in (
        ("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key),
        ("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key),
        ("LANGFUSE_BASE_URL", settings.langfuse_host),
        ("LANGFUSE_HOST", settings.langfuse_host),
    ):
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value


def _get_langfuse_client(settings: Settings) -> Any | None:
    """Return the shared Langfuse client, or None when it cannot be created."""
    try:
        from langfuse import get_client
    except ImportError:
        logger.warning("Langfuse keys are set but the langfuse package is not installed; tracing disabled")
        return None
    _export_settings_to_env(settings)
    try:
        return get_client()
    except Exception:
        logger.warning("Langfuse client initialization failed; tracing disabled", exc_info=True)
        return None


def build_langfuse_callback(settings: Settings) -> Any | None:
    """Return a Langfuse CallbackHandler, or None when tracing is unavailable.

    Never raises: a missing package or a bad configuration only logs a warning.
    """
    if not is_langfuse_tracing_active(settings):
        return None

    handler_cls = _load_callback_handler()
    if handler_cls is None:
        logger.warning("Langfuse keys are set but the langfuse package is not installed; tracing disabled")
        return None

    # The handler resolves its client via get_client(public_key=...), which only
    # returns a working client if one was already initialized for that key.
    # Initialize it first, otherwise the handler silently gets a disabled client
    # and no trace is ever exported.
    if _get_langfuse_client(settings) is None:
        return None
    try:
        return handler_cls(public_key=settings.langfuse_public_key)
    except TypeError:
        # Older handlers take no constructor arguments and read env themselves.
        try:
            return handler_cls()
        except Exception:
            logger.warning("Langfuse handler initialization failed; tracing disabled", exc_info=True)
            return None
    except Exception:
        logger.warning("Langfuse handler initialization failed; tracing disabled", exc_info=True)
        return None


class LangfuseTraceScope:
    """Handle for the trace's root observation; a no-op when tracing is disabled."""

    def __init__(self, span: Any | None = None) -> None:
        self._span = span

    def set_output(self, output: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.update(output=output)
        except Exception:
            logger.warning("Langfuse trace output update failed", exc_info=True)


@contextmanager
def langfuse_trace_scope(
    settings: Settings,
    *,
    user_id: uuid.UUID | str,
    session_id: str | None = None,
    trace_name: str = DEFAULT_TRACE_NAME,
    trace_input: Any = None,
    tags: list[str] | None = None,
) -> Iterator[LangfuseTraceScope]:
    """Open one named trace for a graph run.

    The span becomes the trace root, so `trace_input` and `set_output()` are what
    a reader sees as the request/response of the trace; graph nodes, generations
    and tool calls nest underneath it.
    """
    if not is_langfuse_tracing_active(settings):
        yield LangfuseTraceScope()
        return

    client = _get_langfuse_client(settings)
    if client is None:
        yield LangfuseTraceScope()
        return

    try:
        from langfuse import propagate_attributes
    except ImportError:
        yield LangfuseTraceScope()
        return

    try:
        span_context = client.start_as_current_observation(
            as_type="span",
            name=trace_name,
            input=trace_input,
        )
    except Exception:
        logger.warning("Langfuse trace span creation failed; tracing disabled", exc_info=True)
        yield LangfuseTraceScope()
        return

    with span_context as span:
        with propagate_attributes(
            user_id=str(user_id),
            session_id=str(session_id) if session_id else None,
            trace_name=trace_name,
            tags=list(tags) if tags else None,
        ):
            yield LangfuseTraceScope(span)


def flush_langfuse_callback(handler: Any) -> None:
    """Flush the handler's client so traces survive short-lived workers."""
    client = getattr(handler, "_langfuse_client", None)
    flush = getattr(client, "flush", None)
    if not callable(flush):
        return
    try:
        flush()
    except Exception:
        logger.warning("Langfuse flush failed", exc_info=True)
