"""Regression tests for memory extraction chain: category coverage + exact-text dedup."""

import asyncio
import json
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import MemoryCategory
from services.post_chat_memory_extraction import (
    MEMORY_EXTRACTION_SYSTEM_PROMPT,
    _extract_json_array,
    run_post_chat_memory_extraction,
)
from services.memory_service import add_memory


class MemoryExtractionCategoryCoverageTests(unittest.IsolatedAsyncioTestCase):
    def test_extraction_prompt_covers_fact_and_rule_categories(self) -> None:
        """抽取 prompt 必须覆盖 MemoryCategory 的全部类别，否则 LLM 不会产出
        rule/fact，planner 规则注入会一直为空。"""
        for category in MemoryCategory:
            self.assertIn(
                f"- {category.value}:",
                MEMORY_EXTRACTION_SYSTEM_PROMPT,
                f"抽取 prompt 缺少类别 {category.value}",
            )

    def test_extraction_prompt_forces_pure_chinese(self) -> None:
        """死命令必须强制中文输出并禁止中英混杂。"""
        self.assertIn("simplified Chinese", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("简体中文", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("STRICTLY FORBIDDEN", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("中英混杂", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("hard rule", MEMORY_EXTRACTION_SYSTEM_PROMPT)

    def test_extraction_prompt_forbids_one_off_events_and_tasks(self) -> None:
        """一次性流水账（改简历、拿奶茶、参加腾讯会议）绝不能进长期记忆。"""
        prompt = MEMORY_EXTRACTION_SYSTEM_PROMPT
        self.assertIn("FORBIDDEN TO SAVE", prompt)
        self.assertIn("ONE-OFF TASKS", prompt)
        self.assertIn("取快递", prompt)
        self.assertIn("参加腾讯会议", prompt)
        self.assertIn("prefer NOT saving", prompt)

    def test_extract_json_array_handles_fences(self) -> None:
        raw = '```json\n[{"text": "a", "category": "fact"}]\n```'
        parsed = _extract_json_array(raw)
        self.assertEqual(parsed, [{"text": "a", "category": "fact"}])

    async def _make_settings(self) -> MagicMock:
        settings = MagicMock()
        settings.database_url = "postgresql://x"
        settings.voyageai_api_key = "k"
        settings.voyageai_model = "voyage-3"
        return settings

    async def test_rule_fact_from_chat_are_saved(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        user_id = uuid.uuid4()
        settings = await self._make_settings()

        async def fake_add_memory(session_, settings_, user_id_, text, category):
            row = MagicMock()
            row.id = uuid.uuid4()
            return row

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(side_effect=fake_add_memory),
        ) as mock_add:
            llm = MagicMock()
            llm.ainvoke = AsyncMock(
                return_value=MagicMock(
                    content=json.dumps([
                        {"text": "以后每次开会提前10分钟提醒我", "category": "rule"},
                        {"text": "用户在杭州工作", "category": "fact"},
                    ]),
                ),
            )
            await run_post_chat_memory_extraction(
                session,
                settings,
                user_id,
                [{"role": "user", "content": "提醒我每次开会提前10分钟"}],
                "已记住。",
                extraction_llm=llm,
            )

        self.assertEqual(mock_add.call_count, 2)
        categories = {call.args[4] for call in mock_add.call_args_list}
        self.assertEqual(categories, {"rule", "fact"})

    def test_invalid_category_rejected(self) -> None:
        from agents.tools.memory_tool import add_memory_tool

        valid = {c.value for c in MemoryCategory}
        self.assertIn("fact", valid)
        self.assertIn("rule", valid)


class MemoryDecisionMechanismTests(unittest.IsolatedAsyncioTestCase):
    """Mem0 风格增量决策：ADD/UPDATE/DELETE/NOOP + 纯中文死命令。"""

    async def _make_settings(self) -> MagicMock:
        settings = MagicMock()
        settings.database_url = "postgresql://x"
        settings.voyageai_api_key = "k"
        settings.voyageai_model = "voyage-3"
        return settings

    async def _decision_llm(self, decision_json: str) -> MagicMock:
        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content=decision_json))
        return llm

    async def test_parse_decision_accepts_actions(self) -> None:
        from services.post_chat_memory_extraction import _parse_decision

        self.assertEqual(_parse_decision('{"action": "ADD", "target_memory_id": null}'),
                         {"action": "ADD", "target_memory_id": None})
        uid = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"
        parsed = _parse_decision(f'{{"action": "UPDATE", "target_memory_id": "{uid}"}}')
        self.assertEqual(parsed["action"], "UPDATE")
        self.assertEqual(parsed["target_memory_id"], uid)
        self.assertIsNone(_parse_decision("not json"))
        self.assertIsNone(_parse_decision('{"action": "BOGUS"}'))

    async def test_decide_add_when_no_similar(self) -> None:
        from services.post_chat_memory_extraction import _decide_memory_action

        session = AsyncMock(spec=AsyncSession)
        settings = await self._make_settings()
        user_id = uuid.uuid4()
        decision = await _decide_memory_action(
            session, settings, user_id, "新事实", [], decision_llm=MagicMock(),
        )
        self.assertEqual(decision["action"], "ADD")

    async def test_decision_llm_updates_old_memory(self) -> None:
        """UPDATE 决策：软删旧 + 插入新。"""
        old_id = uuid.uuid4()
        from services.post_chat_memory_extraction import run_post_chat_memory_extraction

        session = AsyncMock(spec=AsyncSession)
        settings = await self._make_settings()
        user_id = uuid.uuid4()

        decision_llm = await self._decision_llm(
            json.dumps({"action": "UPDATE", "target_memory_id": str(old_id)}),
        )

        calls: list[tuple] = []

        async def fake_add(session_, settings_, user_id_, text, category):
            row = MagicMock()
            row.id = uuid.uuid4()
            calls.append(("add", text, category))
            return row

        async def fake_find(session_, settings_, user_id_, query, limit=3):
            return [(old_id, "我以前喜欢拿铁")]

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(side_effect=fake_add),
        ), patch(
            "services.memory_service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),
        ), patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(side_effect=fake_find),
        ), patch(
            "services.post_chat_memory_extraction.soft_delete_memory",
            new=AsyncMock(return_value=True),
        ) as mock_soft_delete:
            llm = MagicMock()
            llm.ainvoke = AsyncMock(return_value=MagicMock(
                content=json.dumps([{"text": "我现在不喜欢拿铁了", "category": "preference"}]),
            ))
            await run_post_chat_memory_extraction(
                session, settings, user_id,
                [{"role": "user", "content": "我不喜欢拿铁了"}],
                "已记住。",
                extraction_llm=llm,
                decision_llm=decision_llm,
            )

        mock_soft_delete.assert_awaited_once_with(session, user_id, old_id)
        self.assertEqual(calls, [("add", "我现在不喜欢拿铁了", "preference")])

    async def test_decision_llm_deletes_old_memory_only(self) -> None:
        """DELETE 决策：仅软删旧，不插入。"""
        old_id = uuid.uuid4()
        from services.post_chat_memory_extraction import run_post_chat_memory_extraction

        session = AsyncMock(spec=AsyncSession)
        settings = await self._make_settings()
        user_id = uuid.uuid4()

        decision_llm = await self._decision_llm(
            json.dumps({"action": "DELETE", "target_memory_id": str(old_id)}),
        )

        async def fake_find(session_, settings_, user_id_, query, limit=3):
            return [(old_id, "旧记忆")]

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add, patch(
            "services.memory_service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),
        ), patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(side_effect=fake_find),
        ), patch(
            "services.post_chat_memory_extraction.soft_delete_memory",
            new=AsyncMock(return_value=True),
        ) as mock_soft_delete:
            llm = MagicMock()
            llm.ainvoke = AsyncMock(return_value=MagicMock(
                content=json.dumps([{"text": "我不再喝咖啡了", "category": "preference"}]),
            ))
            await run_post_chat_memory_extraction(
                session, settings, user_id,
                [{"role": "user", "content": "我已经不喝咖啡了"}],
                "已记住。",
                extraction_llm=llm,
                decision_llm=decision_llm,
            )

        mock_soft_delete.assert_awaited_once_with(session, user_id, old_id)
        mock_add.assert_not_awaited()

    async def test_decision_llm_noop_skips_insert(self) -> None:
        """NOOP 决策：完全重复，跳过。"""
        from services.post_chat_memory_extraction import run_post_chat_memory_extraction

        session = AsyncMock(spec=AsyncSession)
        settings = await self._make_settings()
        user_id = uuid.uuid4()

        decision_llm = await self._decision_llm(
            json.dumps({"action": "NOOP", "target_memory_id": None}),
        )

        async def fake_find(session_, settings_, user_id_, query, limit=3):
            return [(uuid.uuid4(), "以前已保存的事实")]

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add, patch(
            "services.memory_service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),
        ), patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(side_effect=fake_find),
        ), patch(
            "services.post_chat_memory_extraction.soft_delete_memory",
            new=AsyncMock(return_value=True),
        ) as mock_soft_delete:
            llm = MagicMock()
            llm.ainvoke = AsyncMock(return_value=MagicMock(
                content=json.dumps([{"text": "重复的老事实", "category": "fact"}]),
            ))
            await run_post_chat_memory_extraction(
                session, settings, user_id,
                [{"role": "user", "content": "再提一次"}],
                "已记住。",
                extraction_llm=llm,
                decision_llm=decision_llm,
            )

        mock_add.assert_not_awaited()
        mock_soft_delete.assert_not_awaited()

    async def test_decision_llm_failure_falls_back_to_add(self) -> None:
        """决策 LLM 返回垃圾 → fail-open 用 ADD 保存，不丢事实。"""
        from services.post_chat_memory_extraction import run_post_chat_memory_extraction

        session = AsyncMock(spec=AsyncSession)
        settings = await self._make_settings()
        user_id = uuid.uuid4()

        # 决策 LLM 返回非 JSON；extraction LLM 正常。
        decision_llm = MagicMock()
        decision_llm.ainvoke = AsyncMock(return_value=MagicMock(content="not json at all"))

        async def fake_find(session_, settings_, user_id_, query, limit=3):
            return [(uuid.uuid4(), "旧记忆")]

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add, patch(
            "services.memory_service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),
        ), patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(side_effect=fake_find),
        ):
            llm = MagicMock()
            llm.ainvoke = AsyncMock(return_value=MagicMock(
                content=json.dumps([{"text": "不会丢失的事实", "category": "fact"}]),
            ))
            await run_post_chat_memory_extraction(
                session, settings, user_id,
                [{"role": "user", "content": "记住这句"}],
                "已记住。",
                extraction_llm=llm,
                decision_llm=decision_llm,
            )

        mock_add.assert_awaited_once()


