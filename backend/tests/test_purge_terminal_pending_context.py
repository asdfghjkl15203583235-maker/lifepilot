"""模块五：已终结的 PendingAction 必须从发给大模型的上下文中彻底剔除。

真实 badcase：用户拒绝了"发送邮件"草案，随后闲聊时模型却复述"你刚才拒绝了一个
操作/要不要重新确认那封邮件"——因为被 Rejected 的草案摘要还留在 messages 历史里。

修复：组装 messages 时，凡命中已终结（Approved/Rejected/Expired/Failed）
PendingAction 标题/摘要的历史消息一律剔除；用户最新一条消息永不剔除。
"""

import unittest

from services.agent_runner import purge_terminal_pending_action_context


def _u(text: str) -> dict:
    return {"role": "user", "content": text}


def _a(text: str) -> dict:
    return {"role": "assistant", "content": text}


class PurgeTerminalPendingActionTests(unittest.TestCase):
    def test_rejected_draft_recap_removed_from_history(self) -> None:
        payloads = [
            _u("帮我给老师发封邮件"),
            _a("邮件草稿已准备好：发送给 wang@school.edu，点击确认后执行。"),
            _u("拒绝"),
            _a("已取消「发送邮件给老师」。"),
            _u("今天天气怎么样"),
        ]
        terminal = [{
            "title": "发送邮件给老师",
            "summary": "发送给 wang@school.edu",
            "status": "rejected",
        }]
        cleaned = purge_terminal_pending_action_context(payloads, terminal)
        joined = "\n".join(str(m.get("content")) for m in cleaned)
        self.assertNotIn("发送邮件给老师", joined)
        self.assertNotIn("wang@school.edu", joined)
        # 最新用户消息必须保留。
        self.assertIn("今天天气怎么样", joined)

    def test_latest_user_message_never_removed(self) -> None:
        payloads = [_u("确认删除任务「买牛奶」")]
        terminal = [{"title": "删除任务「买牛奶」", "summary": None, "status": "rejected"}]
        cleaned = purge_terminal_pending_action_context(payloads, terminal)
        self.assertEqual(cleaned, payloads)

    def test_pending_draft_is_kept(self) -> None:
        """未终结（pending）的草案不能被剔除——它仍需展示/确认。"""
        payloads = [
            _u("帮我发邮件"),
            _a("邮件草稿已准备好：发送给 wang@school.edu。"),
        ]
        # terminal_actions 只含已终结项；pending 的不在列表里。
        cleaned = purge_terminal_pending_action_context(payloads, [])
        self.assertEqual(len(cleaned), 2)

    def test_no_terminal_actions_returns_copy(self) -> None:
        payloads = [_u("你好"), _a("你好，有什么可以帮你？")]
        cleaned = purge_terminal_pending_action_context(payloads, [])
        self.assertEqual(cleaned, payloads)
        self.assertIsNot(cleaned, payloads)


if __name__ == "__main__":
    unittest.main()
