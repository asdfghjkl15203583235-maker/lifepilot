"""Full-graph runtime-context integration tests.

Regression: the agent graph used to carry AsyncSession / Settings / OAuth
tokens inside AgentState. LangGraph's checkpointer msgpack-serializes state,
which blew up with "Type is not msgpack serializable: AsyncSession" on every
chat call. Runtime-only values now travel in LangGraph `context` instead.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from agents.graph import build_graph
from agents.state import AgentState


class FakeLLM(BaseChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = messages[0].content if isinstance(messages[0].content, str) else ""
        if "路由分类器" in system:
            content = '{"tasks":[{"intent":"普通对话","route":"direct"}]}'
        else:
            content = "你好！我是你的 Chief of Staff。"
        message = AIMessage(content=content)
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self) -> str:
        return "fake-llm"


def _runtime_context(session, settings):
    return {
        "db_session": session,
        "settings": settings,
        "email_credentials": None,
        "gmail_access_token": None,
        "calendar_access_token": None,
        "notion_access_token": None,
    }


import unittest


class AgentFullGraphRuntimeTests(unittest.TestCase):
    def test_invoke_with_real_async_session_in_context(self) -> None:
        graph = build_graph(FakeLLM(), checkpointer=InMemorySaver())
        session = AsyncMock()
        settings = MagicMock()
        initial: AgentState = {
            "user_id": uuid.uuid4(),
            "messages": [HumanMessage(content="你好")],
            "is_voice_request": False,
            "memory_context": "",
        }

        result = asyncio.run(
            graph.ainvoke(
                initial,
                {"configurable": {"thread_id": "regress-async-session"}},
                context=_runtime_context(session, settings),
            )
        )

        self.assertIn("Chief of Staff", result.get("final_answer", ""))
        # AsyncSession must never have entered checkpointable state.
        checkpointable = result.get("db_session")
        self.assertIsNone(checkpointable)

    def test_invoke_without_runtime_context_still_works(self) -> None:
        graph = build_graph(FakeLLM(), checkpointer=InMemorySaver())
        initial: AgentState = {
            "user_id": uuid.uuid4(),
            "messages": [HumanMessage(content="你好")],
            "is_voice_request": False,
            "memory_context": "",
        }

        result = asyncio.run(
            graph.ainvoke(
                initial,
                {"configurable": {"thread_id": "regress-no-context"}},
            )
        )

        self.assertIn("Chief of Staff", result.get("final_answer", ""))


if __name__ == "__main__":
    unittest.main()