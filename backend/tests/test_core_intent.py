"""core_intent：会话核心意图的确定性推导 + HITL 状态机兜底（回归守卫）。

真实 badcase：
  - 上一句"山西榆次明天天气"成功，下一句"那后天呢"被分类器当成闲聊 →
    模型回"没有可用的天气工具"。根因：每轮都是全新 thread，图记不住上一轮
    在哪条路由上。core_intent 用完整历史推导 anchor 路由，追问必须继承。
  - 存在待确认草案时用户说"同意/确认/拒绝"，绝不能被 LLM 重新分类成闲聊，
    否则写操作永远不会执行。
"""

import asyncio
import json
import unittest
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from agents.core_intent import (
    build_core_intent,
    city_of_text,
    hitl_reply_of,
    is_followup_utterance,
    render_core_intent,
    route_of_text,
)
from agents.orchestrator_node import orchestrate_node


def _u(text: str) -> dict:
    return {"role": "user", "content": text}


def _a(text: str) -> dict:
    return {"role": "assistant", "content": text}


class RouteAndCityExtractionTests(unittest.TestCase):
    def test_weather_text_anchors_life_route(self) -> None:
        self.assertEqual(route_of_text("山西榆次明天天气怎么样？"), "life")

    def test_email_text_anchors_email_route(self) -> None:
        self.assertEqual(route_of_text("帮我看看今天的邮件"), "email")

    def test_plain_greeting_has_no_route(self) -> None:
        self.assertIsNone(route_of_text("你好呀"))

    def test_city_extracted_from_weather_request(self) -> None:
        self.assertEqual(city_of_text("帮我查一下明天上海的天气"), "上海")

    def test_city_not_taken_from_filler(self) -> None:
        self.assertIsNone(city_of_text("今天天气怎么样"))


class FollowUpDetectionTests(unittest.TestCase):
    def test_bare_temporal_question_is_followup(self) -> None:
        self.assertTrue(is_followup_utterance("那后天呢？"))
        self.assertTrue(is_followup_utterance("明天呢"))

    def test_recheck_phrase_is_followup(self) -> None:
        self.assertTrue(is_followup_utterance("重查一下"))

    def test_full_new_request_is_not_followup(self) -> None:
        self.assertFalse(is_followup_utterance("帮我查一下北京的天气"))

    def test_bare_confirmation_is_followup(self) -> None:
        # 真实 badcase：助手问"需要我算到门口的路程吗"，用户回"需要"。
        for word in ("需要", "好的", "行", "可以", "对"):
            self.assertTrue(is_followup_utterance(word), word)


class CoreIntentDerivationTests(unittest.TestCase):
    def test_followup_inherits_anchor_route_and_city(self) -> None:
        payloads = [
            _u("山西榆次明天天气怎么样？"),
            _a("山西榆次明天晴，18°C。"),
            _u("那后天呢？"),
        ]
        core = build_core_intent(payloads)
        self.assertEqual(core["route"], "life")
        self.assertTrue(core["followup"])
        self.assertEqual(core["entities"]["destination"], "山西榆次")
        # 追问的 query 必须把 anchor 主题带回来，供下游 agent 直接执行。
        self.assertIn("山西榆次", core["query"])
        self.assertIn("后天", core["query"])

    def test_bare_confirmation_carries_assistant_offer(self) -> None:
        """裸确认"需要"必须把上一轮助手的提议（推荐地点 + 待答问题）带进 query。"""
        payloads = [
            _u("查看榆次今天的天气，并找一个适合短时专注的好去处"),
            _a("推荐晋中市图书馆（首选）和书包客自习室。需要我算到门口的路程时间吗？"),
            _u("需要"),
        ]
        core = build_core_intent(payloads)
        self.assertTrue(core["followup"])
        self.assertEqual(core["route"], "life")
        self.assertIn("上轮助手提议", core["query"])
        self.assertIn("晋中市图书馆", core["query"])
        self.assertTrue(core["assistant_offer"])
        # render 出来的核心上下文也要带这条提议，分类 LLM 才能判断"需要"在回答什么。
        rendered = render_core_intent(core)
        self.assertIn("晋中市图书馆", rendered)

    def test_fresh_topic_overrides_anchor(self) -> None:
        payloads = [
            _u("帮我查一下上海的天气"),
            _a("上海晴。"),
            _u("帮我看看今天的邮件"),
        ]
        core = build_core_intent(payloads)
        self.assertEqual(core["route"], "email")
        self.assertFalse(core["followup"])

    def test_plain_chat_without_anchor_is_direct(self) -> None:
        core = build_core_intent([_u("你好")])
        self.assertEqual(core["route"], "direct")
        self.assertFalse(core["followup"])

    def test_render_injects_topic_and_route(self) -> None:
        core = build_core_intent([
            _u("山西榆次天气"),
            _a("晴。"),
            _u("那后天呢"),
        ])
        block = render_core_intent(core)
        self.assertIn("山西榆次", block)
        self.assertIn("life", block)


