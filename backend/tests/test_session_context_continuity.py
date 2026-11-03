"""会话上下文延续：历史里的持续性事实不得被窗口裁剪掉。

真实 badcase：同一会话里用户说"我要去上海"，过了两轮问"推荐穿什么衣服"，
AI 回答"要看你去哪儿，比如上海、北京…"。

根因是上下文隔离死命令写得过于绝对（历史一律视为"已执行完毕的旧任务"），
加上滑动窗口把早期消息裁掉，目的地这类前提事实就消失了。修复：
1. 隔离规则区分"执行目标"与"会话上下文"；
2. `_build_timeline_messages` 单独注入确定性的上下文事实块，事实从**全部**
   历史提取，不受窗口裁剪影响。
"""

import unittest

from langchain_core.messages import SystemMessage

from services.agent_runner import (
    CONTEXT_WINDOW_ROUNDS,
    _build_timeline_messages,
    _conversation_context_facts,
)


def _conversation_with_destination_gap() -> list[dict]:
    """说"我要去上海"之后，间隔超过一个上下文窗口再提问。"""
    payloads = [{"role": "user", "content": "我下周要去上海出差"}]
    payloads.append({"role": "assistant", "content": "好的，上海出差，需要我帮你安排行程吗？"})
    for index in range(CONTEXT_WINDOW_ROUNDS + 1):
        payloads.append({"role": "user", "content": f"闲聊第{index}轮"})
        payloads.append({"role": "assistant", "content": f"闲聊回答{index}"})
    payloads.append({"role": "user", "content": "推荐穿什么衣服"})
    return payloads


class ConversationContextFactTests(unittest.TestCase):
    def test_extracts_destination_from_user_statement(self) -> None:
        facts = _conversation_context_facts(
            [{"role": "user", "content": "我下周要去上海出差"}]
        )
        self.assertTrue(facts)
        self.assertIn("上海", facts[0])

    def test_ignores_non_place_objects(self) -> None:
        """「我要去吃饭」里的"吃饭"不是目的地，不能当成上下文事实。"""
        facts = _conversation_context_facts(
            [{"role": "user", "content": "我要去吃饭了"}]
        )
        self.assertEqual(facts, [])

    def test_ignores_assistant_statements(self) -> None:
        facts = _conversation_context_facts(
            [{"role": "assistant", "content": "你要去上海的话可以坐高铁"}]
        )
        self.assertEqual(facts, [])

    def test_destination_survives_window_truncation(self) -> None:
        """目的地被裁出最近窗口后，仍必须以独立上下文块出现在消息里。"""
        messages = _build_timeline_messages(_conversation_with_destination_gap())
        blocks = [m.content for m in messages if isinstance(m, SystemMessage)]
        context_blocks = [b for b in blocks if "本会话已确立的上下文" in b]
        self.assertEqual(len(context_blocks), 1)
        self.assertIn("上海", context_blocks[0])

    def test_context_block_precedes_conversation_messages(self) -> None:
        messages = _build_timeline_messages(_conversation_with_destination_gap())
        block_index = next(
            i
            for i, m in enumerate(messages)
            if isinstance(m, SystemMessage) and "本会话已确立的上下文" in m.content
        )
        # 上下文块必须排在所有对话消息之前，作为前提而不是插话。
        self.assertTrue(
            all(
                isinstance(m, SystemMessage) or i > block_index
                for i, m in enumerate(messages)
            )
        )

    def test_latest_request_label_preserved(self) -> None:
        messages = _build_timeline_messages(_conversation_with_destination_gap())
        self.assertTrue(messages[-1].content.startswith("[最新请求] user: 推荐穿什么衣服"))

    def test_no_context_block_for_plain_chat(self) -> None:
        messages = _build_timeline_messages(
            [
                {"role": "user", "content": "今天天气怎么样"},
                {"role": "user", "content": "那明天呢"},
            ]
        )
        self.assertFalse(
            any(
                isinstance(m, SystemMessage) and "本会话已确立的上下文" in m.content
                for m in messages
            )
        )


class IsolationRuleWordingTests(unittest.TestCase):
    def test_rule_keeps_both_isolation_and_continuity(self) -> None:
        from agents.common import CONTEXT_ISOLATION_RULE

        self.assertIn("执行隔离", CONTEXT_ISOLATION_RULE)
        self.assertIn("上下文延续", CONTEXT_ISOLATION_RULE)
        self.assertIn("上海", CONTEXT_ISOLATION_RULE)


if __name__ == "__main__":
    unittest.main()
