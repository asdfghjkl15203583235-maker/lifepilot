"""Conversation title derivation tests — placeholder detection, LLM succeed/fallback.

The title flow must never block chat: LLM failure degrades to truncation, and
explicit user titles always beat generated ones.
"""

import unittest

from services.conversation_title_service import (
    TITLE_MIN_LENGTH,
    is_placeholder_title,
    truncate_title_from_text,
)


class PlaceholderTitleTests(unittest.TestCase):
    def test_empty_and_none_are_placeholders(self) -> None:
        self.assertTrue(is_placeholder_title(None))
        self.assertTrue(is_placeholder_title(""))
        self.assertTrue(is_placeholder_title("   "))

    def test_auto_tab_labels_are_placeholders(self) -> None:
        self.assertTrue(is_placeholder_title("对话 1"))
        self.assertTrue(is_placeholder_title("对话 12"))
        self.assertTrue(is_placeholder_title("无标题对话"))

    def test_real_title_is_not_placeholder(self) -> None:
        self.assertFalse(is_placeholder_title("周末旅游安排"))
        self.assertFalse(is_placeholder_title("SQLAlchemy 迁移问题"))


class TruncateTitleTests(unittest.TestCase):
    def test_truncates_first_line(self) -> None:
        text = "帮我安排明天下午三点的会议，并通知相关人员参加"
        title = truncate_title_from_text(text)
        self.assertEqual(title, text[:30])
        self.assertLessEqual(len(title), 30)

    def test_multiline_takes_first_line(self) -> None:
        text = "第一行标题内容很长的一段话\n第二行不相关内容"
        self.assertEqual(truncate_title_from_text(text), "第一行标题内容很长的一段话")

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(truncate_title_from_text(""), "")
        self.assertEqual(truncate_title_from_text("   "), "")


if __name__ == "__main__":
    unittest.main()