class HitlReplyClassificationTests(unittest.TestCase):
    def test_approve_words(self) -> None:
        for text in ("同意", "确认", "可以", "执行", "确认执行"):
            self.assertEqual(hitl_reply_of(text), "approve", text)

    def test_reject_words(self) -> None:
        for text in ("拒绝", "取消", "算了", "不用了"):
            self.assertEqual(hitl_reply_of(text), "reject", text)

    def test_ordinary_text_is_not_hitl(self) -> None:
        self.assertIsNone(hitl_reply_of("今天天气不错"))


class _NeverCalledLLM:
    """如果 HITL 兜底生效，分类 LLM 根本不会被调用。"""

    async def ainvoke(self, messages, **kwargs):  # pragma: no cover
        raise AssertionError("HITL 回复不得交给 LLM 重新分类")


class HitlStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, latest: str, history=None):
        msgs = list(history or [])
        msgs.append(HumanMessage(content=latest))
        return {
            "messages": msgs,
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
        }

    async def test_approve_forces_direct_route_without_llm(self) -> None:
        state = self._state(
            "同意",
            [AIMessage(content="【待确认】发送邮件给老师，点击确认执行。")],
        )
        result = await orchestrate_node(state, _NeverCalledLLM())
        self.assertEqual(len(result["tasks"]), 1)
        task = result["tasks"][0]
        self.assertEqual(task["route"], "direct")
        self.assertEqual(task["hitl_reply"], "approve")

    async def test_reject_forces_direct_route_without_llm(self) -> None:
        state = self._state("拒绝", [AIMessage(content="【待确认】删除任务。")])
        result = await orchestrate_node(state, _NeverCalledLLM())
        self.assertEqual(result["tasks"][0]["hitl_reply"], "reject")
        self.assertEqual(result["tasks"][0]["route"], "direct")


class _FollowupSensitiveLLM:
    """模拟"只看到'那后天呢'就降级为 direct"的真实模型。"""

    async def ainvoke(self, messages, **kwargs):
        payload = {"tasks": [{"intent": "普通对话", "route": "direct"}]}
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


class RouterFollowupFallbackTests(unittest.IsolatedAsyncioTestCase):
    def test_followup_forces_inherited_route_in_code(self) -> None:
        """即使分类器降级成 direct，代码兜底也要把追问钉回 life 路由。"""
        state = {
            "messages": [
                HumanMessage(content="[10-28 09:00] user: 山西榆次明天天气怎么样？"),
                AIMessage(content="[10-28 09:00] assistant: 山西榆次明天晴，18°C。"),
                HumanMessage(content="[最新请求] user: 那后天呢？"),
            ],
            "settings": MagicMock(planner_timezone="Asia/Shanghai"),
        }
        result = asyncio.run(orchestrate_node(state, _FollowupSensitiveLLM()))
        routes = [t["route"] for t in result["tasks"]]
        self.assertEqual(routes, ["life"], f"追问必须继承 life 路由，实际 {routes}")


if __name__ == "__main__":
    unittest.main()
