"""Regression tests for the email agent's L1-L4 anti-hallucination gates.

These mock the LLM and tools — no real mailbox is touched. They verify:
  1. L2 blocks a no-evidence final answer and forces a get_email call.
  2. L3 rejects an evasive "网易拦截" excuse when body evidence exists.
  3. L4 surfaces raw tool errors verbatim (no fabricated excuses).

Run with: python -m unittest tests.test_email_agent_verification
"""

import unittest
from typing import Any

from agents.email_agent_node import (
    _contains_evidence,
    _evidence_summary,
    _evidence_template,
    _is_evasive,
    _read_failure_template,
    _requires_email_content,
    _truncate_body,
    email_agent_node,
)
from agents.state import AgentState
from langchain_core.messages import AIMessage
from services.mail163_client import (
    Mail163Config,
    _truncate_body as _client_truncate_body,
)

LIST_RESULT = '{"messages": [{"id": "101", "subject": "TikTok 录取", "snippet": "恭喜你被Tiktok录取了"}, {"id": "102", "subject": "Qoder 活动", "snippet": "首次订阅 Lite 套餐买一送一"}], "list_is_summary_only": true}'
BODY_101 = "恭喜你被Tiktok录取了，欢迎加入公司，请于下周一报道。"
BODY_RESULT = f'{{"id": "101", "body": "{BODY_101}"}}'


class FakeTool:
    """Minimal stand-in for a LangChain tool: .name + .ainvoke."""

    def __init__(self, name: str, result: str, *, error: bool = False) -> None:
        self.name = name
        self.result = result
        self.error = error

    async def ainvoke(self, args: dict[str, Any]) -> str:
        if self.error:
            raise RuntimeError("Unsafe Login. Please check your IMAP settings.")
        return self.result


class FakeLLM:
    """Stand-in for ChatOpenAI: only needs .bind_tools (astream_invoke is patched)."""

    def bind_tools(self, tools: list[Any]) -> "FakeLLM":
        return self


