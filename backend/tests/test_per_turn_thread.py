"""每轮必须用独立的 checkpointer thread。

线上故障：多轮对话里第 2、3 轮会复述上一轮的邮件/天气。根因不是提示词，
而是 `thread_id` 复用了 `client_conversation_id`，checkpointer 于是把上一轮
终态恢复进本轮；`agent_results` / `pending_actions` 的 reducer 是 operator.add，
恢复即累加，`merge_results_node` 便把上一轮的邮件结果并进了"1+1"的回复。
"""

import unittest
import uuid

from services.agent_runner import _fresh_thread_id


class PerTurnThreadIdTests(unittest.TestCase):
    def test_same_conversation_gets_distinct_threads(self) -> None:
        conv = str(uuid.uuid4())
        first = _fresh_thread_id(conv)
        second = _fresh_thread_id(conv)
        self.assertNotEqual(first, second)
        # 保留会话 id 便于在 tracing 里按会话检索。
        self.assertTrue(first.startswith(conv))
        self.assertTrue(second.startswith(conv))

    def test_missing_conversation_id_still_unique(self) -> None:
        self.assertNotEqual(_fresh_thread_id(None), _fresh_thread_id(None))

    def test_thread_id_never_equals_conversation_id(self) -> None:
        conv = "conv-initial"
        self.assertNotEqual(_fresh_thread_id(conv), conv)


if __name__ == "__main__":
    unittest.main()