class MemoryLlmTransientRetryTests(unittest.IsolatedAsyncioTestCase):
    """OpenRouter 402/429/5xx 是瞬时并发预算限制，提取/决策必须重试而不是丢事实。"""

    def test_classifies_transient_statuses(self) -> None:
        from services.post_chat_memory_extraction import _is_transient_llm_error

        class Err(Exception):
            def __init__(self, status_code: int) -> None:
                super().__init__("boom")
                self.status_code = status_code

        for status in (402, 429, 500, 502, 503, 504):
            self.assertTrue(_is_transient_llm_error(Err(status)), status)
        for status in (400, 401, 403, 404, 422):
            self.assertFalse(_is_transient_llm_error(Err(status)), status)
        self.assertFalse(_is_transient_llm_error(ValueError("no status")))

    async def test_extraction_retries_after_402(self) -> None:
        import json as jsonlib

        from services.post_chat_memory_extraction import run_post_chat_memory_extraction

        class BudgetError(Exception):
            status_code = 402

        session = AsyncMock(spec=AsyncSession)
        settings = MagicMock()
        settings.database_url = "postgresql://x"
        settings.voyageai_api_key = "k"
        settings.voyageai_model = "voyage-3"
        user_id = uuid.uuid4()

        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=[
            BudgetError("in_flight_budget_exhausted"),
            MagicMock(content=jsonlib.dumps([
                {"text": "用户喜欢喝美式咖啡", "category": "preference"},
            ])),
        ])

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add, patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(return_value=[]),
        ), patch(
            "services.post_chat_memory_extraction.asyncio.sleep",
            new=AsyncMock(),
        ) as mock_sleep:
            await run_post_chat_memory_extraction(
                session, settings, user_id,
                [{"role": "user", "content": "我喜欢喝美式"}],
                "好的。",
                extraction_llm=llm,
            )

        self.assertEqual(llm.ainvoke.await_count, 2)
        mock_sleep.assert_awaited_once()
        mock_add.assert_awaited_once()


