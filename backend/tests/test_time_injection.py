"""时间感知回归测试。

真实 badcase：模型把「今天」算成 11-06、还搞错星期。根因是 System Prompt 里
只给了英文日期（"Wednesday, November 04..."）或根本没给周几，模型只能自己猜。

修复：统一在 `agents/common.py` 用代码动态注入
`【当前时间】：YYYY-MM-DD HH:MM，星期X` + 未来 7 天查表 + 时间锚点死命令，
所有 agent 一律引用，禁止模型自行推算日期/星期。
"""

import re
import unittest
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, HumanMessage

from agents.common import (
    TIME_ANCHOR_RULE,
    render_now_reference,
    render_time_reference,
)
from agents.orchestrator_node import direct_reply_node

_NOW_ANCHOR_RE = re.compile(r"【当前时间】：\d{4}-\d{2}-\d{2} \d{2}:\d{2}，星期[一二三四五六日]")


def _now_asia_shanghai() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


class RenderNowReferenceTests(unittest.TestCase):
    def test_anchor_matches_system_now(self) -> None:
        now = _now_asia_shanghai()
        expected_date = now.strftime("%Y-%m-%d")
        weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[now.weekday()]

        text = render_now_reference("Asia/Shanghai")

        self.assertRegex(text, _NOW_ANCHOR_RE)
        self.assertTrue(text.startswith(f"【当前时间】：{expected_date} "), text)
        self.assertTrue(text.endswith(f"，{weekday}"), text)

    def test_anchor_uses_configured_timezone(self) -> None:
        """时区不是 UTC 时，锚点必须用配置时区的本地时间（用户在中国）。"""
        now = _now_asia_shanghai()
        text = render_now_reference("Asia/Shanghai")
        self.assertIn(now.strftime("%Y-%m-%d"), text)
        # 明确断言不是 UTC 日期（避开跨日边界，仅校验格式完整）。
        self.assertIn("星期", text)

    def test_time_reference_bundles_anchor_rule(self) -> None:
        text = render_time_reference("Asia/Shanghai")
        self.assertIn("【当前时间】：", text)
        self.assertIn("未来 7 天：", text)
        self.assertIn(TIME_ANCHOR_RULE, text)
        self.assertIn("严禁", TIME_ANCHOR_RULE)
        self.assertIn("未来日期", TIME_ANCHOR_RULE)


class DirectReplyTimeAnchorTests(unittest.IsolatedAsyncioTestCase):
    async def test_today_where_to_go_prompt_is_anchored_to_system_today(self) -> None:
        """"今天适合去哪" 的 System Prompt 必须携带系统当前日期，模型无从猜错星期。"""
        captured: dict[str, Any] = {}

        async def fake_astream_invoke(agent: Any, messages: Any, **kwargs: Any) -> AIMessage:
            captured["system"] = messages[0].content
            return AIMessage(content="推荐你去附近公园走走。")

        state = {
            "current_task": {"intent": "今天适合去哪", "route": "direct", "task_key": "x"},
            "messages": [HumanMessage(content="今天适合去哪")],
            "is_voice_request": False,
            "core_intent": None,
            "user_id": None,
        }
        runtime = SimpleNamespace(
            context={"settings": MagicMock(planner_timezone="Asia/Shanghai"), "db_session": None}
        )

        with patch("agents.orchestrator_node.astream_invoke", new=AsyncMock(side_effect=fake_astream_invoke)):
            await direct_reply_node(state, llm=MagicMock(), runtime=runtime)

        system_text = captured["system"]
        self.assertIn("【当前时间】：", system_text)
        now = _now_asia_shanghai()
        self.assertIn(f"【当前时间】：{now.strftime('%Y-%m-%d')}", system_text)
        self.assertIn(TIME_ANCHOR_RULE, system_text)

    async def test_life_route_prompt_includes_anchor(self) -> None:
        """life 路由（天气/地图/出行）同样注入时间锚点，避免推荐到未来日期。"""
        from agents.life_agent_node import life_agent_node

        captured: dict[str, Any] = {}

        async def fake_astream_invoke(agent: Any, messages: Any, **kwargs: Any) -> AIMessage:
            captured["system"] = messages[0].content
            return AIMessage(content="今天可以就近走走。")

        state = {
            "current_task": {"intent": "今天适合去哪", "route": "life", "task_key": "x"},
            "messages": [HumanMessage(content="今天适合去哪")],
            "is_voice_request": False,
            "core_intent": None,
            "user_id": None,
        }
        runtime = SimpleNamespace(
            context={"settings": MagicMock(planner_timezone="Asia/Shanghai"), "db_session": None}
        )

        with patch("agents.life_agent_node.astream_invoke", new=AsyncMock(side_effect=fake_astream_invoke)):
            await life_agent_node(state, llm=MagicMock(), runtime=runtime)

        self.assertIn("【当前时间】：", captured["system"])
        self.assertIn(TIME_ANCHOR_RULE, captured["system"])


if __name__ == "__main__":
    unittest.main()
