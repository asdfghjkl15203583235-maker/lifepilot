"""Cache compiled LangGraph instances — graph structure is static per chat model.

Compiling on every request adds tens of milliseconds; the graph only differs by which
ChatOpenAI instance is bound to nodes. When `langgraph_checkpointer=memory` is set,
one process-wide InMemorySaver is shared by the cached graphs so conversations can
resume interrupted runs.
"""

from functools import lru_cache

from langgraph.graph.state import CompiledStateGraph

from agents.graph import build_graph
from config import chat_llm, get_settings
from services.langgraph_checkpointer import get_memory_checkpointer, get_postgres_checkpointer


async def get_compiled_agent_graph(chat_model: str) -> CompiledStateGraph:
    """Return a compiled graph for the given OpenRouter model id.

    Prefer durable Postgres checkpoints; fall back to a process-wide in-memory
    saver so local Windows runs still recover interrupted state for the process.
    """
    settings = get_settings()
    llm = chat_llm(settings, model=chat_model)

    checkpointer = await get_postgres_checkpointer(settings)
    if checkpointer is None and settings.langgraph_checkpointer in {"postgres", "memory"}:
        checkpointer = get_memory_checkpointer()

    @lru_cache(maxsize=64)
    def _cached_graph(model_id: str) -> CompiledStateGraph:
        return build_graph(llm, checkpointer=checkpointer)

    return _cached_graph(chat_model)