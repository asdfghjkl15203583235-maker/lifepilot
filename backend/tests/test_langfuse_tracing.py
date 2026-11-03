"""Langfuse wiring tests — trace attributes, naming, and graceful degradation."""

import unittest
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

from config import Settings
from services.langfuse_tracing import (
    LangfuseTraceScope,
    build_langfuse_callback,
    flush_langfuse_callback,
    is_langfuse_tracing_active,
    langfuse_trace_scope,
)
from services.langsmith_tracing import build_agent_graph_run_config


def _settings(**overrides: Any) -> Settings:
    settings = Settings()
    settings.langsmith_tracing_enabled = False
    settings.langsmith_api_key = None
    settings.langfuse_public_key = None
    settings.langfuse_secret_key = None
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class LangfuseCallbackTests(unittest.TestCase):
    def test_inactive_without_keys(self) -> None:
        settings = _settings()
        self.assertFalse(is_langfuse_tracing_active(settings))
        self.assertIsNone(build_langfuse_callback(settings))

    def test_inactive_with_only_public_key(self) -> None:
        settings = _settings(langfuse_public_key="pk")
        self.assertFalse(is_langfuse_tracing_active(settings))

    def test_builds_handler_when_configured(self) -> None:
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        fake_handler = object()
        fake_cls = MagicMock(return_value=fake_handler)
        with patch(
            "services.langfuse_tracing._load_callback_handler",
            return_value=fake_cls,
        ), patch(
            "services.langfuse_tracing._get_langfuse_client", return_value=object()
        ):
            handler = build_langfuse_callback(settings)

        self.assertIs(handler, fake_handler)
        fake_cls.assert_called_once_with(public_key="pk")

    def test_initializes_client_before_handler(self) -> None:
        """The handler's get_client(public_key=...) needs a pre-initialized client."""
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        fake_cls = MagicMock(return_value=object())
        with patch(
            "services.langfuse_tracing._load_callback_handler", return_value=fake_cls
        ), patch(
            "services.langfuse_tracing._get_langfuse_client", return_value=object()
        ) as get_client:
            build_langfuse_callback(settings)

        get_client.assert_called_once_with(settings)

    def test_handler_skipped_when_client_unavailable(self) -> None:
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        fake_cls = MagicMock()
        with patch(
            "services.langfuse_tracing._load_callback_handler", return_value=fake_cls
        ), patch("services.langfuse_tracing._get_langfuse_client", return_value=None):
            self.assertIsNone(build_langfuse_callback(settings))
        fake_cls.assert_not_called()

    def test_missing_package_degrades_to_none(self) -> None:
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        with patch("services.langfuse_tracing._load_callback_handler", return_value=None):
            self.assertIsNone(build_langfuse_callback(settings))

    def test_constructor_failure_degrades_to_none(self) -> None:
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        fake_cls = MagicMock(side_effect=RuntimeError("boom"))
        with patch(
            "services.langfuse_tracing._load_callback_handler",
            return_value=fake_cls,
        ), patch(
            "services.langfuse_tracing._get_langfuse_client", return_value=object()
        ):
            self.assertIsNone(build_langfuse_callback(settings))

    def test_flush_calls_client(self) -> None:
        handler = MagicMock()
        flush_langfuse_callback(handler)
        handler._langfuse_client.flush.assert_called_once_with()

    def test_flush_without_client_is_noop(self) -> None:
        flush_langfuse_callback(object())


