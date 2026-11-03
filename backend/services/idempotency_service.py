"""Redis/DB-backed idempotency keys for agent write actions and life-service calls.

The key contract is intentionally small: callers generate a stable UUID for an action
and pass `check_idempotent(key)` before calling an external API, then
`record_idempotent(key, payload)` after success. If a duplicate request arrives with
the same key, we return the stored payload instead of re-running the external call.

This module also owns the **task creation lock** used by `task_service`: chat turns
fan out to several agents in parallel, and two of them may create the same task
within milliseconds (e.g. planner step + reminder tool). A `user_id + title +
due_at` lock with a 300s TTL is the first line of defence; the `tasks` unique index
is the database backstop.
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from services.redis_client import get_redis

logger = logging.getLogger(__name__)

IDEMPOTENCY_TTL_SECONDS = 86_400  # 24 hours
# 与业务约定一致：5 分钟内同一个人/同一标题/同一截止时间只允许建一次。
TASK_CREATE_LOCK_TTL_SECONDS = 300
TASK_CREATE_LOCK_PREFIX = "lifepilot:task-create-lock"


def _redis_key(idempotency_key: str) -> str:
    return f"lifepilot:idem:{idempotency_key}"


def normalize_task_title(title: str) -> str:
    """Collapse whitespace so `"改简历 "` and `"改简历"` share one lock key."""
    return " ".join((title or "").split())


def task_creation_lock_key(
    user_id: object,
    title: str,
    due_at: datetime | None,
) -> str:
    """Stable Redis key for the `user_id + title + due_at` signature."""
    due_text = due_at.isoformat() if due_at is not None else ""
    signature = f"{user_id}|{normalize_task_title(title)}|{due_text}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:32]
    return f"{TASK_CREATE_LOCK_PREFIX}:{digest}"


async def acquire_task_creation_lock(
    user_id: object,
    title: str,
    due_at: datetime | None,
) -> bool | None:
    """Try to claim the creation lock.

    Returns:
        True  — claimed, this request may create the task;
        False — another request claimed it within the TTL (likely a duplicate);
        None  — Redis is unavailable; caller must fall back to the DB unique index.
    """
    redis_client = get_redis()
    if redis_client is None:
        return None
    try:
        acquired = await redis_client.set(
            task_creation_lock_key(user_id, title, due_at),
            "1",
            nx=True,
            ex=TASK_CREATE_LOCK_TTL_SECONDS,
        )
        return bool(acquired)
    except Exception as exc:
        logger.warning("Task creation lock failed for user %s: %s", user_id, exc)
        return None


async def check_idempotent(idempotency_key: str) -> dict[str, Any] | None:
    """Return the recorded result for a key, or None when no prior result exists."""
    redis_client = get_redis()
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(_redis_key(idempotency_key))
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("Idempotency read failed for %s: %s", idempotency_key, exc)
        return None


async def record_idempotent(idempotency_key: str, payload: dict[str, Any]) -> None:
    """Persist a successful action result for exactly-once behavior."""
    redis_client = get_redis()
    if redis_client is None:
        return
    try:
        await redis_client.set(
            _redis_key(idempotency_key),
            json.dumps(payload, default=str),
            ex=IDEMPOTENCY_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Idempotency write failed for %s: %s", idempotency_key, exc)


def new_idempotency_key() -> str:
    return str(uuid.uuid4())