class DeterministicPreferenceReplacementTests(unittest.IsolatedAsyncioTestCase):
    """显式偏好替换必须确定性软删旧记忆，不能只赌决策 LLM 返回 UPDATE。"""

    def test_picks_single_same_domain_preference(self) -> None:
        from services.post_chat_memory_extraction import _deterministic_supersede_target

        old_id = uuid.uuid4()
        target = _deterministic_supersede_target(
            "用户其实更喜欢喝美式咖啡",
            [(old_id, "用户喜欢喝拿铁")],
        )
        self.assertEqual(target, old_id)

    def test_ignores_unrelated_domain(self) -> None:
        from services.post_chat_memory_extraction import _deterministic_supersede_target

        target = _deterministic_supersede_target(
            "用户其实更喜欢喝美式咖啡",
            [(uuid.uuid4(), "用户喜欢跑步")],
        )
        self.assertIsNone(target)

    def test_ambiguous_candidates_are_left_to_llm(self) -> None:
        from services.post_chat_memory_extraction import _deterministic_supersede_target

        target = _deterministic_supersede_target(
            "用户更喜欢喝茶",
            [(uuid.uuid4(), "用户喜欢喝拿铁"), (uuid.uuid4(), "用户喜欢喝美式")],
        )
        self.assertIsNone(target)

    async def test_add_decision_is_overridden_for_replacement(self) -> None:
        from services.post_chat_memory_extraction import run_post_chat_memory_extraction

        session = AsyncMock(spec=AsyncSession)
        settings = MagicMock()
        settings.database_url = "postgresql://x"
        settings.voyageai_api_key = "k"
        settings.voyageai_model = "voyage-3"
        user_id = uuid.uuid4()
        old_id = uuid.uuid4()

        decision_llm = MagicMock()
        decision_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content=json.dumps({"action": "ADD", "target_memory_id": None})),
        )

        async def fake_find(session_, settings_, user_id_, query, limit=3):
            return [(old_id, "用户喜欢喝拿铁")]

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add, patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(side_effect=fake_find),
        ), patch(
            "services.post_chat_memory_extraction.soft_delete_memory",
            new=AsyncMock(return_value=True),
        ) as mock_soft_delete:
            llm = MagicMock()
            llm.ainvoke = AsyncMock(return_value=MagicMock(
                content=json.dumps([
                    {"text": "用户其实更喜欢喝美式咖啡", "category": "preference"},
                ]),
            ))
            await run_post_chat_memory_extraction(
                session, settings, user_id,
                [{"role": "user", "content": "我其实更喜欢喝美式"}],
                "好的。",
                extraction_llm=llm,
                decision_llm=decision_llm,
            )

        mock_soft_delete.assert_awaited_once_with(session, user_id, old_id)
        mock_add.assert_awaited_once()

    def test_explicit_preference_without_replacement_word_supersedes(self) -> None:
        """回归："我最喜欢喝拿铁"（没说"改/换"）也要压过旧的口味记忆。"""
        from services.post_chat_memory_extraction import _deterministic_supersede_target

        old_id = uuid.uuid4()
        target = _deterministic_supersede_target(
            "用户最喜欢喝拿铁",
            [(old_id, "用户喜欢喝美式咖啡")],
        )
        self.assertEqual(target, old_id)

    def test_detects_explicit_preference_sentences(self) -> None:
        from services.post_chat_memory_extraction import (
            _explicit_preference_sentences,
            _missing_explicit_preferences,
        )

        payloads = [
            {"role": "user", "content": "我最喜欢喝拿铁，不过现在更爱美式。"},
            {"role": "assistant", "content": "好，我记下了。"},
        ]
        sentences = _explicit_preference_sentences(payloads)
        self.assertEqual(len(sentences), 1)

        # 只抽出"更爱美式"时，"最喜欢喝拿铁"必须被补回来。
        missing = _missing_explicit_preferences(
            [{"text": "用户更爱美式", "category": "preference"}],
            payloads,
        )
        self.assertEqual([item["text"] for item in missing], ["我最喜欢喝拿铁"])
        self.assertTrue(all(item["category"] == "preference" for item in missing))

    def test_fully_covered_preference_is_not_duplicated(self) -> None:
        from services.post_chat_memory_extraction import _missing_explicit_preferences

        payloads = [{"role": "user", "content": "我最喜欢喝拿铁。"}]
        missing = _missing_explicit_preferences(
            [{"text": "用户最喜欢喝拿铁", "category": "preference"}],
            payloads,
        )
        self.assertEqual(missing, [])

    def test_explicit_preference_is_not_treated_as_one_off_log(self) -> None:
        from services.memory_noise_filter import is_explicit_preference, is_one_off_log

        self.assertTrue(is_explicit_preference("我今天最喜欢喝拿铁"))
        # 句中有"今天"，但说的是口味，不是流水账。
        self.assertFalse(is_one_off_log("我今天最喜欢喝拿铁"))


class AddMemoryExactDedupTests(unittest.IsolatedAsyncioTestCase):
    async def _make_settings(self) -> MagicMock:
        settings = MagicMock()
        settings.voyageai_model = "voyage-3"
        return settings

    def test_normalize_memory_text(self) -> None:
        from services.memory_service import _normalize_memory_text

        self.assertEqual(_normalize_memory_text("  我喜欢  拿铁  "), "我喜欢 拿铁")

    async def test_duplicate_exact_text_returns_existing(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        user_id = uuid.uuid4()
        settings = await self._make_settings()

        existing = MagicMock()
        existing.id = uuid.uuid4()

        with patch(
            "services.memory_service._find_exact_active_memory",
            new=AsyncMock(return_value=existing),
        ) as mock_find, patch(
            "services.memory_service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),
        ) as mock_embed:
            result = await add_memory(session, settings, user_id, "我喜欢拿铁", MemoryCategory.PREFERENCE)

        self.assertEqual(result.id, existing.id)
        mock_find.assert_awaited_once()
        mock_embed.assert_not_awaited()
        session.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
