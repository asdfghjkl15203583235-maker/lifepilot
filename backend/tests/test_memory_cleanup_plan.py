"""模块四清洗脚本：智能清洗记忆库（禁止全删）的可测纯函数。

真实 badcase：记忆库里混入大量第一人称、祈使句和系统噪音（"请记住…"、
"千万别忘了…"、"Notion 活动/已调用接口"）。要求：
  - 物理删除含"请记住""千万别忘了""Notion 活动"等噪音的记录；
  - 合并近重复项；
  - 统一转为第三人称。

本测试锁定脚本的判定/归一/合并逻辑，保证"智能清洗、禁止全删"。
"""

import unittest

from e2e.clean_memory_library import (
    is_system_noise,
    normalize_to_third_person,
    plan_cleanup,
)


class NoiseDetectionTests(unittest.TestCase):
    def test_imperative_frame_is_noise(self) -> None:
        for text in ("请记住我喜欢喝拿铁", "千万别忘了下周开会", "帮我记一下买牛奶"):
            self.assertTrue(is_system_noise(text), text)

    def test_notion_activity_is_noise(self) -> None:
        for text in ("Notion 活动：用户查看了页面", "已调用 Notion 接口", "Notion 页面已更新"):
            self.assertTrue(is_system_noise(text), text)

    def test_plain_fact_is_not_noise(self) -> None:
        for text in ("用户喜欢喝拿铁", "用户的生日是10月1日", "用户住在晋中学院"):
            self.assertFalse(is_system_noise(text), text)


class ThirdPersonNormalizationTests(unittest.TestCase):
    def test_first_person_rewritten(self) -> None:
        self.assertEqual(normalize_to_third_person("我最喜欢喝拿铁"), "用户最喜欢喝拿铁")


class PlanCleanupTests(unittest.TestCase):
    def test_noise_records_planned_for_physical_delete(self) -> None:
        records = [
            {"id": "1", "text": "请记住我喜欢喝拿铁", "category": "preference"},
            {"id": "2", "text": "用户住在晋中学院", "category": "identity"},
        ]
        plan = plan_cleanup(records)
        self.assertEqual([r["id"] for r in plan["delete"]], ["1"])
        # 干净的第三人称唯一记录保留、无需改写。
        self.assertEqual([r["id"] for r in plan["keep"]], ["2"])
        self.assertEqual(plan["rewrite"], [])

    def test_duplicates_merged_keeping_one(self) -> None:
        records = [
            {"id": "1", "text": "用户喜欢喝拿铁", "category": "preference"},
            {"id": "2", "text": "我喜欢喝拿铁", "category": "preference"},
        ]
        plan = plan_cleanup(records)
        # 归一后两条都是"用户喜欢喝拿铁" → 合并为一条，另一条删除。
        self.assertEqual(len(plan["keep"]), 1)
        self.assertEqual(plan["keep"][0]["text"], "用户喜欢喝拿铁")
        self.assertEqual(len(plan["delete"]), 1)

    def test_first_person_record_is_rewritten_not_deleted(self) -> None:
        """第一人称不是噪音 → 改写为第三人称保留，不能一刀切删掉。"""
        records = [{"id": "1", "text": "我的生日是10月1日", "category": "identity"}]
        plan = plan_cleanup(records)
        self.assertEqual(plan["delete"], [])
        self.assertEqual(plan["rewrite"], [{"id": "1", "text": "用户的生日是10月1日"}])

    def test_never_deletes_everything(self) -> None:
        """即使全是噪音，也必须有 dry-run 报告；plan 明确列出删除项而非静默清空。"""
        records = [{"id": "1", "text": "请记住我", "category": "fact"}]
        plan = plan_cleanup(records)
        self.assertEqual(len(plan["delete"]), 1)
        self.assertIn("原因", plan["delete"][0])


if __name__ == "__main__":
    unittest.main()