class RunConfigLangfuseTests(unittest.TestCase):
    def test_langfuse_callback_added_when_configured(self) -> None:
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        fake_handler = object()
        user_id = uuid.uuid4()
        with patch(
            "services.langsmith_tracing.build_langfuse_callback",
            return_value=fake_handler,
        ):
            config = build_agent_graph_run_config(
                settings,
                user_id=user_id,
                thread_id="thread-1",
            )

        self.assertIn("callbacks", config)
        self.assertIn(fake_handler, config["callbacks"])
        self.assertEqual(config["configurable"]["thread_id"], "thread-1")
        self.assertEqual(config["metadata"]["user_id"], str(user_id))

    def test_langfuse_trace_attributes_use_prefixed_metadata_keys(self) -> None:
        """The v5 handler only reads langfuse_* metadata keys for attribution."""
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        user_id = uuid.uuid4()
        with patch(
            "services.langsmith_tracing.build_langfuse_callback",
            return_value=object(),
        ):
            config = build_agent_graph_run_config(
                settings,
                user_id=user_id,
                thread_id="thread-abc",
                session_id="conversation-abc",
                trace_tags=["chat"],
                run_name="chat-turn",
            )

        metadata = config["metadata"]
        self.assertEqual(metadata["langfuse_user_id"], str(user_id))
        self.assertEqual(metadata["langfuse_session_id"], "conversation-abc")
        self.assertEqual(metadata["langfuse_trace_name"], "chat-turn")
        self.assertEqual(config["run_name"], "chat-turn")

    def test_langfuse_keys_absent_when_langfuse_disabled(self) -> None:
        settings = _settings()
        with patch(
            "services.langsmith_tracing.build_langfuse_callback",
            return_value=None,
        ):
            config = build_agent_graph_run_config(
                settings,
                user_id=uuid.uuid4(),
                thread_id="thread-3",
                run_name="chat-turn",
            )
        self.assertNotIn("callbacks", config)
        self.assertNotIn("langfuse_user_id", config.get("metadata", {}))

    def test_no_callbacks_when_no_tracing_configured(self) -> None:
        settings = _settings()
        config = build_agent_graph_run_config(
            settings,
            user_id=uuid.uuid4(),
            thread_id="thread-2",
        )
        self.assertNotIn("callbacks", config)
        self.assertEqual(config["configurable"]["thread_id"], "thread-2")


class LangfuseTraceScopeTests(unittest.TestCase):
    def test_scope_without_keys_is_noop(self) -> None:
        settings = _settings()
        with patch("services.langfuse_tracing._get_langfuse_client") as get_client:
            with langfuse_trace_scope(
                settings,
                user_id=uuid.uuid4(),
                session_id="thread-1",
                trace_name="chat-turn",
                trace_input="hello",
            ) as scope:
                scope.set_output("hi")
        get_client.assert_not_called()

    def test_scope_sets_name_input_and_propagated_attributes(self) -> None:
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        user_id = uuid.uuid4()
        span = MagicMock()
        client = MagicMock()
        client.start_as_current_observation.return_value.__enter__.return_value = span

        fake_propagate = MagicMock()
        fake_propagate.return_value.__enter__.return_value = None

        with patch(
            "services.langfuse_tracing._get_langfuse_client", return_value=client
        ), patch("langfuse.propagate_attributes", fake_propagate):
            with langfuse_trace_scope(
                settings,
                user_id=user_id,
                session_id="thread-9",
                trace_name="chat-turn",
                trace_input="明天有什么安排？",
                tags=["chat"],
            ) as scope:
                scope.set_output("你有两个会议。")

        client.start_as_current_observation.assert_called_once_with(
            as_type="span", name="chat-turn", input="明天有什么安排？"
        )
        kwargs = fake_propagate.call_args.kwargs
        self.assertEqual(kwargs["user_id"], str(user_id))
        self.assertEqual(kwargs["session_id"], "thread-9")
        self.assertEqual(kwargs["trace_name"], "chat-turn")
        self.assertEqual(kwargs["tags"], ["chat"])
        span.update.assert_called_once_with(output="你有两个会议。")

    def test_scope_never_raises_when_span_creation_fails(self) -> None:
        settings = _settings(langfuse_public_key="pk", langfuse_secret_key="sk")
        client = MagicMock()
        client.start_as_current_observation.side_effect = RuntimeError("boom")
        with patch("services.langfuse_tracing._get_langfuse_client", return_value=client):
            with langfuse_trace_scope(
                settings, user_id=uuid.uuid4(), trace_input="hello"
            ) as scope:
                scope.set_output("hi")

    def test_noop_scope_swallows_output_errors(self) -> None:
        LangfuseTraceScope().set_output("ignored")


if __name__ == "__main__":
    unittest.main()
