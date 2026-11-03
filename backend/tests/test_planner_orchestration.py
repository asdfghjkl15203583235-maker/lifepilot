import asyncio
import unittest
from unittest.mock import MagicMock

from agents.graph import _fan_out
from agents.planning import task_layers
from agents.supervisor_node import _result_failed, _task_needs_replan, supervisor_node


class PlannerOrchestrationTests(unittest.TestCase):
    def test_dependency_layers_group_parallel_and_serial_steps(self):
        tasks = [
            {"id": "a", "depends_on": []},
            {"id": "b", "depends_on": []},
            {"id": "c", "depends_on": ["a", "b"]},
            {"id": "d", "depends_on": ["c"]},
        ]
        layers = task_layers(tasks)
        self.assertEqual(
            [[t["id"] for t in layer] for layer in layers],
            [["a", "b"], ["c"], ["d"]],
        )

    def test_dependency_layers_handle_missing_parents(self):
        tasks = [
            {"id": "a", "depends_on": ["missing"]},
            {"id": "b", "depends_on": ["a", "missing"]},
        ]
        layers = task_layers(tasks)
        self.assertEqual([[t["id"] for t in layer] for layer in layers], [["a"], ["b"]])

    def test_fan_out_only_returns_ready_layer(self):
        tasks = [
            {"id": "a", "task_key": "a", "depends_on": [], "route": "direct"},
            {"id": "b", "task_key": "b", "depends_on": ["a"], "route": "direct"},
        ]
        first = _fan_out({"tasks": tasks})
        self.assertEqual([send.node for send in first], ["direct_reply"])
        self.assertEqual(first[0].arg["current_task"]["task_key"], "a")

    def test_result_failed_detects_error_markers(self):
        self.assertTrue(_result_failed({"result": "Sorry, could not connect"}))
        self.assertTrue(_result_failed({"result": "The email API failed"}))
        self.assertFalse(_result_failed({"result": "The weather is sunny"}))

    def test_task_needs_replan_checks_task_key(self):
        task = {"task_key": "t1", "intent": "x"}
        matched = _task_needs_replan(task, {"t1": [{"result": "failed"}]})
        not_matched = _task_needs_replan(task, {"t1": [{"result": "ok"}]})
        self.assertTrue(matched)
        self.assertFalse(not_matched)

    def test_supervisor_continues_to_next_dependency_layer(self):
        tasks = [
            {"id": "a", "task_key": "a", "intent": "x", "depends_on": []},
            {"id": "b", "task_key": "b", "intent": "y", "depends_on": ["a"]},
        ]
        state = {
            "tasks": tasks,
            "agent_results": [{"task_key": "a", "result": "ok", "attempt": 0}],
            "completed_task_ids": [],
            "plan": {"iteration": 0},
            "settings": MagicMock(planner_max_iterations=2),
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertEqual(result["supervisor_decision"]["action"], "continue")
        self.assertEqual(result["completed_task_ids"], ["a"])
        # 保留完整任务表：_fan_out 自己按 completed/ready 过滤。早先只回
        # next_ready，会把依赖尚未满足的下游步骤永久丢掉（3 步链必现）。
        self.assertEqual([t["task_key"] for t in result["tasks"]], ["a", "b"])

    def test_supervisor_keeps_three_step_chain_alive(self):
        tasks = [
            {"task_key": "a", "intent": "查天气", "depends_on": [], "route": "life"},
            {"task_key": "b", "intent": "写邮件", "depends_on": ["a"], "route": "email"},
            {"task_key": "c", "intent": "总结", "depends_on": ["b"], "route": "direct"},
        ]
        state = {
            "tasks": tasks,
            "agent_results": [{"task_key": "a", "task_status": "success", "attempt": 0}],
            "completed_task_ids": [],
            "plan": {"iteration": 0},
            "settings": MagicMock(planner_max_iterations=3),
            "step_results": {"a": "上海的天气：晴 25°C"},
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertEqual(result["supervisor_decision"]["action"], "continue")
        # 第三步 c 依赖未完成的 b，必须仍在 tasks 里，否则整条链断掉。
        self.assertEqual([t["task_key"] for t in result["tasks"]], ["a", "b", "c"])

    def test_supervisor_replans_blocked_step(self):
        state = {
            "tasks": [{"task_key": "t1", "id": "t1", "intent": "x", "depends_on": []}],
            "agent_results": [{"task_key": "t1", "result": "failed", "attempt": 0}],
            "completed_task_ids": [],
            "plan": {"iteration": 0},
            "settings": MagicMock(planner_max_iterations=1),
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertEqual(result["supervisor_decision"]["action"], "replan")
        self.assertEqual(result["plan"]["iteration"], 1)
        # 真正的重规划：失败步骤被丢弃，并显式要求规划器重建计划。
        self.assertTrue(result["force_replan"])
        self.assertEqual([t["task_key"] for t in result["tasks"]], [])
        self.assertEqual(
            [t["intent"] for t in result["replan_context"]["failed"]], ["x"]
        )

    def test_supervisor_keeps_pending_steps_for_replan(self):
        tasks = [
            {"task_key": "a", "intent": "fail", "depends_on": []},
            {"task_key": "b", "intent": "later", "depends_on": ["a"]},
        ]
        state = {
            "tasks": tasks,
            "agent_results": [
                {"task_key": "a", "task_status": "failed", "error": "boom", "attempt": 0}
            ],
            "completed_task_ids": [],
            "plan": {"iteration": 0},
            "settings": MagicMock(planner_max_iterations=3),
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertTrue(result["force_replan"])
        self.assertEqual([t["task_key"] for t in result["tasks"]], ["b"])
        self.assertEqual(result["replan_context"]["failed"][0]["error"], "boom")

    def test_structured_status_overrides_keyword_heuristics(self):
        # 查天气成功但正文含"无法确定明天是否下雨" → 不得判失败。
        self.assertFalse(
            _result_failed({
                "task_status": "success",
                "result": "北京晴。无法确定明天是否下雨。",
            })
        )
        # 正文没写"失败"但结构化状态是 failed → 必须重规划。
        self.assertTrue(
            _result_failed({"task_status": "failed", "result": "已尽力。"})
        )
        # partial（例如草案待确认）不应重复执行。
        self.assertFalse(_result_failed({"task_status": "partial", "result": "草案已就绪"}))

    def test_supervisor_does_not_stale_fail_new_attempt(self):
        state = {
            "tasks": [{"task_key": "t1", "id": "t1", "intent": "x", "depends_on": [], "retry_count": 1}],
            "agent_results": [
                {"task_key": "t1", "result": "failed", "attempt": 0},
                {"task_key": "t1", "result": "ok", "attempt": 1},
            ],
            "completed_task_ids": [],
            "plan": {"iteration": 1},
            "settings": MagicMock(planner_max_iterations=4),
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertEqual(result["supervisor_decision"]["action"], "done")

    def test_supervisor_merges_when_all_done(self):
        state = {
            "tasks": [{"task_key": "t1", "id": "t1", "intent": "x", "depends_on": []}],
            "agent_results": [{"task_key": "t1", "result": "ok", "attempt": 0}],
            "completed_task_ids": [],
            "plan": {"iteration": 0},
            "settings": MagicMock(planner_max_iterations=2),
        }
        result = asyncio.run(supervisor_node(state, MagicMock()))
        self.assertEqual(result["supervisor_decision"]["action"], "done")


class PlannerSingleStepPersistenceTests(unittest.TestCase):
    def test_read_only_query_step_is_not_persisted_as_task(self) -> None:
        """纯查询步骤是图内部执行环节，绝不能被写成用户看板上的任务。

        回归：线上曾出现"查询山西榆次明天天气"重复多行、混进任务看板。
        """
        import uuid
        from unittest.mock import AsyncMock, patch

        from agents.planner_agent_node import planner_node
        from sqlalchemy.ext.asyncio import AsyncSession

        user_id = uuid.uuid4()
        task_id = uuid.uuid4()
        row = MagicMock()
        row.id = task_id

        session = AsyncMock(spec=AsyncSession)
        session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=session)
        session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

        messages = [MagicMock()]
        messages[0].content = "查一下今天北京天气"
        state = {
            "db_session": session,
            "user_id": user_id,
            "settings": MagicMock(),
            "messages": messages,
            "tasks": [{"intent": "查天气", "route": "life"}],
        }

        mock_create = AsyncMock(return_value=row)
        with patch(
            "agents.planner_agent_node.create_task_for_user",
            mock_create,
        ):
            result = asyncio.run(planner_node(state, MagicMock()))

        self.assertIsNone(result["tasks"][0]["persisted_task_id"])
        self.assertEqual(result["tasks"][0]["route"], "life")
        self.assertEqual(result["plan"]["status"], "planned")
        mock_create.assert_not_awaited()

    def test_planner_keeps_direct_task_in_mixed_intents_without_persisting_it(self) -> None:
        """"提醒我明早开会，顺便帮我查下天气" → 混合 [direct, life] 时 direct 必须透传,
        且不能被 planner 落库为持久化 task（由 direct_reply 的工具真正落库）。"""
        import uuid
        from unittest.mock import AsyncMock, patch

        from agents.planner_agent_node import planner_node
        from sqlalchemy.ext.asyncio import AsyncSession

        user_id = uuid.uuid4()
        task_id = uuid.uuid4()
        row = MagicMock()
        row.id = task_id

        session = AsyncMock(spec=AsyncSession)
        session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=session)
        session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

        messages = [MagicMock()]
        messages[0].content = "提醒我明早开会，顺便帮我查下天气"
        state = {
            "db_session": session,
            "user_id": user_id,
            "settings": MagicMock(),
            "messages": messages,
            "tasks": [
                {"intent": "明早开会提醒", "route": "direct"},
                {"intent": "查天气", "route": "life"},
            ],
        }

        mock_create = AsyncMock(return_value=row)
        with patch(
            "agents.planner_agent_node.create_task_for_user",
            mock_create,
        ):
            result = asyncio.run(planner_node(state, MagicMock()))

        routes = [t["route"] for t in result["tasks"]]
        self.assertEqual(routes, ["direct", "life"])
        # 两步都是内部执行环节：direct 由 create_reminder 工具落库，
        # life 只是查天气，两者都不该被规划器写进任务看板。
        self.assertIsNone(result["tasks"][0]["persisted_task_id"])
        self.assertEqual(result["tasks"][0]["task_key"], "step-1")
        self.assertEqual(result["tasks"][0]["intent"], "明早开会提醒")
        self.assertIsNone(result["tasks"][1]["persisted_task_id"])
        self.assertEqual(result["tasks"][1]["intent"], "查天气")
        mock_create.assert_not_awaited()

    def test_planner_without_temporal_clue_does_not_hit_unbound_datetime(self) -> None:
        """回归：无时间线索分支曾因函数内局部 import 导致 UnboundLocalError。

        「帮我发一封邮件给 test@test.com」不含任何时间词，planner 走
        `_build_plan_tasks` 分支，随后遍历任务推断 due_at 时曾访问未绑定的
        局部 `datetime`，直接把邮件 HITL 流程打断。
        """
        import uuid
        from unittest.mock import AsyncMock, patch

        from agents.planner_agent_node import planner_node
        from sqlalchemy.ext.asyncio import AsyncSession

        task_id = uuid.uuid4()
        row = MagicMock()
        row.id = task_id

        session = AsyncMock(spec=AsyncSession)
        session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=session)
        session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
        messages = [MagicMock()]
        messages[0].content = "帮我发一封邮件给 test@test.com，内容是测试"
        state = {
            "db_session": session,
            "user_id": uuid.uuid4(),
            "settings": MagicMock(),
            "messages": messages,
            "tasks": [{"intent": "发送邮件给 test@test.com", "route": "email"}],
        }

        with patch(
            "agents.planner_agent_node.create_task_for_user",
            AsyncMock(return_value=row),
        ):
            result = asyncio.run(planner_node(state, MagicMock()))

        self.assertEqual(result["tasks"][0]["route"], "email")
        self.assertIsNone(result["tasks"][0]["due_at"])
        # 发邮件是内部执行步骤，HITL 草案走 pending_actions，不进任务看板。
        self.assertIsNone(result["tasks"][0]["persisted_task_id"])


class LifeToolTests(unittest.TestCase):
    def test_weather_tool_wires_through_life_client(self):
        from unittest.mock import AsyncMock, patch

        from agents.tools.life_tool import LifeTool
        from config import Settings

        settings = Settings(weather_default_city="Beijing")
        tool = LifeTool(None, settings)
        self.assertEqual(
            [t.name for t in tool.tools()],
            # 没有 db session / user_id 时不挂 create_task
            ["get_weather", "get_map_context", "get_route_planning"],
        )
        fake_weather = {"provider": "amap", "city": "Beijing", "temperature_c": 11}
        with patch(
            "agents.tools.life_tool.get_weather",
            AsyncMock(return_value=fake_weather),
        ) as mock_weather:
            weather_result = asyncio.run(tool._weather("Beijing"))
        self.assertIsInstance(weather_result, str)
        self.assertIn("Beijing", weather_result)
        mock_weather.assert_awaited_once_with(settings, city="Beijing", latitude=None, longitude=None)


class PlannerTemporalDueAtTests(unittest.IsolatedAsyncioTestCase):
    """单步含时间线索（"上午10点去库迪咖啡买拿铁"）必须走规划 LLM：
    title 剥离时间、due_at 按本地时区转成 UTC（02:00Z = 本地10点），不再 +8h 位移。"""

    async def test_temporal_single_step_persists_clean_title_and_correct_due_at(self) -> None:
        import json
        import uuid
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, patch

        from agents.planner_agent_node import planner_node
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from sqlalchemy.ext.asyncio import AsyncSession

        class FakePlanner(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "fake-planner"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content=json.dumps(
                                    {
                                        "plan_id": "test-plan",
                                        "summary": "去买拿铁",
                                        "tasks": [
                                            {
                                                "id": "step-1",
                                                "intent": "去买拿铁",
                                                "route": "life",
                                                "priority": "medium",
                                                "depends_on": [],
                                                "due_at": "2026-10-25T10:00:00",
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        )
                    ]
                )

        user_id = uuid.uuid4()
        task_id = uuid.uuid4()
        row = MagicMock()
        row.id = task_id

        session = AsyncMock(spec=AsyncSession)
        session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=session)
        session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

        state = {
            "db_session": session,
            "user_id": user_id,
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
            "messages": [HumanMessage(content="上午10点去库迪咖啡买拿铁")],
            "tasks": [{"intent": "去买拿铁", "route": "life"}],
        }

        mock_create = AsyncMock(return_value=row)
        with patch("agents.planner_agent_node.create_task_for_user", mock_create):
            result = await planner_node(state, FakePlanner())

        body = mock_create.call_args.kwargs["body"]
        # 标题干净：只剩动作，无时间状语。
        self.assertEqual(body.title, "去买拿铁")
        self.assertNotIn("上午", body.title)
        self.assertNotIn("10点", body.title)
        # due_at 正确：本地 10:00 in Asia/Shanghai == 02:00Z，绝对时刻不再 +8h。
        self.assertEqual(body.due_at, datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(result["tasks"][0]["persisted_task_id"], str(task_id))


class PlannerForceReplanTests(unittest.IsolatedAsyncioTestCase):
    """回归守卫：force_replan 必须真的清空旧计划重建，而不是拿旧任务重试。"""

    async def test_forced_replan_rebuilds_instead_of_reusing_old_tasks(self) -> None:
        import json
        import uuid
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from sqlalchemy.ext.asyncio import AsyncSession

        from agents.planner_agent_node import planner_node

        class FakePlanner(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "fake-planner-replan"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content=json.dumps(
                                    {
                                        "plan_id": "replan",
                                        "summary": "换一种方式读邮件",
                                        "tasks": [
                                            {
                                                "id": "step-1",
                                                "intent": "用主题关键词重新检索邮件",
                                                "route": "email",
                                                "priority": "medium",
                                                "depends_on": [],
                                                "due_at": None,
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        )
                    ]
                )

        task_id = uuid.uuid4()
        row = MagicMock()
        row.id = task_id

        session = AsyncMock(spec=AsyncSession)
        session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=session)
        session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

        old_failed = {"id": "step-1", "intent": "读取邮件", "route": "email"}
        old_pending = {"id": "step-2", "intent": "总结邮件", "route": "direct", "task_key": "step-2"}
        state = {
            "db_session": session,
            "user_id": uuid.uuid4(),
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
            "messages": [HumanMessage(content="把邮件读完并总结")],
            # 旧 plan/tasks 存在，正常情况下 planner 会直接 return {}。
            "plan": {"plan_id": "old", "iteration": 1},
            "tasks": [old_pending],
            "force_replan": True,
            "replan_context": {
                "failed": [{**old_failed, "error": "IMAP 超时"}],
                "pending": [{"intent": "总结邮件", "route": "direct"}],
                "completed": [],
            },
        }

        with patch(
            "agents.planner_agent_node.create_task_for_user",
            AsyncMock(return_value=row),
        ):
            result = await planner_node(state, FakePlanner())

        intents = [t["intent"] for t in result["tasks"]]
        # 新规划步骤 + 保留的未执行步骤；旧失败步骤不在其中。
        self.assertIn("用主题关键词重新检索邮件", intents)
        self.assertIn("总结邮件", intents)
        self.assertNotIn("读取邮件", intents)
        # 标志被消费，下一轮恢复正常规划。
        self.assertFalse(result["force_replan"])
        self.assertEqual(result["replan_context"], {})
        self.assertEqual(result["plan"]["iteration"], 1)


class PlannerDirectStepDueAtTests(unittest.IsolatedAsyncioTestCase):
    """direct 步骤也要拿到兜底 due_at。

    回归：planner 落库循环里 `route == "direct"` 直接 continue，导致 direct 步骤
    既不做 `_infer_due_at_local` 兜底也不归一 due_at。LLM 一旦把时间词剥出 intent
    却忘了给 due_at（"我待会儿还要把快递放回宿舍" → intent="把快递放回宿舍"），
    时间就整条链丢失，direct_reply 无从建提醒。
    """

    async def test_direct_step_without_due_at_gets_inferred_due_at(self) -> None:
        import json
        import uuid
        from datetime import datetime, timedelta, timezone
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from sqlalchemy.ext.asyncio import AsyncSession

        from agents.planner_agent_node import planner_node

        class FakePlanner(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "fake-planner-direct"

            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                # LLM 剥掉了时间词，但没给 due_at —— 正是要兜底的场景。
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content=json.dumps(
                                    {
                                        "plan_id": "test-plan",
                                        "summary": "放快递",
                                        "tasks": [
                                            {
                                                "id": "step-1",
                                                "intent": "把快递放回宿舍",
                                                "route": "direct",
                                                "priority": "medium",
                                                "depends_on": [],
                                                "due_at": None,
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        )
                    ]
                )

        session = AsyncMock(spec=AsyncSession)
        state = {
            "db_session": session,
            "user_id": uuid.uuid4(),
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
            "messages": [HumanMessage(content="我待会儿还要把快递放回宿舍")],
            "tasks": [{"intent": "把快递放回宿舍", "route": "direct"}],
        }

        before = datetime.now(timezone.utc)
        with patch(
            "agents.planner_agent_node.get_planner_rules",
            new=AsyncMock(return_value=[]),
        ), patch(
            "agents.planner_agent_node.create_task_for_user",
            new=AsyncMock(),
        ) as mock_create:
            result = await planner_node(state, FakePlanner())

        # direct 步骤不落库（由 direct_reply 的工具落库）。
        mock_create.assert_not_awaited()
        due_raw = result["tasks"][0]["due_at"]
        self.assertIsNotNone(due_raw, "direct 步骤必须带兜底 due_at，否则时间整条丢失")
        due_utc = datetime.fromisoformat(str(due_raw))
        if due_utc.tzinfo is None:
            self.fail(f"due_at 必须是带时区的 UTC 时刻，实际: {due_raw}")
        due_utc = due_utc.astimezone(timezone.utc)
        # "待会儿" → 当前时间 +1 小时（±5 分钟容差）。
        self.assertAlmostEqual(
            due_utc.timestamp(),
            (before + timedelta(hours=1)).timestamp(),
            delta=300,
        )


class TaskProjectClassifierTests(unittest.TestCase):
    """Planner 生成任务的 project 语义分类（工作/生活/学习/其他）。"""

    def test_classify_work_and_life(self):
        from services.task_project_classifier import classify_task_project

        self.assertEqual(classify_task_project("去交水电费"), "生活")
        self.assertEqual(classify_task_project("写周报"), "工作")
        self.assertEqual(classify_task_project("把周报写了"), "工作")
        self.assertEqual(classify_task_project("复习高数"), "学习")
        self.assertEqual(classify_task_project("出门遛狗"), "生活")
        self.assertEqual(classify_task_project("随便做点事"), "生活")  # 判断不出 → 默认生活

    def test_validate_project_accepts_only_enums(self):
        from services.task_project_classifier import validate_task_project

        self.assertEqual(validate_task_project("工作"), "工作")
        self.assertEqual(validate_task_project("学习"), "学习")
        self.assertEqual(validate_task_project("LifePilot"), "生活")  # 非法值 → 默认
        self.assertEqual(validate_task_project(None), "生活")
        self.assertEqual(validate_task_project(""), "生活")

    def test_plan_to_tasks_classifies_llm_and_fallback_projects(self):
        import uuid

        from agents.planner_agent_node import _plan_to_tasks

        # ① LLM 给出合法 project → 直接用。
        payload_llm = {
            "plan_id": "plan-x",
            "tasks": [{"id": "a", "intent": "写周报", "project": "工作"}],
        }
        _, tasks = _plan_to_tasks(payload_llm, uuid.uuid4())
        self.assertEqual(tasks[0]["project"], "工作")
        # ② LLM 给非法 project → 按关键词兜底 "交水电费"→生活。
        payload_bad = {
            "plan_id": "plan-y",
            "tasks": [{"id": "b", "intent": "明天去交水电费", "project": "LifePilot"}],
        }
        _, tasks = _plan_to_tasks(payload_bad, uuid.uuid4())
        self.assertEqual(tasks[0]["project"], "生活")


class PlannerFuzzyTimeInferenceTests(unittest.TestCase):
    """Planner 模糊时间代码兜底：一会/稍后→+1h，明天/后天无钟点→09:00。"""

    def _infer(self, text, now):
        from agents.planner_agent_node import _infer_due_at_local

        return _infer_due_at_local(text, now, "Asia/Shanghai")

    def test_fuzzy_word_adds_one_hour(self):
        from datetime import datetime

        now = datetime(2026, 10, 26, 15, 0, 0)
        for word in ("一会", "稍后", "晚点", "回头", "等会", "马上"):
            self.assertEqual(
                self._infer(f"{word}交水电费", now),
                datetime(2026, 10, 26, 16, 0, 0),
                word,
            )

    def test_tomorrow_without_clock_defaults_to_nine(self):
        from datetime import datetime

        now = datetime(2026, 10, 26, 15, 0, 0)
        self.assertEqual(self._infer("明天去交水电费", now), datetime(2026, 10, 27, 9, 0, 0))
        self.assertEqual(self._infer("后天交周报", now), datetime(2026, 10, 28, 9, 0, 0))

    def test_explicit_clock_skips_fallback(self):
        from datetime import datetime

        now = datetime(2026, 10, 26, 15, 0, 0)
        # 明确给了钟点 → 不兜底（交由 LLM 精确 due_at）。
        self.assertIsNone(self._infer("明天早上八点改简历", now))

    def test_no_time_returns_none(self):
        from datetime import datetime

        now = datetime(2026, 10, 26, 15, 0, 0)
        self.assertIsNone(self._infer("出门遛狗", now))
        self.assertIsNone(self._infer("", now))


class TemporalTitleCleaningTests(unittest.TestCase):
    """Planner 落库前把时间状语从 title 剥离进 due_at（回归守卫）。"""

    def test_strip_temporal_prefix_cleans_title(self):
        from agents.planner_agent_node import _strip_temporal_prefix

        cases = {
            "上午10点去库迪咖啡买拿铁": "去库迪咖啡买拿铁",
            "设置明天早上八点的简历修改提醒": "简历修改",
            "下午三点开会": "开会",
            "下周二交周报": "交周报",
            "去库迪买拿铁": "去库迪买拿铁",
            "买牛奶": "买牛奶",
        }
        for raw, expected in cases.items():
            self.assertEqual(_strip_temporal_prefix(raw), expected, f"case: {raw}")

    def test_strip_pure_time_keeps_title(self):
        from agents.planner_agent_node import _strip_temporal_prefix

        # 纯时间短语不清空，避免把任务清成空 title。
        self.assertEqual(_strip_temporal_prefix("上午10点"), "上午10点")
        self.assertEqual(_strip_temporal_prefix("明天早上八点"), "明天早上八点")

    def test_has_temporal_clue_detects_time(self):
        from agents.planner_agent_node import _has_temporal_clue
        from langchain_core.messages import HumanMessage

        self.assertTrue(_has_temporal_clue([HumanMessage(content="上午10点去库迪咖啡买拿铁")]))
        self.assertTrue(_has_temporal_clue([HumanMessage(content="提醒我明早9点开会")]))
        # 模糊时间词同样触发 LLM 解析。
        self.assertTrue(_has_temporal_clue([HumanMessage(content="稍后帮我交水电费")]))
        self.assertTrue(_has_temporal_clue([HumanMessage(content="一会去拿快递")]))
        self.assertFalse(_has_temporal_clue([HumanMessage(content="去库迪买拿铁")]))
        self.assertFalse(_has_temporal_clue([HumanMessage(content="看看最近一周的邮件")]))

    def test_has_temporal_clue_only_scans_latest_message(self):
        # 回归守卫：历史消息里的时间词不得污染当前轮判定，否则用户这轮只是
        # 问"去库迪买拿铁"，也会因为上轮的"明天"被强行建提醒。
        from agents.planner_agent_node import _has_temporal_clue
        from langchain_core.messages import AIMessage, HumanMessage

        history = [
            HumanMessage(content="明天早上八点提醒我改简历"),
            AIMessage(content="好的，已安排。"),
            HumanMessage(content="去库迪买拿铁"),
        ]
        self.assertFalse(_has_temporal_clue(history))

    def test_planner_prompt_requires_naive_local_due_at(self):
        # 回归守卫：due_at 必须是 naive 本地时间（无 Z 后缀），否则后端会把
        # 本地上午10点按 UTC 解释成 +8h 位移（卡片 18:00 / 倒计时 7h36m 的根因）。
        from agents.planner_agent_node import PLANNER_SYSTEM_PROMPT

        self.assertIn("{tz_name}", PLANNER_SYSTEM_PROMPT)
        self.assertIn("无 Z", PLANNER_SYSTEM_PROMPT)
        # prompt 不应再指导 LLM 输出 UTC 绝对时间。
        self.assertNotIn("YYYY-MM-DDTHH:MM:SSZ", PLANNER_SYSTEM_PROMPT)


class LocalTaskRoutingTests(unittest.TestCase):
    """回归守卫：说"任务/待办"且没提 Notion 时，绝不能路由到 notion 代理。

    线上故障：用户说「把任务X标记为已完成」，分类器判给 notion，agent 跑去
    Notion 工作区搜索并回"没找到它"，任务纹丝不动、也没有确认草案。
    """

    def test_detects_local_task_phrasing(self):
        from agents.common import looks_like_local_task_management

        for text in (
            "把任务「报销300」标记为已完成",
            "帮我把待办删掉",
            "这个任务改期到下周一",
            "to-do 里那条逾期了",
        ):
            self.assertTrue(looks_like_local_task_management(text), text)

    def test_respects_explicit_notion_mentions(self):
        from agents.common import looks_like_local_task_management

        for text in (
            "在 Notion 里建一个任务页面",
            "去工作区的数据库找一下任务",
            "把 Notion 页面上的待办同步过来",
        ):
            self.assertFalse(looks_like_local_task_management(text), text)

    def test_plain_chat_is_not_task_management(self):
        from agents.common import looks_like_local_task_management

        for text in ("你好", "今天天气怎么样？", "讲个笑话"):
            self.assertFalse(looks_like_local_task_management(text), text)


if __name__ == "__main__":
    unittest.main()