class TestEmailAgentGates(unittest.IsolatedAsyncioTestCase):
    def _credentials(self) -> Mail163Config:
        return Mail163Config(email="user@163.com", auth_code="authcode-123")

    def _state(self, intent: str) -> AgentState:
        return AgentState(
            current_task={"intent": intent},
            messages=[],
            is_voice_request=False,
            email_credentials=self._credentials(),
        )

    def _patch_tools(self, tools: list[FakeTool]) -> None:
        def factory(config: Mail163Config) -> list[FakeTool]:
            return tools

        from unittest.mock import patch

        patcher = patch("agents.email_agent_node.make_mail163_email_tools", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_astream(self, sequence: list[AIMessage]) -> list[tuple[bool, int]]:
        from unittest.mock import patch

        calls: list[tuple[bool, int]] = []

        async def fake_astream_invoke(
            agent: Any,
            messages: Any,
            *,
            stream: bool = True,
            **kwargs: Any,
        ) -> AIMessage:
            calls.append((stream, len(messages)))
            if not sequence:
                return AIMessage(content="完成。")
            return sequence.pop(0)

        patcher = patch("agents.email_agent_node.astream_invoke", fake_astream_invoke)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    @staticmethod
    def _tool_call(name: str, args: dict[str, Any], mid: str) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": mid}])

    async def test_l2_blocks_no_evidence_and_forces_get_email(self) -> None:
        """LLM lists then answers without evidence → L2 injects get_email, body in the reply."""
        list_tool = FakeTool("list_emails", LIST_RESULT)
        get_tool = FakeTool("get_email", BODY_RESULT)
        self._patch_tools([list_tool, get_tool])
        self._patch_astream([
            self._tool_call("list_emails", {"query": "", "max_results": 10}, "c1"),
            AIMessage(content="抱歉，暂时无法查看邮件内容。"),
            AIMessage(content="以下是邮件正文：" + BODY_101),
        ])

        result = await email_agent_node(
            self._state("查看最近邮件的完整内容"),
            llm=FakeLLM(),
        )

        final = result["agent_results"][0]["result"]
        self.assertIn(BODY_101, final)
        self.assertNotIn("无法查看", final)

    async def test_l3_rejects_evasive_excuse_when_evidence_exists(self) -> None:
        """Model reads the body but then answers with an evasion → L3 rewrites it out."""
        get_tool = FakeTool("get_email", BODY_RESULT)
        self._patch_tools([get_tool])
        self._patch_astream([
            self._tool_call("get_email", {"message_id": "101"}, "c1"),
            AIMessage(content="由于网易邮箱安全拦截，暂时无法获取邮件内容。"),
            AIMessage(content="以下是邮件正文：" + BODY_101),
        ])

        result = await email_agent_node(
            self._state("看看最近邮件的原文"),
            llm=FakeLLM(),
        )

        final = result["agent_results"][0]["result"]
        self.assertNotIn("安全拦截", final)
        self.assertIn(BODY_101, final)

    async def test_l4_raw_error_template_no_fabrication(self) -> None:
        """Every read attempt errors → final answer is the raw-error template, no invented words."""
        list_tool = FakeTool("list_emails", LIST_RESULT)
        get_tool = FakeTool("get_email", "", error=True)
        self._patch_tools([list_tool, get_tool])
        # LLM never gets evidence; each round it answers without citing a body, and L2 keeps
        # forcing reads: first a forced list (captures id), then forced get_email(s) that error.
        self._patch_astream([
            AIMessage(content="让我看看。"),
            AIMessage(content="让我看看。"),
            AIMessage(content="让我看看。"),
            AIMessage(content="让我看看。"),
        ])

        result = await email_agent_node(
            self._state("读取最新邮件的完整内容"),
            llm=FakeLLM(),
        )

        final = result["agent_results"][0]["result"]
        self.assertIn("Unsafe Login", final)
        self.assertIn("check your IMAP settings", final)
        for word in ("无法访问", "出于隐私", "权限不足"):
            self.assertNotIn(word, final)

    async def test_l2_uses_list_when_no_id_known(self) -> None:
        """L2 injects a list first when no email id is known, then a get_email."""
        list_tool = FakeTool("list_emails", LIST_RESULT)
        get_tool = FakeTool("get_email", BODY_RESULT)
        self._patch_tools([list_tool, get_tool])
        # Round 0: no evidence → L2 injects forced list (captures ids).
        # Round 1: still no evidence → L2 injects forced get_email (captures body).
        # Round 2: final answer now quotes the fetched body.
        self._patch_astream([
            AIMessage(content="我看看有没有新邮件。"),
            AIMessage(content="获取中，请稍等。"),
            AIMessage(content="以下是邮件正文：" + BODY_101),
        ])

        result = await email_agent_node(
            self._state("帮我看看这周收到了什么邮件，把原文都给我"),
            llm=FakeLLM(),
        )

        final = result["agent_results"][0]["result"]
        # Forced list found id 101 → forced get_email → body present.
        self.assertIn(BODY_101, final)
        self.assertNotIn("请稍等", final)

    def test_requires_email_content_detection(self) -> None:
        self.assertTrue(_requires_email_content("看看最近的邮件内容"))
        self.assertTrue(_requires_email_content("读取邮件正文给我展示原文"))
        self.assertTrue(_requires_email_content("总结一下邮件里说了什么"))
        self.assertFalse(_requires_email_content("帮我回复这封邮件"))
        self.assertFalse(_requires_email_content("发送邮件给张三"))
        self.assertFalse(_requires_email_content("把这封邮件标记为已读"))

    def test_list_only_query_does_not_require_body(self) -> None:
        """「看看最近有没有新邮件」是列表查询，不该强制拉正文。

        真实回归：这句话里的「看」命中了内容词，于是 agent 强制 get_email
        并把整段原始正文（含 Qoder 验证码、MIME 头）倒进回复。
        """
        self.assertFalse(_requires_email_content("帮我看看最近三天有没有新邮件"))
        self.assertFalse(_requires_email_content("收件箱里有几封未读"))
        self.assertFalse(_requires_email_content("最近有哪些邮件"))
        # 明确要正文时，即使句子里有"有没有"也要读原文。
        self.assertTrue(_requires_email_content("有没有新邮件？把正文发我"))

    def test_evidence_template_does_not_dump_raw_bodies(self) -> None:
        template = _evidence_template(["Subject: hi\n\n" + "正文" * 300])
        self.assertIn("摘要", template)
        self.assertNotIn("以下是已读取到的邮件正文", template)
        self.assertNotIn("---", template)
        self.assertLess(len(template), 600)

    def test_is_evasive_detects_excuse_words(self) -> None:
        self.assertTrue(_is_evasive("由于网易安全拦截，暂时无法查看"))
        self.assertTrue(_is_evasive("需要检查你的IMAP设置"))
        self.assertTrue(_is_evasive("需要授权码才能访问"))
        self.assertFalse(_is_evasive("这是邮件正文：恭喜你被Tiktok录取了"))

    def test_contains_evidence_matches_body_fragment(self) -> None:
        self.assertTrue(
            _contains_evidence("恭喜你被Tiktok录取了，欢迎加入", [BODY_101])
        )
        self.assertFalse(_contains_evidence("无法查看", [BODY_101]))

    def test_read_failure_template_includes_raw_errors(self) -> None:
        template = _read_failure_template(["Unsafe Login. Please check IMAP settings."])
        self.assertIn("Unsafe Login", template)
        self.assertIn("原始错误信息", template)

    def test_evidence_summary_truncates(self) -> None:
        body = "恭喜你被Tiktok录取了，" * 20
        summary = _evidence_summary([body])
        self.assertIn("恭喜你被Tiktok录取了", summary)
        self.assertLess(len(summary), 400)

    # ── Edge case 3: over-long email body truncation ─────────────────

    def test_client_truncates_overlong_body_with_marker(self) -> None:
        long_body = "这是长文正文。章节" * 1200  # ~12000+ chars
        self.assertGreater(len(long_body), 5000)
        result = _client_truncate_body(long_body)
        self.assertLessEqual(len(result), 5000 + 30)  # body cap + short marker
        self.assertTrue(result.endswith("[内容过长，已截断...]"))
        self.assertIn("这是长文正文。章节", result)

    def test_client_keeps_short_body_untouched(self) -> None:
        result = _client_truncate_body("短正文")
        self.assertEqual(result, "短正文")
        self.assertNotIn("已截断", result)

    def test_node_truncate_body_caps_injected_evidence(self) -> None:
        long_body = "z" * 20_000  # would otherwise blow the context window
        result = _truncate_body(long_body)
        self.assertLessEqual(len(result), 5_000 + 30)
        self.assertTrue(result.endswith("[内容过长，已截断...]"))

    def test_node_truncate_body_avoids_double_marker(self) -> None:
        already_marked = "前文" + "\n[内容过长，已截断...]"
        result = _truncate_body(already_marked)
        self.assertEqual(result.count("[内容过长，已截断...]"), 1)

    async def test_evidence_template_caps_overlong_bodies(self) -> None:
        long_body = "重磅" * 3000  # 6000 chars, over the node cap
        template = _evidence_template([long_body])
        self.assertLessEqual(len(template), 600)
        self.assertIn("…", template)

    # ── Edge case 4: multiple emails loaded on demand ────────────────

    async def test_l2_injects_parallel_get_email_for_multiple_emails(self) -> None:
        """Summarizing 3 recent emails → L2 forces one parallel get_email per id."""
        list_result = '{"messages": [{"id": "201", "subject": "A", "snippet": "a"}, {"id": "202", "subject": "B", "snippet": "b"}, {"id": "203", "subject": "C", "snippet": "c"}]}'

        class MultiGet(FakeTool):
            async def ainvoke(self, args: dict[str, Any]) -> str:
                mid = args.get("message_id", "?")
                return f'{{"id": "{mid}", "body": "邮件{mid}的正文内容，共一段。"}}'

        list_tool = FakeTool("list_emails", list_result)
        get_tool = MultiGet("get_email", "")
        self._patch_tools([list_tool, get_tool])

        # Round 0: forced list (captures all 3 ids). Round 1: forced parallel
        # get_email for all 3 ids. Round 2: shared final summary of all 3.
        self._patch_astream([
            AIMessage(content="先查一下有哪些邮件。"),
            AIMessage(content="获取中。"),
            AIMessage(content="邮件201的正文内容，共一段。邮件202的正文内容，共一段。邮件203的正文内容，共一段。"),
        ])

        result = await email_agent_node(
            self._state("把最近3封邮件的核心内容都总结一下"),
            llm=FakeLLM(),
        )

        final = result["agent_results"][0]["result"]
        # All three bodies must be reflected in the final summary.
        for mid in ("201", "202", "203"):
            self.assertIn(f"邮件{mid}的正文内容", final)


class TestEmailAgentHitlGate(unittest.IsolatedAsyncioTestCase):
    """HITL 发送闸门：无 propose 回执的"已发送"声称必须被打回。"""

    def _credentials(self) -> Mail163Config:
        return Mail163Config(email="user@163.com", auth_code="authcode-123")

    def _state(self, intent: str) -> AgentState:
        return AgentState(
            current_task={"intent": intent},
            messages=[],
            is_voice_request=False,
            email_credentials=self._credentials(),
        )

    def _patch_tools(self, tools: list[FakeTool]) -> None:
        def factory(config: Mail163Config) -> list[FakeTool]:
            return tools

        from unittest.mock import patch

        patcher = patch("agents.email_agent_node.make_mail163_email_tools", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_propose_tools(self, tools: list[FakeTool]) -> None:
        def factory(session: Any, user_id: Any, config: Any) -> list[FakeTool]:
            return tools

        from unittest.mock import patch

        patcher = patch("agents.email_agent_node.make_propose_email_tools", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_astream(self, sequence: list[AIMessage]) -> None:
        from unittest.mock import patch

        async def fake_astream_invoke(
            agent: Any,
            messages: Any,
            *,
            stream: bool = True,
            **kwargs: Any,
        ) -> AIMessage:
            if not sequence:
                return AIMessage(content="完成。")
            return sequence.pop(0)

        patcher = patch("agents.email_agent_node.astream_invoke", fake_astream_invoke)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _tool_call(name: str, args: dict[str, Any], mid: str) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": mid}])

    async def test_hitl_blocks_unbacked_send_claim(self) -> None:
        """LLM claims '已发送' without a propose receipt → mechanically blocked."""
        self._patch_tools([])
        self._patch_propose_tools([])
        # Every retry the model stubbornly claims the send succeeded.
        self._patch_astream([
            AIMessage(content="已经发送邮件给 user@example.com。"),
            AIMessage(content="已经发送邮件给 user@example.com。"),
            AIMessage(content="已经发送邮件给 user@example.com。"),
            AIMessage(content="已经发送邮件给 user@example.com。"),
        ])

        result = await email_agent_node(
            self._state("帮我发一封邮件给 user@example.com，说周末见"),
            llm=FakeLLM(),
        )

        final = result["agent_results"][0]["result"]
        self.assertNotIn("已发送", final)
        self.assertNotIn("发送成功", final)
        self.assertEqual(result["pending_actions"], [])

    async def test_hitl_allows_claim_with_propose_receipt(self) -> None:
        """propose_email_draft returns a pending receipt → claim allowed + draft emitted."""
        propose_tool = FakeTool(
            "propose_email_draft",
            '{"status": "pending", "action_id": "aaa-bbb-ccc", "action_type": "email_send", "to": "user@example.com", "subject": "周末见"}',
        )
        self._patch_tools([])
        self._patch_propose_tools([propose_tool])
        self._patch_astream([
            self._tool_call(
                "propose_email_draft",
                {"to": "user@example.com", "subject": "周末见", "body": "这周末见！"},
                "c1",
            ),
            AIMessage(content="草稿已准备好，等待你确认后发送。"),
        ])

        result = await email_agent_node(
            self._state("帮我发一封邮件给 user@example.com，说周末见"),
            llm=FakeLLM(),
        )

        self.assertEqual(len(result["pending_actions"]), 1)
        self.assertEqual(result["pending_actions"][0]["action_type"], "email_send")
        final = result["agent_results"][0]["result"]
        self.assertIn("草稿", final)

    async def test_hitl_empty_final_text_gets_deterministic_confirmation(self) -> None:
        """草案已落库但模型返回空文本 → 必须给出「待确认」文案，不能是空气泡。"""
        propose_tool = FakeTool(
            "propose_email_draft",
            '{"status": "pending", "action_id": "aaa-bbb-ccc", "action_type": "email_send", "to": "user@example.com", "subject": "周末见"}',
        )
        self._patch_tools([])
        self._patch_propose_tools([propose_tool])
        self._patch_astream([
            self._tool_call(
                "propose_email_draft",
                {"to": "user@example.com", "subject": "周末见", "body": "这周末见！"},
                "c1",
            ),
            AIMessage(content=""),
        ])

        result = await email_agent_node(
            self._state("帮我发一封邮件给 user@example.com，说周末见"),
            llm=FakeLLM(),
        )

        self.assertEqual(len(result["pending_actions"]), 1)
        final = result["agent_results"][0]["result"]
        self.assertIn("确认", final)
        self.assertIn("草稿", final)
        self.assertNotIn("已发送", final)


if __name__ == "__main__":
    unittest.main()
