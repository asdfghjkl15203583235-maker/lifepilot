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

from agents.city_dictionary import extract_city_by_dictionary
from agents.core_intent import (
    DESTINATION_CONFIDENCE_HIGH,
    DESTINATION_SOURCE_INHERITED,
    build_core_intent,
    city_candidate_of_text,
    city_of_text,
)
from agents.destination_resolver import extract_destination_candidate
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

    def test_high_confidence_city_still_goes_llm(self) -> None:
        # 涉及地点的请求即便正则给了高置信度，也必须经 LLM 复核——
        # 「查看青岛」会被抽成「看青岛」这类污染，高置信度不代表正确。
        core = build_core_intent([_u("去上海玩两天")])
        self.assertEqual(core["entities"]["destination"], "上海")
        self.assertTrue(needs_llm_resolution(core, core["latest_utterance"]))


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


class UnifiedDestinationResolverTests(unittest.TestCase):
    """统一入口 `extract_destination_candidate` 覆盖三类提取的历史 badcase。"""

    def test_view_prefix_is_not_polluted_into_city(self) -> None:
        # 「查看青岛」不得被抽成「看青岛」——真实 badcase 直接导致地点查询失败。
        candidate, confidence = extract_destination_candidate(
            "查看青岛今天的天气，并找一个适合短时专注的好去处"
        )
        self.assertEqual(candidate, "青岛")
        self.assertGreaterEqual(confidence, DESTINATION_CONFIDENCE_HIGH)

    def test_planner_rewritten_weather_intents(self) -> None:
        cases = {
            "查询上海近两天的天气情况": "上海",
            "根据上海天气制定两天出行计划": "上海",
            "上海最近一周的天气": "上海",
        }
        for text, expected in cases.items():
            candidate, _ = extract_destination_candidate(text)
            self.assertEqual(candidate, expected, text)

    def test_travel_near_duration_words(self) -> None:
        cases = {"北京周末两日游": "北京", "帮我规划上海两日游": "上海"}
        for text, expected in cases.items():
            candidate, _ = extract_destination_candidate(text)
            self.assertEqual(candidate, expected, text)

    def test_time_word_still_not_a_city(self) -> None:
        candidate, confidence = extract_destination_candidate("周二天气怎么样")
        self.assertIsNone(candidate)
        self.assertLess(confidence, DESTINATION_CONFIDENCE_HIGH)

    def test_plain_chat_has_no_destination(self) -> None:
        candidate, confidence = extract_destination_candidate("你好呀")
        self.assertIsNone(candidate)
        self.assertEqual(confidence, 0.0)


class CityDictionaryAndCachingTests(unittest.TestCase):
    """词典识别闲聊地名 + 跨轮指代继承 + 同城缓存跳过 LLM。"""

    def test_dictionary_extracts_casual_city(self) -> None:
        # 正则认不出「北京挺好的」这种随意提到，词典必须补上。
        self.assertEqual(extract_city_by_dictionary("北京挺好的，还挺吸引人"), "北京")
        self.assertIsNone(extract_city_by_dictionary("火星挺好的"))

    def test_followup_inherits_casual_city(self) -> None:
        core = build_core_intent([
            _u("北京挺好的，还挺吸引人"),
            _a("确实，北京值得一去。"),
            _u("我还挺想去的，你帮我规划一下"),
        ])
        self.assertEqual(core["entities"]["destination"], "北京")
        self.assertEqual(core["destination_source"], DESTINATION_SOURCE_INHERITED)
        # 明确指代继承 → 跳过 LLM（同城缓存命中）。
        self.assertFalse(needs_llm_resolution(core, core["latest_utterance"]))

    def test_ambiguous_time_word_still_asks_llm(self) -> None:
        core = build_core_intent([
            _u("我要去上海玩"),
            _a("好的。"),
            _u("周二天气怎么样"),
        ])
        self.assertEqual(core["entities"]["destination"], "上海")
        # 时间词指代不明确，仍需 LLM 判断继承哪个城市。
        self.assertTrue(needs_llm_resolution(core, core["latest_utterance"]))


if __name__ == "__main__":
    unittest.main()
