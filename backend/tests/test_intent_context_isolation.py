"""意图分类不得被历史消息污染（回归守卫）。

真实场景：用户上一轮让助手写邮件草稿，这一轮只是问"晋中学院附近的美食"，
分类器却把整段历史（含邮件草案）当输入，返回 route="email"，于是已完成的
邮件草案被重新弹出来，前几轮消息也被缝合进回答。

修复：分类只喂最新一条用户消息，历史交给对话节点；历史里的写操作视为已完成。
"""

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agents.orchestrator_node import orchestrate_node

_EMAIL_DRAFT_HISTORY = [
    HumanMessage(content="帮我给王老师写一封请假邮件"),
    AIMessage(content="邮件草稿已准备好（等待你确认后才会真正发送）：收件人 wang@school.edu"),
    HumanMessage(content="确认执行"),
    AIMessage(content="邮件已提交发送。"),
]


class _HistorySensitiveLLM:
    """模拟真实模型：看到对话历史里的邮件草案就继续归为 email。"""

    def __init__(self) -> None:
        self.seen: list = []

    async def ainvoke(self, messages, **kwargs):
        self.seen = list(messages)
        # 只看对话消息，忽略 system prompt（真实模型也是被"历史对话"带偏的）。
        conversation = [m for m in messages if not isinstance(m, SystemMessage)]
        joined = "\n".join(str(getattr(m, "content", "")) for m in conversation)
        if "请假邮件" in joined:
            payload = {"tasks": [{"intent": "继续发送请假邮件", "route": "email"}]}
        elif "美食" in joined:
            payload = {"tasks": [{"intent": "查晋中学院附近的美食", "route": "life"}]}
        else:
            payload = {"tasks": [{"intent": "普通对话", "route": "direct"}]}
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


class IntentContextIsolationTests(unittest.TestCase):
    def _run(self, history, latest):
        llm = _HistorySensitiveLLM()
        state = {
            "messages": [*history, HumanMessage(content=latest)],
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
        }
        result = asyncio.run(orchestrate_node(state, llm))
        return llm, result

    def test_classifier_only_sees_latest_user_message(self):
        llm, _ = self._run(_EMAIL_DRAFT_HISTORY, "晋中学院附近有什么美食")
        self.assertEqual(len(llm.seen), 2, "分类只应有 system + 最新用户消息")
        self.assertIsInstance(llm.seen[0], SystemMessage)
        self.assertIsInstance(llm.seen[1], HumanMessage)
        self.assertEqual(llm.seen[1].content, "晋中学院附近有什么美食")

    def test_chit_chat_after_email_draft_does_not_route_to_email(self):
        _, result = self._run(_EMAIL_DRAFT_HISTORY, "晋中学院附近有什么美食")
        routes = [task["route"] for task in result["tasks"]]
        self.assertNotIn("email", routes, "历史邮件草案不得把新问题重新路由到 email")
        self.assertEqual(routes, ["life"])

    def test_write_intent_in_latest_message_still_routes(self):
        # 隔离只针对历史：最新消息自己表达的写意图必须照常识别。
        llm = _HistorySensitiveLLM()
        state = {
            "messages": [HumanMessage(content="帮我写一封请假邮件给王老师")],
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
        }
        result = asyncio.run(orchestrate_node(state, llm))
        self.assertEqual([t["route"] for t in result["tasks"]], ["email"])


if __name__ == "__main__":
    unittest.main()
