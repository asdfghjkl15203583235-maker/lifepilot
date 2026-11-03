"""Regression tests: direct_reply must turn a planner-resolved `due_at` into a reminder.

Bug (T2): for "我待会儿还要把快递放回宿舍" the planner strips the fuzzy time word
("待会儿") out of `intent` and puts it into `due_at`. `direct_reply_node` only ever
read `intent`, so the time word was gone from the text it inspected and no reminder
was created — the task silently lost its time.

The fix: the forced-reminder fallback must trigger on `task["due_at"]` alone, even
when `intent` contains no temporal clue at all.

Run with: uv run pytest tests/test_direct_reply_due_at_reminder.py
"""

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.orchestrator_node import direct_reply_node
from agents.state import AgentState


class FakeLLM:
    """Stand-in for ChatOpenAI: only needs .bind_tools (astream_invoke is patched)."""

    def bind_tools(self, tools: list[Any]) -> "FakeLLM":
        return self


class _FakeReminderTool:
    name = "create_reminder"

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self._calls = calls

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self._calls.append(args)
        return json.dumps(
            {
                "status": "created",
                "reminder_id": str(uuid.uuid4()),
                "task_id": str(uuid.uuid4()),
                "title": args.get("title"),
                "remind_at": str(args.get("remind_at")),
            },
            ensure_ascii=False,
        )


class DirectReplyDueAtReminderTests(unittest.IsolatedAsyncioTestCase):
    """`due_at` 存在即必须建提醒，不管 intent 里还有没有时间词。"""

    def setUp(self) -> None:
        self.settings = MagicMock()
        self.settings.planner_timezone = "Asia/Shanghai"
        self.settings.reminder_default_lead_minutes = 30
        self.user_id = uuid.uuid4()
        self.session = AsyncMock(spec=AsyncSession)
        self.reminder_calls: list[dict[str, Any]] = []
        # 未来的本地墙钟时间（naive），避免测试随日期失效。
        self.tz = ZoneInfo("Asia/Shanghai")
        self.future_local = (datetime.now(self.tz) + timedelta(hours=3)).replace(
            second=0, microsecond=0, tzinfo=None
        )
        self.future_utc = self.future_local.replace(tzinfo=self.tz).astimezone(timezone.utc)

    def _runtime(self) -> SimpleNamespace:
        return SimpleNamespace(context={"db_session": self.session, "settings": self.settings})

    def _state(self, task: dict[str, Any]) -> AgentState:
        return AgentState(
            current_task=task,
            messages=[],
            user_id=self.user_id,
        )

    def _patch_backends(self, reply: str = "好的，记下了。") -> None:
        async def fake_astream_invoke(agent: Any, messages: Any, *, stream: bool = True, **kwargs: Any) -> AIMessage:
            return AIMessage(content=reply)

        calls = self.reminder_calls

        def fake_factory(session: Any, user_id: Any, settings: Any) -> _FakeReminderTool:
            return _FakeReminderTool(calls)

        for target, new in (
            ("agents.orchestrator_node.astream_invoke", fake_astream_invoke),
            ("agents.orchestrator_node.make_create_reminder_tool", fake_factory),
        ):
            patcher = patch(target, new=new)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_due_at_alone_triggers_reminder(self) -> None:
        """时间词已被 planner 剥进 due_at，intent 里一个时间词都没有 → 仍必须建提醒。"""
        self._patch_backends()
        result = await direct_reply_node(
            self._state(
                {
                    "intent": "把快递放回宿舍",
                    "route": "direct",
                    "due_at": self.future_local.isoformat(),  # naive 本地墙钟
                }
            ),
            llm=FakeLLM(),
            runtime=self._runtime(),  # type: ignore[arg-type]
        )

        self.assertEqual(len(self.reminder_calls), 1, "due_at 存在时必须落一条提醒")
        call = self.reminder_calls[0]
        self.assertEqual(call["title"], "把快递放回宿舍")
        # 本地墙钟被正确解释为用户时区，而不是当成 UTC。
        self.assertEqual(
            datetime.fromisoformat(call["remind_at"]).astimezone(timezone.utc),
            self.future_utc,
        )
        text = result["agent_results"][0]["result"]
        self.assertIn("提醒", text)
        self.assertIn(self.future_local.strftime("%H:%M"), text)

    async def test_fuzzy_time_is_announced_as_approximate(self) -> None:
        """模糊时间只能猜，必须说明是"大概这个点"并邀请纠正，不能伪装成精确时间。"""
        self._patch_backends()
        result = await direct_reply_node(
            self._state({"intent": "待会儿把快递放回宿舍", "route": "direct"}),
            llm=FakeLLM(),
            runtime=self._runtime(),  # type: ignore[arg-type]
        )

        self.assertEqual(len(self.reminder_calls), 1)
        text = result["agent_results"][0]["result"]
        self.assertIn("大概", text)
        self.assertIn("告诉我", text)

    async def test_no_due_at_and_no_time_word_creates_nothing(self) -> None:
        """纯闲聊：既无 due_at 也无时间词 → 绝不凭空建提醒。"""
        self._patch_backends()
        result = await direct_reply_node(
            self._state({"intent": "你好", "route": "direct"}),
            llm=FakeLLM(),
            runtime=self._runtime(),  # type: ignore[arg-type]
        )

        self.assertEqual(self.reminder_calls, [])
        self.assertEqual(result["agent_results"][0]["result"], "好的，记下了。")

    async def test_past_due_at_does_not_create_reminder(self) -> None:
        """过期 due_at 不该落一条马上炸开的提醒。"""
        self._patch_backends()
        await direct_reply_node(
            self._state(
                {
                    "intent": "把快递放回宿舍",
                    "route": "direct",
                    "due_at": "2020-01-01T10:00:00",
                }
            ),
            llm=FakeLLM(),
            runtime=self._runtime(),  # type: ignore[arg-type]
        )

        self.assertEqual(self.reminder_calls, [])

    async def test_llm_created_reminder_is_not_duplicated(self) -> None:
        """LLM 自己已经建好提醒 → 兜底不再重复建。"""
        calls = self.reminder_calls

        class _FakeTool:
            name = "create_reminder"

            async def ainvoke(self, args: dict[str, Any]) -> str:
                calls.append(args)
                return json.dumps(
                    {"status": "created", "reminder_id": str(uuid.uuid4())},
                    ensure_ascii=False,
                )

        sequence = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "create_reminder",
                        "args": {"title": "把快递放回宿舍", "remind_at": "2026-10-26T12:00:00+00:00"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="已设置提醒。"),
        ]

        async def fake_astream_invoke(agent: Any, messages: Any, *, stream: bool = True, **kwargs: Any) -> AIMessage:
            return sequence.pop(0)

        for target, new in (
            ("agents.orchestrator_node.astream_invoke", fake_astream_invoke),
            ("agents.orchestrator_node.make_create_reminder_tool", lambda *a, **k: _FakeTool()),
        ):
            patcher = patch(target, new=new)
            patcher.start()
            self.addCleanup(patcher.stop)

        result = await direct_reply_node(
            self._state(
                {
                    "intent": "把快递放回宿舍",
                    "route": "direct",
                    "due_at": self.future_local.isoformat(),
                }
            ),
            llm=FakeLLM(),
            runtime=self._runtime(),  # type: ignore[arg-type]
        )

        self.assertEqual(len(self.reminder_calls), 1, "只应落一条提醒（LLM 自己那条）")
        self.assertEqual(result["agent_results"][0]["result"], "已设置提醒。")


if __name__ == "__main__":
    unittest.main()
