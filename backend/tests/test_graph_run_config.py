"""Regression tests for chat graph run configs.

The graph is compiled with a checkpointer, so LangGraph requires a thread_id in
`configurable` for every invoke/stream. `build_agent_graph_run_config` historically
returned None when LangSmith tracing was off, making valid chat calls fail even
though the checkpointer was configured.
"""

import uuid
import unittest
from unittest.mock import patch

from config import Settings
from services.langsmith_tracing import build_agent_graph_run_config


class BuildAgentGraphRunConfigTests(unittest.TestCase):
    def test_returns_config_with_thread_id_when_tracing_off(self) -> None:
        settings = Settings(
            LANGSMITH_TRACING_V2=False,
            LANGSMITH_API_KEY=None,
        )
        # A developer's .env may hold real Langfuse keys; this test asserts the
        # "nothing configured" shape, so force the no-handler path.
        with patch(
            "services.langsmith_tracing.build_langfuse_callback", return_value=None
        ):
            thread_id = str(uuid.uuid4())

            run_config = build_agent_graph_run_config(
                settings,
                user_id=uuid.uuid4(),
                thread_id=thread_id,
            )

        self.assertIsNotNone(run_config)
        self.assertIsNotNone(run_config)
        assert run_config is not None
        self.assertIn("configurable", run_config)
        self.assertEqual(run_config["configurable"]["thread_id"], thread_id)
        self.assertNotIn("callbacks", run_config)

    def test_returns_config_with_thread_id_when_tracing_on(self) -> None:
        settings = Settings(
            LANGSMITH_TRACING_V2=True,
            LANGSMITH_API_KEY="test-key",
        )
        thread_id = str(uuid.uuid4())

        run_config = build_agent_graph_run_config(
            settings,
            user_id=uuid.uuid4(),
            thread_id=thread_id,
        )

        self.assertIsNotNone(run_config)
        assert run_config is not None
        self.assertIn("configurable", run_config)
        self.assertEqual(run_config["configurable"]["thread_id"], thread_id)
        self.assertIn("callbacks", run_config)


if __name__ == "__main__":
    unittest.main()