"""混合意图：局部失败不得掩盖整体成功。

真实 badcase："明天八点开会，十点交纪要，十二点去财务部报销"。
报销落库成功，开会却失败，最终回复只说"本地日历没连接，没能写进日历"，
用户以为整条指令都失败了。

两个根因：
1. `calendar_agent_node` 依赖 Google OAuth token，没有 token 就谎称"本地日历未连接"，
   而前端日历页展示的是本地事件 + 带 due_at 的任务；
2. `merge_results_node` 只把各分支文本丢给 LLM，失败分支被弱化或吞掉。
"""

import asyncio
import json
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.calendar_agent_node import calendar_agent_node
from agents.orchestrator_node import _latest_result_per_task, merge_results_node
from config import Settings


def _settings() -> Settings:
    return Settings(openai_api_key="test", planner_timezone="Asia/Shanghai")


def _runtime(session, settings) -> MagicMock:
    runtime = MagicMock()
    runtime.context = {
        "db_session": session,
        "settings": settings,
        "calendar_access_token": None,
    }
    return runtime


class _CalendarLLM:
    """第一次调用写日程，第二次给出终答。"""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_system = ""

    def bind_tools(self, tools, **kwargs):
        self.tool_names = {t.name for t in tools}
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        self.seen_system = messages[0].content
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "create_event",
                    "args": {
                        "summary": "开会",
                        "start": "2026-10-30T08:00:00",
                        "end": "2026-10-30T09:00:00",
                    },
                    "id": "call-event-1",
                }],
            )
        return AIMessage(content="已把「开会」添加到明天 08:00-09:00。")

    async def astream(self, messages, **kwargs):
        raise NotImplementedError


class CalendarAgentLocalCalendarTests(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_claim_calendar_disconnected_without_google_token(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        session.add = MagicMock()
        session.flush = AsyncMock()

        llm = _CalendarLLM()
        result = await calendar_agent_node(
            {
                "user_id": uuid.uuid4(),
                "messages": [],
                "current_task": {"intent": "明天八点开会", "route": "calendar"},
            },
            llm,
            _runtime(session, _settings()),
        )

        answer = result["agent_results"][0]["result"]
        self.assertNotIn("未连接", answer)
        self.assertNotIn("Google", answer)
        self.assertEqual(result["agent_results"][0]["task_status"], "success")
        # 工具必须包含本地写库能力，而不是 Google 日历工具。
        self.assertIn("create_event", llm.tool_names)
        self.assertNotIn("list_calendars", llm.tool_names)

    async def test_writes_local_event_row(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        added: list = []
        session.add = MagicMock(side_effect=added.append)
        session.flush = AsyncMock()

        llm = _CalendarLLM()
        await calendar_agent_node(
            {
                "user_id": uuid.uuid4(),
                "messages": [],
                "current_task": {"intent": "明天八点开会", "route": "calendar"},
            },
            llm,
            _runtime(session, _settings()),
        )

        self.assertEqual(len(added), 1)
        event = added[0]
        self.assertEqual(event.title, "开会")
        self.assertTrue(event.uid.startswith("local-"))
        self.assertIsNotNone(event.starts_at)

    async def test_unbacked_write_claim_is_marked_failed(self) -> None:
        """没调工具却声称已添加日程 → 必须判失败，不能把幻觉当成功。"""

        class _HallucinatingLLM:
            def bind_tools(self, tools, **kwargs):
                return self

            async def ainvoke(self, messages, **kwargs):
                return AIMessage(content="已添加明天八点的开会日程。")

            async def astream(self, messages, **kwargs):
                raise NotImplementedError

        session = AsyncMock(spec=AsyncSession)
        session.add = MagicMock()
        session.flush = AsyncMock()

        result = await calendar_agent_node(
            {
                "user_id": uuid.uuid4(),
                "messages": [],
                "current_task": {"intent": "明天八点开会", "route": "calendar"},
            },
            _HallucinatingLLM(),
            _runtime(session, _settings()),
        )

        self.assertEqual(result["agent_results"][0]["task_status"], "failed")


class MergePartialFailureTests(unittest.IsolatedAsyncioTestCase):
    def test_latest_result_per_task_dedupes_retries(self) -> None:
        results = [
            {"task_key": "a", "attempt": 0, "result": "第一次失败", "task_status": "failed"},
            {"task_key": "a", "attempt": 1, "result": "第二次成功", "task_status": "success"},
        ]
        latest = _latest_result_per_task(results, [{"task_key": "a"}])
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["result"], "第二次成功")

    def test_dropped_replanned_task_is_not_reported(self) -> None:
        results = [
            {"task_key": "old-step", "result": "旧步骤失败", "task_status": "failed"},
            {"task_key": "new-step", "result": "新步骤成功", "task_status": "success"},
        ]
        latest = _latest_result_per_task(results, [{"task_key": "new-step"}])
        self.assertEqual([r["task_key"] for r in latest], ["new-step"])

    async def test_merge_keeps_failed_branch_visible(self) -> None:
        class _MergeLLM:
            async def ainvoke(self, messages, **kwargs):
                # 模拟真实模型：只讲成功项，把失败项吞掉。
                return AIMessage(content="报销已经添加好了。")

            async def astream(self, messages, **kwargs):
                raise NotImplementedError

        state = {
            "messages": [],
            "is_voice_request": False,
            "tasks": [{"task_key": "a"}, {"task_key": "b"}],
            "agent_results": [
                {
                    "task_key": "a",
                    "intent": "明天八点开会",
                    "route": "calendar",
                    "result": "日历服务暂时不可用。",
                    "task_status": "failed",
                    "error": "数据库超时",
                },
                {
                    "task_key": "b",
                    "intent": "十二点去财务部报销",
                    "route": "direct",
                    "result": "已把「去财务部报销」安排到 12:00。",
                    "task_status": "success",
                },
            ],
        }

        result = await merge_results_node(state, _MergeLLM())
        answer = result["final_answer"]
        self.assertIn("报销", answer)
        # 失败分支必须出现在最终回复里，且带上原因。
        self.assertIn("开会", answer)
        self.assertIn("数据库超时", answer)

    async def test_merge_prompt_carries_structured_status(self) -> None:
        seen: list = []

        class _MergeLLM:
            async def ainvoke(self, messages, **kwargs):
                seen.extend(messages)
                return AIMessage(content="好的。")

            async def astream(self, messages, **kwargs):
                raise NotImplementedError

        state = {
            "messages": [],
            "tasks": [{"task_key": "a"}, {"task_key": "b"}],
            "agent_results": [
                {"task_key": "a", "intent": "开会", "result": "失败", "task_status": "failed"},
                {"task_key": "b", "intent": "报销", "result": "成功", "task_status": "success"},
            ],
        }
        await merge_results_node(state, _MergeLLM())
        system = seen[0].content
        self.assertIn("failed", system)
        self.assertIn("如实告知", system)


if __name__ == "__main__":
    unittest.main()
