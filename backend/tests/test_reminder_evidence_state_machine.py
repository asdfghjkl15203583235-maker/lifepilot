"""基于工具回执的确定性状态机 + 上下文时间隔离 回归测试。

用户报告的严重 bug 链：
1. 混合意图（"提醒我周一上午十一点开会并查邮件"）里提醒分支被静默吞掉；
2. LLM 未真正调用 create_reminder 却谎报"已设置提醒"；
3. 把昨天晚上的"听歌、洗脚"翻出来当成当前执行目标，时间错乱。

这三条都由确定性状态机（intercept + force-retry + evidence gate）与
时间戳隔离（[最新请求]/[本会话背景] 标签 + 死命令）来兜底。本文件覆盖：
- 图级：混合意图同时触发 create_reminder 与查邮件工具，且 merge 完整呈现两分支；
- direct_reply 级：无工具回执却声称"已设置" → 被拦截并注入强制重试；
- agent_runner 级：历史消息加时间标签、最新请求标 [最新请求]。
"""

import json
import unittest
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from agents.graph import build_graph
from agents.orchestrator_node import (
    _append_reminder_confirm,
    _reminder_confirm_from_evidence,
)

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")


class ScriptedLLM(BaseChatModel):
    """按上下文自动路由到 orchestrate/direct_reply/email/merge 的假 LLM。

    每个分支读 `messages` 现有内容决定下一轮回什么（第一轮工具调用、
    拿到回执后终答），从而能让 LangGraph 完整走工具调用循环。
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def _llm_type(self) -> str:
        return "scripted-llm"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        # 不真正绑定工具——_generate 通过检查 messages 内容决定下一轮回复，
        # 包括是否发出 tool_call；返回 self 即可让节点按原逻辑走工具循环。
        return self

    def _generate(
        self,
        messages,
        stop=None,
        run_manager=None,
        **kwargs,
    ):
        system = messages[0].content if isinstance(messages[0].content, str) else ""
        if "路由分类器" in system:
            return self._resp(
                json.dumps(
                    {
                        "tasks": [
                            {"intent": "提醒我周一上午十一点开会", "route": "direct"},
                            {"intent": "看看最近一周的邮件", "route": "email"},
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        if "多个并行分支" in system:
            return self._resp(
                "好的，已帮你办妥：\n\n1. **周一上午十一点的会议提醒**已设置，届时会提前通知你。\n\n"
                "2. **最近一周的邮件**共 3 封，最紧要的一封是同事的请假申请，正文提到"
                "“我下周需要请假三天”。"
            )
        if "163 邮箱账户" in system or "邮件助手" in system:
            return self._email_round(messages)
        if "create_reminder" in system:
            return self._direct_round(messages)
        return self._resp("好的，明白了。")

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _resp(content: str) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @staticmethod
    def _tool_messages(messages):
        return [m.content for m in messages if isinstance(m, ToolMessage)]

    def _email_round(self, messages):
        tool_texts = "\n".join(self._tool_messages(messages))
        if "请假申请" in tool_texts:
            # 第三轮：已有正文回执 → 正常终答。
            return self._resp(
                "本周共 3 封邮件。最紧要的一封来自同事，主题是请假申请，正文为："
                "“我下周需要请假三天”，请留意安排。"
            )
        if '"b1"' in tool_texts:
            # 第二轮：有 list 回执但没有正文 → L2 门禁强制 get_email。
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "get_email",
                                    "args": {"message_id": "b1"},
                                    "id": "call-email-2",
                                }
                            ],
                        )
                    )
                ]
            )
        # 第一轮：还没有任何邮件回执 → 先 list。
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "list_emails",
                                "args": {"query": "", "max_results": 10},
                                "id": "call-email-1",
                            }
                        ],
                    )
                )
            ]
        )

    def _direct_round(self, messages):
        tool_texts = "\n".join(self._tool_messages(messages))
        if "created" in tool_texts:
            return self._resp("好的，已设置周一上午十一点的会议提醒，届时会提前通知你。")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "create_reminder",
                                "args": {
                                    "title": "周一开会提醒",
                                    "remind_at": "2026-10-28T11:00:00",
                                    "note": "用户要求提醒开会",
                                },
                                "id": "call-reminder-1",
                            }
                        ],
                    )
                )
            ]
        )


class ForceRetryLLM(BaseChatModel):
    """direct_reply 状态机拦截测试专用：先谎报、被拦截后才调工具。"""

    def __init__(self) -> None:
        super().__init__()
        object.__setattr__(self, "calls", [])

    @property
    def _llm_type(self) -> str:
        return "retry-llm"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        texts = [m.content for m in messages if isinstance(m.content, str)]
        self.calls.append(texts)
        joined = "\n".join(texts)
        has_create = any(isinstance(m, ToolMessage) and "created" in (m.content or "") for m in messages)
        if has_create:
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="好的，已设置提醒，回执已确认。"))]
            )
        if "未检测到成功的工具执行记录" in joined:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "create_reminder",
                                    "args": {"title": "开会提醒", "remind_at": "2026-10-28T11:00:00"},
                                    "id": "call-reminder-2",
                                }
                            ],
                        )
                    )
                ]
            )
        # 第一轮：直接谎报，不调用任何工具。
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="好的，已设置周一开会提醒。"))])


def _make_session() -> AsyncMock:
    from sqlalchemy.ext.asyncio import AsyncSession

    session = AsyncMock(spec=AsyncSession)
    session.begin_nested.return_value.__aenter__ = AsyncMock(return_value=session)
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    dedup_result = MagicMock()
    dedup_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=dedup_result)
    return session


def _make_settings() -> MagicMock:
    settings = MagicMock()
    settings.planner_timezone = "Asia/Shanghai"
    settings.planner_max_iterations = 4
    settings.reminder_default_lead_minutes = 10
    return settings


def _runtime_context(session, settings, email_credentials):
    return {
        "db_session": session,
        "settings": settings,
        "email_credentials": email_credentials,
        "gmail_access_token": None,
        "calendar_access_token": None,
        "notion_access_token": None,
    }


class _ListEmailsArgs(BaseModel):
    query: str = ""
    max_results: int = 10


class _GetEmailArgs(BaseModel):
    message_id: str


def _fake_email_tools():
    """返回成对 mock 邮件工具：list_emails → get_email，带可擦写的调用计数。"""
    list_fn = MagicMock(return_value=json.dumps(
        {"messages": [{"id": "b1", "from": "同事", "subject": "请假申请", "date": "2026-10-20", "snippet": "…"}]},
        ensure_ascii=False,
    ))
    get_fn = MagicMock(return_value=json.dumps(
        {"id": "b1", "body": "我下周需要请假三天。"},
        ensure_ascii=False,
    ))

    async def _list_emails(**kwargs: Any) -> str:
        return list_fn()

    async def _get_email(**kwargs: Any) -> str:
        return get_fn()

    tools = [
        StructuredTool(name="list_emails", description="List emails", args_schema=_ListEmailsArgs, coroutine=_list_emails),
        StructuredTool(name="get_email", description="Get email body", args_schema=_GetEmailArgs, coroutine=_get_email),
    ]
    return tools, list_fn, get_fn


class ReminderEvidenceStateMachineTests(unittest.IsolatedAsyncioTestCase):
    """图级回归：混合意图必须同时触发提醒工具与查邮件工具并完整合并。"""

    async def test_graph_mixed_intent_triggers_both_tools_and_merges_all_branches(self) -> None:
        reminder_row = MagicMock()
        reminder_row.id = uuid.uuid4()
        task_row = MagicMock()
        task_row.id = uuid.uuid4()

        graph = build_graph(ScriptedLLM(), checkpointer=InMemorySaver())
        session = _make_session()
        email_tools, list_fn, get_fn = _fake_email_tools()

        mock_create_reminder = AsyncMock(return_value=reminder_row)
        mock_create_task = AsyncMock(return_value=task_row)

        # 预置昨天"听歌洗脚"历史，验证最新请求不会串台。
        initial = {
            "user_id": USER_ID,
            "messages": [
                HumanMessage(content="[10-24 22:00] user: 提醒我今晚十点听歌洗脚"),
                AIMessage(content="[10-24 22:02] assistant: 好的，已为你在今晚十点安排听歌洗脚提醒。"),
                HumanMessage(content="[最新请求] user: 提醒我周一上午十一点开会，并看看最近一周的邮件"),
            ],
            "is_voice_request": False,
            "memory_context": "",
        }

        with (
            patch("agents.planner_agent_node.create_task_for_user", new=mock_create_task),
            patch("agents.tools.reminder_tool.create_reminder", new=mock_create_reminder),
            patch("agents.tools.reminder_tool.create_task_for_user", new=mock_create_task),
            patch("agents.email_agent_node.make_mail163_email_tools", return_value=email_tools),
        ):
            result = await graph.ainvoke(
                initial,
                {"configurable": {"thread_id": "mixed-intent-reminder-email"}},
                context=_runtime_context(session, _make_settings(), MagicMock()),
            )

        final = result.get("final_answer", "")
        # 合并结果必须完整呈现两个分支，任何分支不得遗漏。
        self.assertIn("会议提醒", final)
        self.assertIn("邮件", final)

        # 两个工具分支都必须真实触发（证据链）：
        # - 邮件查询：list 与 get 都被调用；
        # - 提醒：create_reminder 写库服务必须被真实调用，而不是只靠 LLM 嘴说。
        self.assertTrue(list_fn.called)
        self.assertTrue(get_fn.called)
        self.assertTrue(mock_create_reminder.called)
        # 昨天的"听歌洗脚"绝不能出现在最终回复里。
        self.assertNotIn("听歌", final)
        self.assertNotIn("洗脚", final)

    async def test_direct_reply_unbacked_claim_gets_intercepted_and_forced_to_retry(self) -> None:
        """无成功工具回执却声称"已设置" → 拦截并注入强制重试，最终必须工具回执放行。"""
        from agents.orchestrator_node import direct_reply_node

        reminder_row = MagicMock()
        reminder_row.id = uuid.uuid4()
        task_row = MagicMock()
        task_row.id = uuid.uuid4()

        session = _make_session()
        llm = ForceRetryLLM()
        state = {
            "db_session": session,
            "user_id": USER_ID,
            "settings": _make_settings(),
            "messages": [
                HumanMessage(content="[10-24 22:00] user: 提醒我今晚十点听歌洗脚"),
                HumanMessage(content="[最新请求] user: 提醒我周一开会"),
            ],
            "current_task": {
                "intent": "提醒我周一开会",
                "route": "direct",
                "task_key": "direct-1",
            },
            "is_voice_request": False,
        }

        with (
            patch("agents.tools.reminder_tool.create_reminder", new=AsyncMock(return_value=reminder_row)),
            patch("agents.tools.reminder_tool.create_task_for_user", new=AsyncMock(return_value=task_row)),
        ):
            result = await direct_reply_node(state, llm)

        results = result["agent_results"]
        self.assertEqual(len(results), 1)
        text = results[0]["result"]

        # 第一轮谎报被拦截：LLM 至少被调用两次（谎报 → 重试工具 → 终答）。
        self.assertGreaterEqual(len(llm.calls), 3)
        # 重试提示必须被注入过。
        injected = any("没有成功" in "\n".join(c) or "未检测到成功的工具执行记录" in "\n".join(c) for c in llm.calls)
        self.assertTrue(injected, "force-retry SystemMessage 未被注入到 LLM 上下文")
        # 最终回复与单轮谎报不同（被拿回执确认的版本）。
        self.assertIn("回执已确认", text)
        # 昨天的"听歌洗脚"绝不能出现在最终回复里。
        self.assertNotIn("听歌", text)
        self.assertNotIn("洗脚", text)


class EvidenceGateHelperTests(unittest.TestCase):
    def test_parse_tool_receipt(self) -> None:
        from agents.orchestrator_node import _parse_tool_receipt
        self.assertEqual(_parse_tool_receipt('{"status":"created","reminder_id":"r1"}')["status"], "created")
        self.assertIsNone(_parse_tool_receipt("not json"))
        self.assertIsNone(_parse_tool_receipt(""))

    def test_successful_receipt_requires_status_and_id(self) -> None:
        from agents.orchestrator_node import _is_successful_receipt
        self.assertTrue(_is_successful_receipt({"status": "created", "reminder_id": "r1"}))
        self.assertTrue(_is_successful_receipt({"status": "exists", "job_id": "j1"}))
        self.assertFalse(_is_successful_receipt({"status": "created"}))  # 无 id
        self.assertFalse(_is_successful_receipt({"status": "error", "reminder_id": "r1"}))
        self.assertFalse(_is_successful_receipt(None))

    def test_contains_claim_detects_setup_language(self) -> None:
        from agents.orchestrator_node import _contains_claim
        self.assertTrue(_contains_claim("已设置明早9点开会提醒"))
        self.assertTrue(_contains_claim("提醒已设置"))
        self.assertTrue(_contains_claim("reminder set for tomorrow"))
        self.assertFalse(_contains_claim("我查了一下邮件，共 3 封。"))


class TimelineIsolationTests(unittest.TestCase):
    def test_build_timeline_messages_labels_history_and_latest(self) -> None:
        from services.agent_runner import _build_timeline_messages

        payloads = [
            {"role": "user", "content": "提醒我今晚听歌洗脚", "timestamp": "2026-10-24T22:00:00+08:00"},
            {"role": "assistant", "content": "好的，已设置。", "timestamp": "2026-10-24T22:00:05+08:00"},
            {"role": "user", "content": "提醒我周一上午十一点开会并查邮件"},
        ]
        messages = _build_timeline_messages(payloads)

        self.assertTrue(messages[0].content.startswith("[10-24 22:00] user: 提醒我今晚听歌洗脚"))
        self.assertTrue(messages[1].content.startswith("[10-24 22:00] assistant: 好的，已设置。"))
        # 最后一条用户消息不带 timestamp → 标为 [最新请求]。
        self.assertTrue(messages[2].content.startswith("[最新请求] user: 提醒我周一上午十一点开会并查邮件"))

    def test_unknown_timestamp_falls_back_to_background_label(self) -> None:
        """无时间戳的历史消息标 [本会话背景]：它是上下文，不是可忽略的旧任务。"""
        from services.agent_runner import _build_timeline_messages

        payloads = [
            {"role": "user", "content": "昨天的任务", "timestamp": "not-a-date"},
            {"role": "user", "content": "现在的新请求"},
        ]
        messages = _build_timeline_messages(payloads)
        self.assertTrue(messages[0].content.startswith("[本会话背景] user: 昨天的任务"))
        self.assertTrue(messages[1].content.startswith("[最新请求] user: 现在的新请求"))

    def test_older_rounds_are_compressed_into_one_summary(self) -> None:
        """超过 5 轮的历史必须压成一条摘要，且摘要不能伪装成本轮请求。"""
        from langchain_core.messages import SystemMessage

        from services.agent_runner import _build_timeline_messages

        payloads = []
        for index in range(8):
            payloads.append({"role": "user", "content": f"旧问题{index}"})
            payloads.append({"role": "assistant", "content": f"旧回答{index}"})
        payloads.append({"role": "user", "content": "本轮新问题"})

        messages = _build_timeline_messages(payloads)

        self.assertIsInstance(messages[0], SystemMessage)
        self.assertIn("更早的对话摘要", messages[0].content)
        # 摘要里出现的旧内容不能以 [最新请求] 的身份混进来。
        self.assertTrue(all("[最新请求]" not in m.content for m in messages[:-1]))
        # 最后一条仍是本轮用户消息，标签正确。
        self.assertTrue(messages[-1].content.startswith("[最新请求] user: 本轮新问题"))
        # 最近 5 轮完整保留（5 条用户消息 + 4 条助手回复），加上摘要共 10 条。
        self.assertEqual(len(messages), 10)

    def test_short_conversation_has_no_summary(self) -> None:
        from services.agent_runner import _build_timeline_messages

        payloads = [
            {"role": "user", "content": "问题一"},
            {"role": "assistant", "content": "回答一"},
            {"role": "user", "content": "问题二"},
        ]
        messages = _build_timeline_messages(payloads)
        self.assertEqual(len(messages), 3)
        self.assertNotIn("更早的对话摘要", messages[0].content)

    def test_system_prompts_embed_context_isolation_rule(self) -> None:
        # 死命令必须进到分类器、direct、规划器的 system prompt。
        from agents.orchestrator_node import (
            CLASSIFY_SYSTEM_PROMPT,
            CONTEXT_ISOLATION_RULE,
        )
        from agents.planner_agent_node import PLANNER_SYSTEM_PROMPT, _CONTEXT_ISOLATION_RULE

        self.assertIn("{CONTEXT_ISOLATION_RULE}", CLASSIFY_SYSTEM_PROMPT)
        self.assertNotIn("绝对不能作为当前执行目标；严禁把昨天的任务重复确认或重复执行。".split("；")[0], "")
        self.assertIn("{context_isolation_rule}", PLANNER_SYSTEM_PROMPT)
        self.assertEqual(_CONTEXT_ISOLATION_RULE, CONTEXT_ISOLATION_RULE)


class ReminderConfirmHelperTests(unittest.TestCase):
    """模型自调 create_reminder 落库后，最终文本若没提提醒，确定性补确认句。"""

    RECEIPT = json.dumps(
        {
            "status": "created",
            "reminder_id": "r-1",
            "task_id": "t-1",
            "title": "国家图书馆 专注时段",
            "remind_at": "2026-11-05T01:30:00+00:00",
        },
        ensure_ascii=False,
    )

    def test_append_when_reply_drifted_away(self) -> None:
        # 真实 badcase：提醒落库了，回复却跑题成"明天的天气"。
        text = "北京市明天的天气：晴，气温 17至2°C。"
        result = _append_reminder_confirm(text, {"create_reminder": self.RECEIPT}, "Asia/Shanghai")
        self.assertIn("已设置提醒", result)
        self.assertIn("国家图书馆 专注时段", result)

    def test_skip_when_reply_already_mentions_reminder(self) -> None:
        text = "已设置提醒：「国家图书馆 专注时段」"
        result = _append_reminder_confirm(text, {"create_reminder": self.RECEIPT}, "Asia/Shanghai")
        self.assertEqual(result, text)

    def test_skip_when_no_receipt(self) -> None:
        text = "好的。"
        self.assertEqual(_append_reminder_confirm(text, {}, "Asia/Shanghai"), text)

    def test_confirmation_formats_local_time(self) -> None:
        # remind_at 是 UTC，必须换算成用户本地时区（Asia/Shanghai → +8 小时）。
        confirm = _reminder_confirm_from_evidence(self.RECEIPT, "Asia/Shanghai")
        self.assertIn("11-05 09:30", confirm)

    def test_skip_on_failed_receipt(self) -> None:
        failed = json.dumps({"status": "error", "message": "缺少参数"}, ensure_ascii=False)
        self.assertIsNone(_reminder_confirm_from_evidence(failed, "Asia/Shanghai"))


if __name__ == "__main__":
    unittest.main()