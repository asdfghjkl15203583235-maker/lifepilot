"""端到端：DAG 依赖之间的数据流转（长程复杂任务规划）。

指令："帮我查一下上海明天的天气，然后根据天气帮我写一封发给张三的邮件"

必须走完整 graph：orchestrate → planner → life_agent → supervisor →
email_agent → supervisor → merge_results。只有 LLM 与外部网络被替换成确定性
假实现，图的拓扑、Send 分发、依赖判定、状态合并都是真的。

断言的是数据真的流过去了：email_agent 拿到的 system prompt 里必须出现
life_agent 查到的真实天气文本，且提交的邮件草案正文引用了它。
"""

import asyncio
import json
import re
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession

from agents.graph import build_graph
from agents.state import AgentState

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")

# life_agent 查到的"真实"天气，必须原样出现在邮件正文里。
WEATHER_TEXT = "上海的天气：晴，气温 25°C"
RECIPIENT = "zhangsan@example.com"


def _plan_payload() -> dict:
    return {
        "plan_id": "plan-weather-email",
        "summary": "先查上海天气，再据此写邮件给张三",
        "tasks": [
            {
                "id": "step-1",
                "intent": "查询上海明天的天气",
                "route": "life",
                "priority": "medium",
                "project": "生活",
                "depends_on": [],
                "due_at": None,
            },
            {
                "id": "step-2",
                "intent": "根据天气写一封发给张三的邮件",
                "route": "email",
                "priority": "medium",
                "project": "工作",
                "depends_on": ["step-1"],
                "due_at": None,
            },
        ],
    }


class ScriptedLLM(BaseChatModel):
    """按 system prompt 的角色返回确定性输出，并记录收到的提示词。"""

    # 记录 email_agent 实际看到的 system prompt，用于断言数据流真的发生。
    email_system_prompts: list = []
    drafted_bodies: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted-llm"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = messages[0].content if isinstance(messages[0].content, str) else ""

        if "路由分类器" in system:
            content = json.dumps(
                {
                    "tasks": [
                        {"intent": "查询上海明天的天气", "route": "life"},
                        {"intent": "根据天气写一封发给张三的邮件", "route": "email"},
                    ]
                },
                ensure_ascii=False,
            )
        elif "LifePilot 规划器" in system:
            content = json.dumps(_plan_payload(), ensure_ascii=False)
        elif "邮件助手" in system:
            type(self).email_system_prompts.append(system)
            has_tool_result = any(
                type(m).__name__ == "ToolMessage" for m in messages
            )
            if has_tool_result:
                # 草案已提交，第二轮返回终答，否则会一直撞工具轮次上限。
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content="邮件草稿已准备好（等待你确认后才会真正发送）。"
                            )
                        )
                    ]
                )
            body = self._compose_body(system)
            type(self).drafted_bodies.append(body)
            message = AIMessage(
                content="",
                tool_calls=[{
                    "name": "propose_email_draft",
                    "args": {
                        "to": RECIPIENT,
                        "subject": "上海明日天气提醒",
                        "body": body,
                    },
                    "id": "call-propose-1",
                }],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        elif "日历助手" in system or "Notion 助手" in system:
            content = "（本测试不涉及该路由）"
        elif "合并" in system or "Chief of Staff" in system:
            content = "天气已查好，邮件草稿也已准备好，等待你确认发送。"
        else:
            content = "好的。"

        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content))]
        )

    @staticmethod
    def _compose_body(system_prompt: str) -> str:
        """只有真的拿到了上游天气，正文才会带上具体气温。"""
        match = re.search(r"气温\s*(-?\d+(?:\.\d+)?)\s*°C", system_prompt)
        condition = "晴" if "晴" in system_prompt else "未知"
        if match:
            return (
                f"张三你好：\n\n明天{condition}，气温 {match.group(1)}°C，"
                "适合外出，注意增减衣物。\n\n祝好"
            )
        return "张三你好：\n\n明天天气情况我还没有拿到，稍后补上。\n\n祝好"


def _session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=session)
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _runtime_context(session, settings) -> dict:
    return {
        "db_session": session,
        "settings": settings,
        "email_credentials": MagicMock(name="mail163-config"),
        "gmail_access_token": None,
        "calendar_access_token": None,
        "notion_access_token": None,
    }


class LongHorizonDataFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_weather_result_flows_into_email_draft(self) -> None:
        ScriptedLLM.email_system_prompts = []
        ScriptedLLM.drafted_bodies = []

        session = _session()

        created_rows: list[MagicMock] = []

        async def fake_create_task(session_, *, user_id, body):
            row = MagicMock()
            row.id = uuid.uuid4()
            row.title = getattr(body, "title", "")
            created_rows.append(row)
            return row

        async def fake_weather(settings, *, city=None, latitude=None, longitude=None):
            return {
                "provider": "amap",
                "city": "上海",
                "condition": "晴",
                "temperature_c": 25,
            }

        graph = build_graph(ScriptedLLM(), checkpointer=InMemorySaver())
        initial: AgentState = {
            "user_id": USER_ID,
            "messages": [
                HumanMessage(
                    content="帮我查一下上海明天的天气，然后根据天气帮我写一封发给张三的邮件"
                )
            ],
            "is_voice_request": False,
            "memory_context": "",
        }

        with patch(
            "agents.planner_agent_node.create_task_for_user", fake_create_task
        ), patch(
            "agents.planner_agent_node.get_planner_rules",
            AsyncMock(return_value=[]),
        ), patch(
            "agents.planner_agent_node.schedule_deadline_reminder",
            AsyncMock(return_value=None),
        ), patch(
            "agents.life_agent_node.get_weather", fake_weather
        ), patch(
            "services.pending_action_service.create_pending_action",
            AsyncMock(side_effect=self._fake_pending_action),
        ):
            result = await graph.ainvoke(
                initial,
                {"configurable": {"thread_id": "long-horizon-weather-email"}},
                context=_runtime_context(session, MagicMock(planner_timezone="Asia/Shanghai")),
            )

        # 1) 计划真的是两个有依赖关系的步骤。
        tasks = result.get("tasks") or []
        self.assertEqual(len(tasks), 2, f"应规划出两步，实际: {tasks}")
        life_task, email_task = tasks[0], tasks[1]
        self.assertEqual(life_task["route"], "life")
        self.assertEqual(email_task["route"], "email")
        # depends_on 必须指向上游的持久化 task_key，否则下游永远不会就绪。
        self.assertEqual(email_task["depends_on"], [life_task["task_key"]])

        # 2) 上游结果被记录进了 step_results。
        step_results = result.get("step_results") or {}
        self.assertIn(life_task["task_key"], step_results)
        self.assertIn("25", step_results[life_task["task_key"]])

        # 3) email_agent 确实在 prompt 里看到了上游天气（这才是数据流转）。
        self.assertTrue(ScriptedLLM.email_system_prompts, "email agent 未被调用")
        email_prompt = ScriptedLLM.email_system_prompts[-1]
        self.assertIn("上游步骤", email_prompt)
        self.assertIn("25", email_prompt)

        # 4) 邮件正文准确引用天气。
        self.assertTrue(ScriptedLLM.drafted_bodies, "未生成邮件草案")
        body = ScriptedLLM.drafted_bodies[-1]
        self.assertIn("25", body)
        self.assertIn("晴", body)

        # 5) 两个任务都已完成，图正常收敛。
        self.assertEqual(len(result.get("completed_task_ids") or []), 2)
        self.assertIn("草稿", result.get("final_answer") or "")

    @staticmethod
    async def _fake_pending_action(session, **kwargs):
        row = MagicMock()
        row.id = uuid.uuid4()
        return row


class UpstreamContextUnitTests(unittest.TestCase):
    """不跑图，直接验证拼接逻辑的边界行为。"""

    def test_no_dependencies_yields_empty_context(self) -> None:
        from agents.common import upstream_results_context

        task = {"task_key": "t2", "depends_on": []}
        self.assertEqual(
            upstream_results_context(task, {"t1": "上海的天气：晴 25°C"}), ""
        )

    def test_failed_upstream_is_not_recorded(self) -> None:
        from agents.common import TASK_STATUS_FAILED, step_result_update

        update = step_result_update(
            {"task_key": "t1"}, "工具错误：超时", TASK_STATUS_FAILED
        )
        self.assertEqual(update, {})

    def test_partial_upstream_is_recorded(self) -> None:
        from agents.common import TASK_STATUS_PARTIAL, step_result_update

        update = step_result_update(
            {"task_key": "t1"}, "上海的天气：晴 25°C", TASK_STATUS_PARTIAL
        )
        self.assertEqual(update["step_results"]["t1"], "上海的天气：晴 25°C")

    def test_context_renders_dependency_output(self) -> None:
        from agents.common import upstream_results_context

        tasks = [
            {"id": "step-1", "task_key": "key-1", "intent": "查天气"},
            {"id": "step-2", "task_key": "key-2", "intent": "写邮件"},
        ]
        task = {**tasks[1], "depends_on": ["key-1"]}
        context = upstream_results_context(
            task, {"key-1": "上海的天气：晴，气温 25°C"}, tasks
        )
        self.assertIn("查天气", context)
        self.assertIn("25", context)
        # 下游自己不能混进上游结果。
        self.assertNotIn("写邮件] 的结果", context)

    def test_concurrent_step_results_merge_without_loss(self) -> None:
        from agents.state import merge_step_results

        merged = merge_step_results({"a": "1"}, {"b": "2"})
        self.assertEqual(merged, {"a": "1", "b": "2"})
        self.assertEqual(merge_step_results(None, {"b": "2"}), {"b": "2"})


class LifeAgentWeatherTests(unittest.IsolatedAsyncioTestCase):
    async def test_weather_intent_awaits_city_parse(self) -> None:
        """`_parse_city` 是 async，漏掉 await 会把 coroutine 传给 get_weather。

        真实故障：`'coroutine' object has no attribute 'strip'` —— 天气查询直接
        500。这里断言 city 已经被解析成字符串，而不是协程对象。
        """
        from agents import life_agent_node as node

        captured: dict[str, object] = {}

        async def fake_weather(settings, city=None):
            captured["city"] = city
            return {"city": city or "未知", "condition": "晴", "temperature_c": 25}

        settings = MagicMock(planner_timezone="Asia/Shanghai")
        runtime = MagicMock()
        runtime.context = _runtime_context(MagicMock(), settings)
        with patch.object(node, "get_weather", fake_weather):
            result = await node.life_agent_node(
                {
                    "user_id": USER_ID,
                    "messages": [HumanMessage(content="山西榆次明天天气怎么样？")],
                    "current_task": {"intent": "查询山西榆次明天天气", "route": "life"},
                },
                MagicMock(),
                runtime,
            )

        self.assertIsInstance(captured["city"], str)
        self.assertIn("榆次", captured["city"])
        self.assertIn("25", result["agent_results"][0]["result"])


if __name__ == "__main__":
    unittest.main()
