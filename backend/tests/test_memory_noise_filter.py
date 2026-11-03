"""T1.2 —— 一次性流水账必须在落库前被代码拦截，不能只靠 Prompt。

Prompt 里写了 "FORBIDDEN TO SAVE"，但 LLM 仍会把"明天早上八点改简历""点了杯
拿铁"存成长期记忆。记忆污染会通过检索影响后续所有对话，所以必须有确定性闸门。

Run with: uv run pytest tests/test_memory_noise_filter.py
"""

import json
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from services.memory_noise_filter import is_one_off_log
from services.post_chat_memory_extraction import run_post_chat_memory_extraction


class IsOneOffLogTests(unittest.TestCase):
    def test_one_off_tasks_and_transactions_are_flagged(self) -> None:
        cases = [
            "明天早上八点改简历",
            "我待会儿要把快递放回宿舍",
            "今天九点参加腾讯会议",
            "下午三点取快递",
            "今天抢到了免单",
            "点了杯拿铁",
            "今天买了个新键盘",
            "10月25日去医院复查",
            "今天很忙",
            "这周要赶项目",
        ]
        for case in cases:
            self.assertTrue(is_one_off_log(case), f"应判定为流水账: {case}")

    def test_stable_facts_and_habits_pass(self) -> None:
        cases = [
            "用户喜欢喝燕麦拿铁",
            "我每天喝燕麦拿铁",
            "用户在杭州工作",
            "用户是一名后端工程师",
            "以后每次开会提前10分钟提醒我",
            "我每周三晚上打球",
            "用户偏好简洁的回复风格",
        ]
        for case in cases:
            self.assertFalse(is_one_off_log(case), f"不应判定为流水账: {case}")

    def test_empty_text_is_not_flagged(self) -> None:
        self.assertFalse(is_one_off_log(""))
        self.assertFalse(is_one_off_log("   "))


class HabitCategoryTests(unittest.TestCase):
    """长期习惯必须落 preference，不能被 LLM 归成 episodic 的流水账。"""

    def test_habit_sentences_are_recognised(self) -> None:
        from services.memory_noise_filter import describe_habit

        for case in ("用户爱玩手机", "喜欢打游戏", "爱刷短视频", "沉迷王者荣耀"):
            self.assertTrue(describe_habit(case), case)
        self.assertFalse(describe_habit("明天早上八点改简历"))
        self.assertFalse(describe_habit("我住在晋中学院"))

    def test_episodic_habit_is_corrected_to_preference(self) -> None:
        from services.memory_noise_filter import correct_habit_category

        self.assertEqual(correct_habit_category("用户爱玩手机", "episodic"), "preference")
        self.assertEqual(correct_habit_category("喜欢打游戏", "fact"), "preference")
        # 已经是 preference 的不动，真正的一次性事件也不动。
        self.assertEqual(correct_habit_category("喜欢喝燕麦拿铁", "preference"), "preference")
        self.assertEqual(correct_habit_category("昨天参加了毕业典礼", "episodic"), "episodic")

    def test_habits_are_not_filtered_as_one_off(self) -> None:
        self.assertFalse(is_one_off_log("用户爱玩手机"))
        self.assertFalse(is_one_off_log("喜欢打游戏"))


class ExtractionSkipsOneOffLogsTests(unittest.IsolatedAsyncioTestCase):
    """即使抽取 LLM 违规返回了流水账，也不得落库。"""

    async def _settings(self) -> MagicMock:
        settings = MagicMock()
        settings.database_url = "postgresql://x"
        settings.voyageai_api_key = "k"
        settings.voyageai_model = "voyage-3"
        return settings

    async def test_one_off_fact_is_dropped_before_persist(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        settings = await self._settings()

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add, patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(return_value=[]),
        ):
            llm = MagicMock()
            llm.ainvoke = AsyncMock(
                return_value=MagicMock(
                    content=json.dumps(
                        [
                            {"text": "明天早上八点改简历", "category": "episodic"},
                            {"text": "用户喜欢喝燕麦拿铁", "category": "preference"},
                        ],
                        ensure_ascii=False,
                    )
                )
            )
            await run_post_chat_memory_extraction(
                session,
                settings,
                uuid.uuid4(),
                [{"role": "user", "content": "明天早上八点改简历，顺便说下我喜欢燕麦拿铁"}],
                "好的。",
                extraction_llm=llm,
            )

        # 只保存那条真正的长期偏好。
        self.assertEqual(mock_add.call_count, 1)
        self.assertEqual(mock_add.call_args.args[3], "用户喜欢喝燕麦拿铁")

    async def test_habit_misclassified_as_episodic_is_stored_as_preference(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        settings = await self._settings()

        with patch(
            "services.post_chat_memory_extraction.add_memory_tool",
            new=AsyncMock(),
        ) as mock_add, patch(
            "services.post_chat_memory_extraction.find_similar_memories",
            new=AsyncMock(return_value=[]),
        ):
            llm = MagicMock()
            llm.ainvoke = AsyncMock(
                return_value=MagicMock(
                    content=json.dumps(
                        [{"text": "用户爱玩手机", "category": "episodic"}],
                        ensure_ascii=False,
                    )
                )
            )
            await run_post_chat_memory_extraction(
                session,
                settings,
                uuid.uuid4(),
                [{"role": "user", "content": "我平时爱玩手机"}],
                "了解啦。",
                extraction_llm=llm,
            )

        self.assertEqual(mock_add.call_count, 1)
        self.assertEqual(mock_add.call_args.args[3], "用户爱玩手机")
        self.assertEqual(mock_add.call_args.args[4], "preference")


if __name__ == "__main__":
    unittest.main()
