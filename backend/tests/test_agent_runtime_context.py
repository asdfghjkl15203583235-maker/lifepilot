"""Regression tests for moving non-serializable runtime objects out of checkpoint state.

LangGraph checkpoints serialize the full state with msgpack. AsyncSession,
Settings and email credentials cannot be serialized, which broke every chat
invocation. Runtime-only values must live in LangGraph `context` instead.
"""

import unittest
from unittest.mock import MagicMock

from agents.graph import build_graph
from agents.state import AgentRuntimeContext


class AgentRuntimeContextTests(unittest.TestCase):
    def test_build_graph_uses_runtime_context_schema(self) -> None:
        graph = build_graph(MagicMock())

        self.assertIs(graph.context_schema, AgentRuntimeContext)


if __name__ == "__main__":
    unittest.main()