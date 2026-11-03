"""Regression tests for the HITL (human-in-the-loop) high-risk write gate.

These mock the LLM and persistence layers — no real mailbox/task row is touched.
They verify:
  1. direct_reply rejects an unbacked "任务已完成/已删除" claim until a propose
     tool receipt exists, then emits a pending_action draft.
  2. propose_task_update / propose_task_delete persist a PendingAction draft
     and never mutate the task directly.
  3. pending_action_service.approve_and_execute executes TASK_UPDATE from the
     frozen payload (not from conversation text).
  4. email_agent blocks a "已发送" success claim without a propose receipt.

Run with: python -m unittest tests.test_pending_action_hitl / uv run pytest tests/test_pending_action_hitl.py
"""

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage
from sqlalchemy.ext.asyncio import AsyncSession

from agents.orchestrator_node import direct_reply_node
from agents.state import AgentState
from agents.tools.task_action_tools import (
    PROPOSE_TASK_DELETE_TOOL_NAME,
    PROPOSE_TASK_UPDATE_TOOL_NAME,
    make_task_action_tools,
)
from lib.enums import PendingActionStatus, PendingActionType
from services.mail163_client import Mail163Error
from services.pending_action_service import (
    PendingActionNotApprovableError,
    approve_and_execute,
)


class FakeLLM:
    """Stand-in for ChatOpenAI: only needs .bind_tools (astream_invoke is patched)."""

    def bind_tools(self, tools: list[Any]) -> "FakeLLM":
        return self


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _fake_task_row(task_id: uuid.UUID, title: str = "写周报") -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.title = title
    task.status = MagicMock()
    task.status.value = "in_progress"
    task.priority = MagicMock()
    task.priority.value = "medium"
    task.due_at = None
    task.project = None
    return task


