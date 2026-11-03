"""模块六：上下文与状态分离（叙事流 vs 事实库）。

真实问题：把工具调用的原始长 JSON（高德完整返回、notion 页面数据）无限追加进
messages 历史，Token 暴涨，且模型在历史里"考古"事实容易被上一轮的旧 JSON 带偏。

修复：
  - messages 只保留用户/助手的**对话文本流**（叙事流）；工具原始 JSON 不进历史；
  - core_intent / step_results / pending_actions 作为 State 的一等公民字段，
    通过结构化 System Prompt 注入（事实库），而不是让模型去历史里翻。
"""

import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.context import (
    is_tool_payload,
    render_state_facts,
    sanitize_narrative,
)
from services.agent_runner import _build_timeline_messages


class IsToolPayloadTests(unittest.TestCase):
    def test_amap_json_blob_detected(self) -> None:
        blob = json.dumps({
            "provider": "amap",
            "city": "上海",
            "raw": {"lives": [{"weather": "晴", "temperature": "18"}]},
        }, ensure_ascii=False)
        self.assertTrue(is_tool_payload(blob))

    def test_reminder_receipt_detected(self) -> None:
        blob = json.dumps({"status": "created", "reminder_id": "abc", "task_id": "t"})
        self.assertTrue(is_tool_payload(blob))

    def test_plain_prose_not_detected(self) -> None:
        self.assertFalse(is_tool_payload("上海今天晴，气温 18°C。"))
        self.assertFalse(is_tool_payload("你好呀"))
        self.assertFalse(is_tool_payload(""))


class SanitizeNarrativeTests(unittest.TestCase):
    def test_tool_json_message_dropped_from_narrative(self) -> None:
        blob = json.dumps({"status": "created", "reminder_id": "x"})
        payloads = [
            {"role": "user", "content": "提醒我明天开会"},
            {"role": "assistant", "content": blob},
            {"role": "user", "content": "好"},
        ]
        cleaned = sanitize_narrative(payloads)
        contents = [m["content"] for m in cleaned]
        self.assertNotIn(blob, contents)
        self.assertIn("提醒我明天开会", contents)

    def test_conversation_text_is_preserved(self) -> None:
        payloads = [
            {"role": "user", "content": "山西榆次明天天气怎么样"},
            {"role": "assistant", "content": "山西榆次明天晴，18°C。"},
        ]
        self.assertEqual(sanitize_narrative(payloads), payloads)


class RenderStateFactsTests(unittest.TestCase):
    def test_core_intent_and_step_results_rendered(self) -> None:
        state = {
            "core_intent": {
                "topic": "山西榆次天气",
                "route": "life",
                "entities": {"destination": "山西榆次"},
                "followup": True,
                "query": "山西榆次天气；那后天呢",
            },
            "step_results": {"step-1": "山西榆次明天晴，18°C"},
        }
        block = render_state_facts(state)
        self.assertIsInstance(block, SystemMessage)
        self.assertIn("山西榆次", block.content)
        self.assertIn("life", block.content)
        # step_results 作为事实注入。
        self.assertIn("step-1", block.content)

    def test_empty_state_returns_none(self) -> None:
        self.assertIsNone(render_state_facts({}))


class TimelineMessagesExcludesToolJsonTests(unittest.TestCase):
    def test_build_timeline_drops_tool_blob(self) -> None:
        blob = json.dumps({"provider": "amap", "raw": {"lives": []}})
        payloads = [
            {"role": "user", "content": "上海天气"},
            {"role": "assistant", "content": blob},
            {"role": "user", "content": "那明天呢"},
        ]
        messages = _build_timeline_messages(payloads)
        joined = "\n".join(m.content for m in messages if isinstance(m.content, str))
        self.assertNotIn('"provider"', joined)
        self.assertNotIn('"lives"', joined)

    def test_build_timeline_keeps_narrative(self) -> None:
        payloads = [
            {"role": "user", "content": "上海天气"},
            {"role": "assistant", "content": "上海今天晴。"},
        ]
        messages = _build_timeline_messages(payloads)
        joined = "\n".join(m.content for m in messages if isinstance(m.content, str))
        self.assertIn("上海今天晴", joined)


class DirectReplyInjectsStateFactsTests(unittest.IsolatedAsyncioTestCase):
    """direct_reply 组装 Prompt 时，step_results 作为事实注入 System Prompt，
    而不是让模型去历史 messages 里考古。"""

    async def test_step_results_injected_into_system_prompt(self) -> None:
        import uuid as _uuid
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        from langchain_core.messages import AIMessage as _AIMessage
        from sqlalchemy.ext.asyncio import AsyncSession

        from agents.orchestrator_node import direct_reply_node
        from agents.state import AgentState

        captured: dict[str, str] = {}

        class _LLM:
            def bind_tools(self, tools):
                return self

        async def fake_astream_invoke(agent, messages, *, stream=True, **kwargs):
            captured["system"] = messages[0].content
            return _AIMessage(content="好的。")

        settings = MagicMock()
        settings.planner_timezone = "Asia/Shanghai"
        session = AsyncMock(spec=AsyncSession)
        state = AgentState(
            current_task={"intent": "根据天气写邮件", "route": "direct"},
            messages=[],
            user_id=_uuid.uuid4(),
            step_results={"step-1": "山西榆次明天晴，18°C"},
        )
        with patch("agents.orchestrator_node.astream_invoke", new=fake_astream_invoke):
            await direct_reply_node(
                state,
                llm=_LLM(),  # type: ignore[arg-type]
                runtime=SimpleNamespace(  # type: ignore[arg-type]
                    context={"db_session": session, "settings": settings}
                ),
            )
        self.assertIn("step-1", captured["system"])
        self.assertIn("山西榆次明天晴", captured["system"])


if __name__ == "__main__":
    unittest.main()
