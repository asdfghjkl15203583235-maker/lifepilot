"""模块三：地名实体的混合路由（正则置信度 → LLM 二次推理）。

真实 badcase：用户先说要「去上海出差」，随后问「周二天气怎么样」，
正则把「周二」当成地名 → 查了一个不存在的地点 / 回落到默认城市。
修复要点：
  - 时间词（周一…周日、明天、下周）绝不可能是地名；
  - 抽取结果必须带置信度，低置信度必须降级到 LLM；
  - 会话里已确认的目的地作为强先验注入 LLM。
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from agents.core_intent import (
    DESTINATION_CONFIDENCE_HIGH,
    build_core_intent,
    city_candidate_of_text,
    city_of_text,
)
from agents.entity_resolution import (
    needs_llm_resolution,
    resolve_core_intent_destination,
)
from agents.life_agent_node import _parse_city


def _u(text: str) -> dict:
    return {"role": "user", "content": text}


def _a(text: str) -> dict:
    return {"role": "assistant", "content": text}


class CityConfidenceTests(unittest.TestCase):
    def test_weekday_is_not_a_city(self) -> None:
        self.assertIsNone(city_of_text("周二天气怎么样"))

    def test_weekday_prefix_does_not_leak_into_city(self) -> None:
        self.assertEqual(city_of_text("下周二上海天气"), "上海")

    def test_activity_tail_is_stripped(self) -> None:
        self.assertEqual(city_of_text("去上海玩两天"), "上海")

    def test_ambiguous_candidate_reports_low_confidence(self) -> None:
        candidate, confidence = city_candidate_of_text("周二天气怎么样")
        self.assertIsNone(candidate)
        self.assertLess(confidence, DESTINATION_CONFIDENCE_HIGH)

    def test_plain_chat_has_zero_confidence(self) -> None:
        candidate, confidence = city_candidate_of_text("你好呀")
        self.assertIsNone(candidate)
        self.assertEqual(confidence, 0.0)

    def test_clean_city_is_high_confidence(self) -> None:
        candidate, confidence = city_candidate_of_text("帮我查一下明天上海的天气")
        self.assertEqual(candidate, "上海")
        self.assertGreaterEqual(confidence, DESTINATION_CONFIDENCE_HIGH)


class CoreIntentHistoryPriorTests(unittest.TestCase):
    def test_history_destination_is_used_as_prior(self) -> None:
        core = build_core_intent([
            _u("我要去上海出差"),
            _a("好的，已记下。"),
            _u("周二天气怎么样"),
        ])
        self.assertEqual(core["entities"]["destination"], "上海")
        self.assertIn("上海", core["history_destinations"])
        # 继承来的先验不是高置信度抽取值，仍应允许 LLM 复核。
        self.assertLess(core["destination_confidence"], DESTINATION_CONFIDENCE_HIGH)
        self.assertTrue(needs_llm_resolution(core, core["latest_utterance"]))

    def test_high_confidence_city_skips_llm(self) -> None:
        core = build_core_intent([_u("去上海玩两天")])
        self.assertEqual(core["entities"]["destination"], "上海")
        self.assertFalse(needs_llm_resolution(core, core["latest_utterance"]))


class _RecordingLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.messages: list = []

    async def ainvoke(self, messages, **kwargs):
        self.messages = messages
        return AIMessage(content=json.dumps(self.payload, ensure_ascii=False))


class LlmResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_turn_asks_llm_with_history_prior(self) -> None:
        core = build_core_intent([
            _u("我要去上海出差"),
            _a("好的。"),
            _u("周二天气怎么样"),
        ])
        fake = _RecordingLLM(
            {"destination": "上海", "confidence": 0.9, "reason": "周二只是时间，沿用上海"}
        )
        with patch("agents.entity_resolution.chat_llm", return_value=fake):
            resolved = await resolve_core_intent_destination(
                MagicMock(openrouter_api_key="test-key"), core
            )

        self.assertTrue(fake.messages, "低置信度必须触发 LLM 二次推理")
        self.assertIn("上海", fake.messages[1].content)
        self.assertEqual(resolved["entities"]["destination"], "上海")
        self.assertEqual(resolved["destination_confidence"], 0.9)

    async def test_time_word_alone_is_resolved_to_prior_city(self) -> None:
        core = build_core_intent([_u("周二天气怎么样")])
        self.assertIsNone(core["entities"].get("destination"))
        fake = _RecordingLLM({"destination": "上海", "confidence": 0.9, "reason": "沿用上下文"})
        with patch("agents.entity_resolution.chat_llm", return_value=fake):
            resolved = await resolve_core_intent_destination(
                MagicMock(openrouter_api_key="test-key"), core
            )
        self.assertEqual(resolved["entities"]["destination"], "上海")

    async def test_low_confidence_new_city_does_not_override_prior(self) -> None:
        core = build_core_intent([
            _u("我要去上海出差"),
            _a("好的。"),
            _u("周二天气怎么样"),
        ])
        fake = _RecordingLLM({"destination": "火星", "confidence": 0.2, "reason": "不确定"})
        with patch("agents.entity_resolution.chat_llm", return_value=fake):
            resolved = await resolve_core_intent_destination(
                MagicMock(openrouter_api_key="test-key"), core
            )
        self.assertEqual(resolved["entities"]["destination"], "上海")


class LifeAgentCityParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_weekday_is_not_parsed_as_city(self) -> None:
        self.assertIsNone(await _parse_city("周二天气怎么样"))
        self.assertIsNone(await _parse_city("查询周二的天气"))

    async def test_real_city_still_parsed(self) -> None:
        self.assertEqual(await _parse_city("上海明天天气"), "上海")


if __name__ == "__main__":
    unittest.main()
