"""任务四：记忆提取 Prompt 重写 + 入库兜底（"请务必记住"不得成为记忆）。

两层防线：
1. `MEMORY_EXTRACTION_SYSTEM_PROMPT` 明确只抽持久事实、绝不抽祈使句，并要求
   把"请务必记住X"还原成 X 这个事实；
2. `add_memory` 入库前的确定性闸门：剥离命令框架后几乎无内容 → 直接丢弃。
"""

import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import MemoryCategory
from services.memory_service import (
    add_memory,
    is_pure_memory_instruction,
    normalize_memory_text_for_storage,
)
from services.post_chat_memory_extraction import MEMORY_EXTRACTION_SYSTEM_PROMPT


class ExtractionPromptRuleTests(unittest.TestCase):
    """Prompt 必须带上两条英文硬规则，且保留原有的第三人称与禁存规则。"""

    def test_durable_only_rule_present(self) -> None:
        self.assertIn("Only extract durable facts", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("NEVER extract imperative phrases", MEMORY_EXTRACTION_SYSTEM_PROMPT)

    def test_fact_behind_instruction_rule_present(self) -> None:
        self.assertIn(
            "extract the FACT behind it, not the instruction itself",
            MEMORY_EXTRACTION_SYSTEM_PROMPT,
        )

    def test_imperative_examples_are_named(self) -> None:
        for phrase in ("请务必记住", "千万别忘了"):
            self.assertIn(phrase, MEMORY_EXTRACTION_SYSTEM_PROMPT)

    def test_earlier_guards_are_preserved(self) -> None:
        self.assertIn("THIRD-PERSON REWRITE", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("SYSTEM NOISE", MEMORY_EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("FORBIDDEN TO SAVE", MEMORY_EXTRACTION_SYSTEM_PROMPT)


class PureInstructionGateTests(unittest.TestCase):
    def test_bare_directives_are_rejected(self) -> None:
        for text in (
            "请务必记住",
            "请记住。",
            "千万别忘了！",
            "记住",
            "帮我记一下",
            "记得：",
            "请务必记住，千万别忘了",
        ):
            self.assertTrue(is_pure_memory_instruction(text), text)

    def test_directive_with_real_fact_is_kept(self) -> None:
        for text in (
            "请务必记住我对花生过敏",
            "千万别忘了我们下周要去上海出差",
            "记住我最喜欢喝燕麦拿铁",
            "用户对花生过敏",
        ):
            self.assertFalse(is_pure_memory_instruction(text), text)

    def test_empty_text_is_not_treated_as_instruction(self) -> None:
        self.assertFalse(is_pure_memory_instruction(""))
        self.assertFalse(is_pure_memory_instruction("   "))


class AddMemoryDropsPureInstructionTests(unittest.IsolatedAsyncioTestCase):
    """入库闸门：纯指令绝不落库，也不触发 embedding 调用。"""

    async def test_pure_instruction_is_not_persisted(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        settings = MagicMock()
        settings.voyageai_model = "voyage-3"

        with patch(
            "services.memory_service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),
        ) as embed, patch(
            "services.memory_service._find_exact_active_memory",
            new=AsyncMock(return_value=None),
        ):
            result = await add_memory(
                session,
                settings,
                uuid.uuid4(),
                "请务必记住，千万别忘了！",
                MemoryCategory.RULE,
            )

        self.assertIsNone(result)
        session.add.assert_not_called()
        embed.assert_not_called()

    async def test_fact_behind_directive_is_saved_as_third_person(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        settings = MagicMock()
        settings.voyageai_model = "voyage-3"
        captured: dict[str, str] = {}

        def _make_row(**kwargs):
            captured["text"] = kwargs.get("text")
            row = MagicMock()
            row.id = uuid.uuid4()
            return row

        session.add = MagicMock()
        with patch(
            "services.memory_service._find_exact_active_memory",
            new=AsyncMock(return_value=None),
        ), patch(
            "services.memory_service.generate_embedding",
            new=AsyncMock(return_value=[0.1, 0.2]),
        ), patch(
            "services.memory_service.get_redis",
            return_value=None,
        ), patch(
            "services.memory_service.UserMemory",
            side_effect=_make_row,
        ):
            await add_memory(
                session,
                settings,
                uuid.uuid4(),
                "请务必记住我对花生过敏",
                MemoryCategory.FACT,
            )

        self.assertEqual(captured["text"], "用户对花生过敏")

    async def test_normalization_strips_emphasis_words(self) -> None:
        self.assertEqual(
            normalize_memory_text_for_storage("请务必记住我最喜欢喝拿铁"),
            "用户最喜欢喝拿铁",
        )


class MemoryToolSkipStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_reports_skipped_instead_of_crashing(self) -> None:
        from agents.tools.memory_tool import add_memory_tool

        with patch(
            "agents.tools.memory_tool.add_memory",
            new=AsyncMock(return_value=None),
        ):
            result = await add_memory_tool(
                AsyncMock(spec=AsyncSession),
                MagicMock(),
                uuid.uuid4(),
                "请记住",
                MemoryCategory.FACT.value,
            )

        self.assertEqual(result["status"], "skipped")
        self.assertIn("没有可保存的事实", result["detail"])


if __name__ == "__main__":
    unittest.main()
