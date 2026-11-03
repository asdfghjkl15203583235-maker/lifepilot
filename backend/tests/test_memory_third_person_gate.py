"""模块四：记忆入库前的第三人称强制归一（回归守卫）。

背景：抽取 Prompt 里已写了"THIRD-PERSON REWRITE — MANDATORY"，但 LLM 仍会
偶发把第一人称原话存进 `user_memories`（"请记住我最喜欢喝拿铁"）。记忆一旦被
污染，会通过检索注入污染后续所有对话。

修复：`add_memory` 入库前加一层确定性正则归一，即使大模型发疯存了第一人称，
入库前也会被强行掰正成第三人称。
"""

import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from lib.enums import MemoryCategory
from services.memory_service import add_memory, normalize_memory_text_for_storage


class NormalizeMemoryTextTests(unittest.TestCase):
    def test_first_person_pronoun_rewritten(self) -> None:
        self.assertEqual(
            normalize_memory_text_for_storage("我最喜欢喝拿铁"),
            "用户最喜欢喝拿铁",
        )

    def test_imperative_frame_stripped(self) -> None:
        self.assertEqual(
            normalize_memory_text_for_storage("请记住我最喜欢喝拿铁"),
            "用户最喜欢喝拿铁",
        )

    def test_dont_forget_frame_stripped(self) -> None:
        self.assertEqual(
            normalize_memory_text_for_storage("千万别忘了我们下周要出差"),
            "用户下周要出差",
        )

    def test_my_possessive_rewritten(self) -> None:
        self.assertEqual(
            normalize_memory_text_for_storage("我的生日是10月1日"),
            "用户的生日是10月1日",
        )

    def test_already_third_person_is_idempotent(self) -> None:
        text = "用户喜欢喝拿铁"
        self.assertEqual(normalize_memory_text_for_storage(text), text)

    def test_third_person_others_untouched(self) -> None:
        """说别人（他/她）的句子不该被改成"用户"。"""
        text = "用户的朋友小李喜欢打游戏"
        self.assertEqual(normalize_memory_text_for_storage(text), text)

    def test_empty_is_empty(self) -> None:
        self.assertEqual(normalize_memory_text_for_storage(""), "")


class AddMemoryNormalizationGateTests(unittest.IsolatedAsyncioTestCase):
    """入库闸门：即使调用方传第一人称，落库的 text 也必须是第三人称。"""

    async def test_add_memory_normalizes_before_persist(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        settings = MagicMock()
        settings.voyageai_model = "voyage-3"
        user_id = uuid.uuid4()

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
                session, settings, user_id, "请记住我最喜欢喝拿铁", MemoryCategory.PREFERENCE,
            )
        self.assertEqual(captured["text"], "用户最喜欢喝拿铁")


if __name__ == "__main__":
    unittest.main()
