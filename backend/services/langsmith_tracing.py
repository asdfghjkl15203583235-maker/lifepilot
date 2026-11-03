"""Build LangGraph run configs that send traces to LangSmith/Langfuse when settings allow."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client

from config import LANGSMITH_DEFAULT_PROJECT_NAME, Settings
from services.langfuse_tracing import (
    build_langfuse_callback,
    flush_langfuse_callback,
)

logger = logging.getLogger(__name__)


def is_langsmith_tracing_active(settings: Settings) -> bool:
    return bool(settings.langsmith_tracing_enabled and settings.langsmith_api_key)


def build_agent_graph_run_config(
    settings: Settings,
    *,
    user_id: uuid.UUID,
    trace_tags: list[str] | None = None,
    trace_metadata: dict[str, Any] | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
    run_name: str | None = None,
) -> RunnableConfig:
    """Return a RunnableConfig for a graph run.

    The graph is compiled with a checkpointer, so `configurable.thread_id` is
    always required. Tracing callbacks are added only when LangSmith or Langfuse
    is configured; the config is otherwise still returned so the checkpointer has
    its thread key.
    """
    configurable = {}
    if thread_id:
        configurable["thread_id"] = thread_id

    langsmith_active = is_langsmith_tracing_active(settings)
    langfuse_handler = build_langfuse_callback(settings)
    if not langsmith_active and langfuse_handler is None:
        return {"configurable": configurable} if configurable else {}

    metadata: dict[str, Any] = {"user_id": str(user_id)}
    if trace_metadata:
        metadata.update(trace_metadata)
    # The Langfuse v5 callback handler only reads these prefixed keys for trace
    # attribution; every other metadata entry is copied onto observations
    # untouched (see _parse_langfuse_trace_attributes).
    if langfuse_handler is not None:
        metadata["langfuse_user_id"] = str(user_id)
        if session_id:
            metadata["langfuse_session_id"] = session_id
        if run_name:
            metadata["langfuse_trace_name"] = run_name
    metadata_str = {k: str(v) for k, v in metadata.items()}

    tags = list(trace_tags) if trace_tags else []
    callbacks: list[Any] = []

    if langsmith_active:
        api_key = settings.langsmith_api_key
        if api_key is not None:
            project_name = settings.langsmith_project or LANGSMITH_DEFAULT_PROJECT_NAME
            client_kwargs: dict[str, Any] = {"api_key": api_key}
            if settings.langsmith_api_url:
                client_kwargs["api_url"] = settings.langsmith_api_url

            callbacks.append(
                LangChainTracer(
                    client=Client(**client_kwargs),
                    project_name=project_name,
                    tags=tags,
                    metadata=metadata_str,
                )
            )
            logger.debug("LangSmith tracing enabled for user_id=%s project=%s", user_id, project_name)

    if langfuse_handler is not None:
        callbacks.append(langfuse_handler)
        logger.debug(
            "Langfuse tracing enabled for user_id=%s host=%s",
            user_id,
            settings.langfuse_host,
        )

    run_config: RunnableConfig = {
        "callbacks": callbacks,
        "metadata": metadata_str,
        "tags": tags,
        **({"configurable": configurable} if configurable else {}),
    }
    if run_name:
        run_config["run_name"] = run_name
    return run_config


def flush_tracing_after_run(run_config: RunnableConfig | None) -> None:
    """Flush per-run tracing clients so traces are not dropped on short-lived workers."""
    if run_config is None:
        return
    callbacks = run_config.get("callbacks")
    if not callbacks:
        return
    for handler in callbacks:
        if isinstance(handler, LangChainTracer) and handler.client is not None:
            handler.client.flush()
        elif type(handler).__module__.startswith("langfuse"):
            flush_langfuse_callback(handler)


# Backwards-compatible alias; existing call sites keep working.
flush_langsmith_after_run = flush_tracing_after_run