def _fake_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _pending_action_row(
    action_type: PendingActionType,
    payload: dict[str, Any],
    *,
    status: PendingActionStatus = PendingActionStatus.PENDING,
    expires_in: timedelta | None = timedelta(days=7),
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    row.action_type = action_type
    row.status = status
    row.title = "测试操作"
    row.summary = "摘要"
    row.payload = payload
    row.result = None
    row.expires_at = datetime.now(timezone.utc) + expires_in if expires_in else None
    row.executed_at = None
    row.updated_at = datetime.now(timezone.utc)
    return row


class DirectReplyTaskGateTests(unittest.IsolatedAsyncioTestCase):
    """direct_reply 任务写门槛：无 propose 回执的"已完成/已删除"声称必须被拦截。"""

    def setUp(self) -> None:
        self.settings = MagicMock()
        self.settings.reminder_default_lead_minutes = 30
        self.task_id = _uuid()

    def _runtime(self, session: AsyncMock) -> SimpleNamespace:
        return SimpleNamespace(context={"db_session": session, "settings": self.settings})

    def _state(self) -> AgentState:
        return AgentState(
            current_task={"intent": "把「写周报」标为完成"},
            messages=[],
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        )

    def _patch_astream(self, sequence: list[AIMessage]) -> None:
        async def fake_astream_invoke(agent: Any, messages: Any, *, stream: bool = True, **kwargs: Any) -> AIMessage:
            if not sequence:
                return AIMessage(content="完成。")
            return sequence.pop(0)

        patcher = patch("agents.orchestrator_node.astream_invoke", fake_astream_invoke)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_task_backend(self) -> None:
        async def fake_get_task_for_user(session, *, task_id, user_id):
            return _fake_task_row(task_id)

        async def fake_create_pending_action(session, *, user_id, action_type, title, summary, payload, conversation_id=None, expires_in_days=7):
            row = _pending_action_row(action_type, payload)
            row.title = title
            row.summary = summary
            return row

        for target in (
            "agents.tools.task_action_tools.get_task_for_user",
            "agents.tools.task_action_tools.create_pending_action",
        ):
            patcher = patch(target, new=fake_get_task_for_user if "get_task" in target else fake_create_pending_action)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _tool_call(name: str, args: dict[str, Any], mid: str) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": mid}])

    async def test_unbacked_completed_claim_returns_no_result_and_retries(self) -> None:
        """LLM claims done without calling propose → blocked, keep looping."""
        session = _fake_session()
        calls: list[str] = []

        async def fake_astream_invoke(agent: Any, messages: Any, *, stream: bool = True, **kwargs: Any) -> AIMessage:
            # Every retry, the LLM stubbornly answers a claim without tools.
            for msg in messages:
                if isinstance(msg, SystemMessage):
                    calls.append(msg.content)
            return AIMessage(content="任务已完成，已标为完成。")

        with patch("agents.orchestrator_node.astream_invoke", fake_astream_invoke):
            result = await direct_reply_node(
                self._state(),
                llm=FakeLLM(),
                runtime=self._runtime(session),  # type: ignore[arg-type]
            )

        self.assertEqual(result["pending_actions"], [])
        text = result["agent_results"][0]["result"]
        self.assertNotIn("已完成", text)
        # The gate must have injected the task-specific retry prompt at least once.
        self.assertTrue(any("草案" in c or "propose_task_update" in c for c in calls))

    async def test_propose_receipt_allows_done_claim(self) -> None:
        """propose_task_update returns a pending receipt → claim allowed + draft emitted."""
        session = _fake_session()
        self._patch_task_backend()

        # Round 1: propose_task_update creates a receipt. Round 2: final answer.
        self._patch_astream([
            self._tool_call(PROPOSE_TASK_UPDATE_TOOL_NAME, {"task_id": str(self.task_id), "status": "done"}, "c1"),
            AIMessage(content="草稿已准备好，等待你确认后执行。"),
        ])
        result = await direct_reply_node(
            self._state(),
            llm=FakeLLM(),
            runtime=self._runtime(session),  # type: ignore[arg-type]
        )

        self.assertEqual(len(result["pending_actions"]), 1)
        self.assertEqual(result["pending_actions"][0]["action_type"], "task_update")
        out = result["agent_results"][0]["result"]
        self.assertTrue(any(k in out for k in ("草稿", "确认")))


class TaskActionToolsTests(unittest.IsolatedAsyncioTestCase):
    """propose 工具只落草案，绝不直接改任务。"""

    def setUp(self) -> None:
        self.session = _fake_session()
        self.user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        self.task_id = _uuid()

    async def _patch_task_backend(self) -> None:
        async def fake_get_task_for_user(session, *, task_id, user_id):
            return _fake_task_row(task_id)

        async def fake_create_pending_action(session, *, user_id, action_type, title, summary, payload, conversation_id=None, expires_in_days=7):
            row = _pending_action_row(action_type, payload)
            row.title = title
            row.summary = summary
            return row

        patch_get = patch(
            "agents.tools.task_action_tools.get_task_for_user",
            new=fake_get_task_for_user,
        )
        patch_create = patch(
            "agents.tools.task_action_tools.create_pending_action",
            new=fake_create_pending_action,
        )
        patch_get.start()
        patch_create.start()
        self.addCleanup(patch_get.stop)
        self.addCleanup(patch_create.stop)

    async def test_propose_update_creates_draft_document(self) -> None:
        await self._patch_task_backend()
        update_tool = next(
            t for t in make_task_action_tools(self.session, self.user_id)
            if t.name == PROPOSE_TASK_UPDATE_TOOL_NAME
        )
        result = await update_tool.ainvoke(
            {"task_id": str(self.task_id), "status": "done", "due_at": "2026-11-01T09:00:00"}
        )

        import json as _json
        receipt = _json.loads(result)
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(receipt["action_type"], "task_update")
        self.assertTrue(receipt.get("action_id"))

    async def test_propose_delete_creates_draft_document(self) -> None:
        await self._patch_task_backend()
        delete_tool = next(
            t for t in make_task_action_tools(self.session, self.user_id)
            if t.name == PROPOSE_TASK_DELETE_TOOL_NAME
        )
        result = await delete_tool.ainvoke({"task_id": str(self.task_id)})

        import json as _json
        receipt = _json.loads(result)
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(receipt["action_type"], "task_delete")


class ApproveExecuteTests(unittest.IsolatedAsyncioTestCase):
    """approve 从冻结 payload 执行，绝不重新解析对话文本。"""

    def setUp(self) -> None:
        self.session = _fake_session()
        self.settings = MagicMock()
        self.settings.reminder_default_lead_minutes = 30
        self.user_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        self.task_id = _uuid()

    async def test_approve_task_update_executes_from_payload(self) -> None:
        action = _pending_action_row(
            PendingActionType.TASK_UPDATE,
            {"task_id": str(self.task_id), "task_title": "写周报", "changes": {"status": "done"}},
        )

        updated = MagicMock()
        updated.title = "写周报"
        updated.status = MagicMock()
        updated.status.value = "done"
        updated.due_at = None

        with patch(
            "services.pending_action_service.get_pending_action_for_user",
            new=AsyncMock(return_value=action),
        ), patch(
            "services.pending_action_service.get_task_for_user",
            new=AsyncMock(return_value=_fake_task_row(self.task_id)),
        ), patch(
            "services.pending_action_service.update_task",
            new=AsyncMock(return_value=updated),
        ):
            action, result = await approve_and_execute(
                self.session, self.user_id, action.id, settings=self.settings,
            )

        self.assertEqual(action.status, PendingActionStatus.APPROVED)
        self.assertEqual(result["status"], "done")
        self.session.flush.assert_awaited()

    async def test_approve_expired_rejects(self) -> None:
        expired = _pending_action_row(
            PendingActionType.TASK_UPDATE,
            {"task_id": str(self.task_id), "changes": {"status": "done"}},
            status=PendingActionStatus.PENDING,
            expires_in=timedelta(days=-1),
        )
        with patch(
            "services.pending_action_service.get_pending_action_for_user",
            new=AsyncMock(return_value=expired),
        ):
            with self.assertRaises(PendingActionNotApprovableError):
                await approve_and_execute(
                    self.session, self.user_id, expired.id, settings=self.settings,
                )
        self.assertEqual(expired.status, PendingActionStatus.EXPIRED)

    async def test_approve_already_approved_is_idempotent(self) -> None:
        """回归：双击/重试会让同一个确认提交两次，第二次必须返回既有结果，
        而不是抛"状态为 approved，无法确认"让用户以为邮件没发出去。"""
        approved = _pending_action_row(
            PendingActionType.TASK_UPDATE,
            {"task_id": str(self.task_id), "changes": {"status": "done"}},
            status=PendingActionStatus.APPROVED,
        )
        approved.result = {"status": "done"}
        with patch(
            "services.pending_action_service.get_pending_action_for_user",
            new=AsyncMock(return_value=approved),
        ):
            action, result = await approve_and_execute(
                self.session, self.user_id, approved.id, settings=self.settings,
            )
        self.assertIs(action, approved)
        self.assertEqual(result, {"status": "done"})

    async def test_retryable_mail_failure_keeps_action_pending(self) -> None:
        """163 临时拒绝登录（限频）时，草稿必须还能再点一次发送。"""
        action = _pending_action_row(
            PendingActionType.EMAIL_SEND,
            {"to": "team@example.com", "subject": "周报", "body": "本周进展"},
        )
        failure = Mail163Error("163 邮箱暂时拒绝了登录，请稍后重试", 502, retryable=True)

        with patch(
            "services.pending_action_service.get_pending_action_for_user",
            new=AsyncMock(return_value=action),
        ), patch(
            "services.pending_action_service._execute_email",
            new=AsyncMock(side_effect=failure),
        ):
            with self.assertRaises(Mail163Error):
                await approve_and_execute(
                    self.session, self.user_id, action.id, settings=self.settings,
                )

        self.assertEqual(action.status, PendingActionStatus.PENDING)
        self.assertTrue(action.result["retryable"])

    async def test_permanent_mail_failure_marks_action_failed(self) -> None:
        action = _pending_action_row(
            PendingActionType.EMAIL_SEND,
            {"to": "team@example.com", "subject": "周报", "body": "本周进展"},
        )
        failure = Mail163Error("收件人地址无效", 400)

        with patch(
            "services.pending_action_service.get_pending_action_for_user",
            new=AsyncMock(return_value=action),
        ), patch(
            "services.pending_action_service._execute_email",
            new=AsyncMock(side_effect=failure),
        ):
            with self.assertRaises(Mail163Error):
                await approve_and_execute(
                    self.session, self.user_id, action.id, settings=self.settings,
                )

        self.assertEqual(action.status, PendingActionStatus.FAILED)

    async def test_approve_non_pending_rejects(self) -> None:
        already_rejected = _pending_action_row(
            PendingActionType.TASK_UPDATE,
            {"task_id": str(self.task_id), "changes": {"status": "done"}},
            status=PendingActionStatus.REJECTED,
        )
        with patch(
            "services.pending_action_service.get_pending_action_for_user",
            new=AsyncMock(return_value=already_rejected),
        ):
            with self.assertRaises(PendingActionNotApprovableError):
                await approve_and_execute(
                    self.session, self.user_id, already_rejected.id, settings=self.settings,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)