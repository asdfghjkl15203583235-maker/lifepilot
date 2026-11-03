"""模块二：direct_reply 必须挂载基础工具，且追问不得被降级丢掉工具（回归守卫）。

真实 badcase：
  - 上一句查天气成功，下一句"这是什么天气/那后天呢" → 模型回"没有可用的天气工具"；
  - 用户说"下周出差"，模型回"无法新建任务"。

根因：意图分类器在长上下文中把追问错误路由给纯聊天的 direct_reply_node，
而该节点又只挂了提醒/日程工具，没有天气与建任务工具，于是模型手里没工具可用。

修复：direct_reply 挂载基础工具（get_weather / create_task 等）作为兜底。
"""

import json
import unittest
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.orchestrator_node import direct_reply_node
from agents.tools.task_action_tools import (
    CREATE_TASK_TOOL_NAME,
    make_task_action_tools,
)
from agents.state import AgentState


class CapturingLLM:
    """记录 bind_tools 收到的工具名，随后直接给纯文本回复（无 tool_call）。"""

    def __init__(self) -> None:
        self.bound_tool_names: list[str] = []

    def bind_tools(self, tools: list[Any]) -> "CapturingLLM":
        self.bound_tool_names = [getattr(t, "name", "") for t in tools]
        return self


class DirectReplyBaseToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = MagicMock()
        self.settings.planner_timezone = "Asia/Shanghai"
        self.settings.reminder_default_lead_minutes = 30
        self.user_id = uuid.uuid4()
        self.session = AsyncMock(spec=AsyncSession)

    def _runtime(self) -> SimpleNamespace:
        return SimpleNamespace(
            context={"db_session": self.session, "settings": self.settings}
        )

    def _state(self, intent: str) -> AgentState:
        return AgentState(
            current_task={"intent": intent, "route": "direct"},
            messages=[],
            user_id=self.user_id,
        )

    def _patch_reply(self, reply: str = "好的。"):
        async def fake_astream_invoke(agent, messages, *, stream=True, **kwargs):
            return AIMessage(content=reply)

        patcher = patch("agents.orchestrator_node.astream_invoke", new=fake_astream_invoke)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_weather_tool_is_mounted(self) -> None:
        """追问落到 direct 时，"这是什么天气" 仍要有 get_weather 可用。"""
        self._patch_reply()
        llm = CapturingLLM()
        await direct_reply_node(
            self._state("这是什么天气"),
            llm=llm,  # type: ignore[arg-type]
            runtime=self._runtime(),  # type: ignore[arg-type]
        )
        self.assertIn("get_weather", llm.bound_tool_names)

    async def test_create_task_tool_is_mounted(self) -> None:
        """用户说"帮我建个任务：下周出差" 时 direct 必须有建任务工具。"""
        self._patch_reply()
        llm = CapturingLLM()
        await direct_reply_node(
            self._state("帮我建个任务：下周出差"),
            llm=llm,  # type: ignore[arg-type]
            runtime=self._runtime(),  # type: ignore[arg-type]
        )
        self.assertIn(CREATE_TASK_TOOL_NAME, llm.bound_tool_names)


class FollowupPromptTests(unittest.IsolatedAsyncioTestCase):
    """分类器 Prompt 在追问时必须带上"继承路由"死命令。"""

    async def test_classify_prompt_carries_inheritance_rule_on_followup(self) -> None:
        from langchain_core.messages import HumanMessage
        from agents.orchestrator_node import orchestrate_node
        from agents.common import FOLLOWUP_INHERITANCE_RULE

        captured: dict[str, str] = {}

        class _CaptureLLM:
            async def ainvoke(self, messages, **kwargs):
                captured["system"] = messages[0].content
                return AIMessage(content=json.dumps({"tasks": [{"intent": "x", "route": "life"}]}))

        state = {
            "messages": [
                HumanMessage(content="[10-28 09:00] user: 山西榆次明天天气怎么样？"),
                AIMessage(content="[10-28 09:00] assistant: 山西榆次明天晴。"),
                HumanMessage(content="[最新请求] user: 那后天呢？"),
            ],
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
        }
        await orchestrate_node(state, _CaptureLLM())
        self.assertIn(FOLLOWUP_INHERITANCE_RULE[:20], captured["system"])


class CreateTaskToolTests(unittest.IsolatedAsyncioTestCase):
    """create_task 工具本身要能落库并返回 created 回执。"""

    async def test_create_task_persists_and_returns_receipt(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        user_id = uuid.uuid4()
        tools = make_task_action_tools(session, user_id)
        tool = next(t for t in tools if t.name == CREATE_TASK_TOOL_NAME)
        with patch(
            "agents.tools.task_action_tools.create_task_for_user",
            new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), title="下周出差")),
        ):
            out = json.loads(await tool.ainvoke({"title": "下周出差"}))
        self.assertEqual(out["status"], "created")
        self.assertIn("task_id", out)


class CreateTaskNotBlockedTests(unittest.IsolatedAsyncioTestCase):
    """LLM 经 create_task 成功建任务后回"已创建任务"，绝不能被假回复拦截器打回。

    假回复拦截器（_contains_claim）把"已创建"当成需要回执的声称；create_task
    的成功回执（status=created + task_id）必须被认作有效凭证，否则用户说
    "建个任务"会得到"尝试但没成功"的错误回复。
    """

    def setUp(self) -> None:
        self.settings = MagicMock()
        self.settings.planner_timezone = "Asia/Shanghai"
        self.user_id = uuid.uuid4()
        self.session = AsyncMock(spec=AsyncSession)

    def _runtime(self) -> SimpleNamespace:
        return SimpleNamespace(
            context={"db_session": self.session, "settings": self.settings}
        )

    async def test_create_task_receipt_allows_created_claim(self) -> None:
        task_id = str(uuid.uuid4())
        sequence = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": CREATE_TASK_TOOL_NAME,
                        "args": {"title": "下周出差"},
                        "id": "call-1",
                    }
                ],
            ),
            AIMessage(content="已创建任务「下周出差」。"),
        ]

        async def fake_astream_invoke(agent, messages, *, stream=True, **kwargs):
            return sequence.pop(0)

        class _FakeCreateTool:
            name = CREATE_TASK_TOOL_NAME

            async def ainvoke(self, args):
                return json.dumps(
                    {"status": "created", "task_id": task_id, "title": "下周出差"},
                    ensure_ascii=False,
                )

        def fake_task_tools(session, user_id, settings=None):
            return [_FakeCreateTool()]

        with patch("agents.orchestrator_node.astream_invoke", new=fake_astream_invoke), \
             patch("agents.orchestrator_node.make_task_action_tools", new=fake_task_tools):
            result = await direct_reply_node(
                AgentState(
                    current_task={"intent": "帮我建个任务：下周出差", "route": "direct"},
                    messages=[],
                    user_id=self.user_id,
                ),
                llm=FakeBindLLM(),  # type: ignore[arg-type]
                runtime=self._runtime(),  # type: ignore[arg-type]
            )

        text = result["agent_results"][0]["result"]
        # 未被拦截器打回，回执原文保留；不是"尝试但没成功"。
        self.assertIn("已创建", text)
        self.assertNotIn("没有成功", text)


class FakeBindLLM:
    def bind_tools(self, tools):
        return self


if __name__ == "__main__":
    unittest.main()
