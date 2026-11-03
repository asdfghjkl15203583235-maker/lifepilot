"""并发幂等：重复建任务的 Redis 锁 + 数据库唯一索引兜底。

真实 badcase：一轮对话里 planner 和 reminder 工具同时对同一件事建任务，
看板上出现两张一模一样的卡片。这里锁住 `create_task_for_user` 的行为：
  - 锁键只由 `user_id + title + due_at` 决定，标题空白差异不影响；
  - Redis 说"刚建过"时直接返回已有任务，不新增行；
  - 数据库唯一索引冲突时返回已存在的任务而不是抛错。
"""

import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import IntegrityError

from lib.enums import TaskStatus
from models.task import Task
from schemas.task import TaskCreateRequest
from services import task_service
from services.idempotency_service import (
    TASK_CREATE_LOCK_TTL_SECONDS,
    acquire_task_creation_lock,
    task_creation_lock_key,
)

USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DUE_AT = datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc)


def _task(title: str = "改简历") -> Task:
    return Task(
        id=uuid.uuid4(),
        user_id=USER_ID,
        title=title,
        description=None,
        status=TaskStatus.NOT_STARTED,
        project="生活",
        assigned_agent=None,
        priority="medium",
        dependencies=[],
        plan_id=None,
        due_at=DUE_AT,
        plan_source=None,
        source="user",
        source_metadata=None,
    )


def _body(title: str = "改简历") -> TaskCreateRequest:
    return TaskCreateRequest(title=title, due_at=DUE_AT)


def _session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


class LockKeyTests(unittest.TestCase):
    def test_same_signature_shares_one_key(self) -> None:
        first = task_creation_lock_key(USER_ID, "  改简历 ", DUE_AT)
        second = task_creation_lock_key(USER_ID, "改简历", DUE_AT)
        self.assertEqual(first, second)

    def test_different_due_at_gets_different_key(self) -> None:
        other = DUE_AT.replace(hour=11)
        self.assertNotEqual(
            task_creation_lock_key(USER_ID, "改简历", DUE_AT),
            task_creation_lock_key(USER_ID, "改简历", other),
        )

    def test_ttl_is_five_minutes(self) -> None:
        self.assertEqual(TASK_CREATE_LOCK_TTL_SECONDS, 300)


class AcquireLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_without_redis(self) -> None:
        with patch("services.idempotency_service.get_redis", return_value=None):
            self.assertIsNone(await acquire_task_creation_lock(USER_ID, "改简历", DUE_AT))

    async def test_returns_busy_when_set_nx_fails(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=None)
        with patch("services.idempotency_service.get_redis", return_value=redis):
            self.assertIs(await acquire_task_creation_lock(USER_ID, "改简历", DUE_AT), False)
        redis.set.assert_awaited_once()
        self.assertEqual(redis.set.await_args.kwargs["ex"], 300)

    async def test_returns_true_when_claim_succeeds(self) -> None:
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with patch("services.idempotency_service.get_redis", return_value=redis):
            self.assertIs(await acquire_task_creation_lock(USER_ID, "改简历", DUE_AT), True)


class CreateTaskDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_busy_returns_existing_task_without_insert(self) -> None:
        session = _session()
        existing = _task()
        with patch.object(task_service, "get_user_by_id", new=AsyncMock(return_value=MagicMock())), patch.object(
            task_service, "acquire_task_creation_lock", new=AsyncMock(return_value=False)
        ), patch.object(
            task_service, "find_task_by_signature", new=AsyncMock(return_value=existing)
        ):
            row = await task_service.create_task_for_user(session, user_id=USER_ID, body=_body())

        self.assertIs(row, existing)
        session.add.assert_not_called()

    async def test_unique_index_conflict_returns_existing_task(self) -> None:
        session = _session()
        session.flush = AsyncMock(
            side_effect=IntegrityError("INSERT", {}, Exception("duplicate key"))
        )
        existing = _task()
        with patch.object(task_service, "get_user_by_id", new=AsyncMock(return_value=MagicMock())), patch.object(
            task_service, "acquire_task_creation_lock", new=AsyncMock(return_value=True)
        ), patch.object(
            task_service, "find_task_by_signature", new=AsyncMock(return_value=existing)
        ):
            row = await task_service.create_task_for_user(session, user_id=USER_ID, body=_body())

        self.assertIs(row, existing)

    async def test_happy_path_inserts_normalized_title(self) -> None:
        session = _session()
        with patch.object(task_service, "get_user_by_id", new=AsyncMock(return_value=MagicMock())), patch.object(
            task_service, "acquire_task_creation_lock", new=AsyncMock(return_value=True)
        ):
            row = await task_service.create_task_for_user(
                session, user_id=USER_ID, body=_body("  改   简历 ")
            )

        self.assertIsNotNone(row)
        self.assertEqual(row.title, "改 简历")
        session.add.assert_called_once()
        session.flush.assert_awaited()
