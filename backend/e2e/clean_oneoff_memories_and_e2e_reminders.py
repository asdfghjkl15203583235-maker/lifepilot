"""一次性数据清洗：软删历史流水账记忆 + 删除 E2E 产生的快递测试任务/提醒。

背景：
1. `memory_noise_filter.is_one_off_log` 是新增的写入闸门，只防新增，不清历史。
   本脚本按同一判定扫 active 记忆，命中的软删除（is_active=false），并清 Redis
   记忆缓存，保证检索链路立即生效。
2. `e2e_direct_due_at_reminder.py` 跑过两次，在真实库里留下两条
   「把快递放回宿舍」任务与同刻提醒。它们是测试数据，删除。

运行方式（先审计，确认后再执行）：

    uv run python e2e/clean_oneoff_memories_and_e2e_reminders.py --dry-run
    uv run python e2e/clean_oneoff_memories_and_e2e_reminders.py
"""

import asyncio
import sys
from pathlib import Path

import redis.asyncio as aioredis
from sqlalchemy import text

from config import get_settings
from db.session import create_async_engine_from_settings
from services.memory_noise_filter import is_one_off_log

REPORT_PATH = Path(__file__).resolve().parent / "clean_oneoff_memories_and_e2e_reminders_report.txt"
E2E_TASK_TITLE = "把快递放回宿舍"


async def _clean_memories(conn, lines: list[str], dry_run: bool) -> dict[str, list[str]]:
    res = await conn.execute(
        text(
            """
            SELECT id, user_id, text, category, created_at
            FROM user_memories
            WHERE is_active = TRUE
            ORDER BY created_at
            """
        )
    )
    rows = list(res.fetchall())
    lines.append(f"active 记忆总数: {len(rows)}")

    affected: dict[str, list[str]] = {}
    for row_id, user_id, mem_text, category, created_at in rows:
        candidate = (mem_text or "").strip()
        if not is_one_off_log(candidate):
            continue
        affected.setdefault(str(user_id), []).append(f"{row_id} | {category} | {candidate}")
        if not dry_run:
            await conn.execute(
                text("UPDATE user_memories SET is_active = FALSE WHERE id = :id"),
                {"id": row_id},
            )

    total = sum(len(items) for items in affected.values())
    lines.append(f"{'将软删' if dry_run else '已软删'}流水账记忆: {total} 条")
    for user_id, items in affected.items():
        lines.append(f"  user {user_id}:")
        for item in items:
            lines.append(f"    - {item}")
    return affected


async def _clean_e2e_reminders(conn, lines: list[str], dry_run: bool) -> int:
    task_res = await conn.execute(
        text(
            """
            SELECT id, title, due_at, created_at
            FROM tasks
            WHERE title = :title
            ORDER BY created_at
            """
        ),
        {"title": E2E_TASK_TITLE},
    )
    tasks = list(task_res.fetchall())
    lines.append(f"命中标题「{E2E_TASK_TITLE}」的任务: {len(tasks)} 条")
    if not tasks:
        return 0

    task_ids = [row[0] for row in tasks]
    rem_res = await conn.execute(
        text(
            """
            SELECT id, title, remind_at, status
            FROM user_reminders
            WHERE task_id = ANY(:task_ids) OR title = :title
            ORDER BY created_at
            """
        ),
        {"task_ids": task_ids, "title": E2E_TASK_TITLE},
    )
    reminders = list(rem_res.fetchall())

    for task_id, title, due_at, created_at in tasks:
        lines.append(f"  task {task_id} | due={due_at} | created={created_at} | {title}")
    for rem_id, title, remind_at, status in reminders:
        lines.append(f"  reminder {rem_id} | remind_at={remind_at} | {status} | {title}")

    if not dry_run:
        await conn.execute(
            text(
                """
                DELETE FROM user_reminders
                WHERE task_id = ANY(:task_ids) OR title = :title
                """
            ),
            {"task_ids": task_ids, "title": E2E_TASK_TITLE},
        )
        await conn.execute(
            text("DELETE FROM tasks WHERE id = ANY(:task_ids)"),
            {"task_ids": task_ids},
        )
    lines.append(
        f"{'将删除' if dry_run else '已删除'} E2E 测试任务 {len(tasks)} 条、提醒 {len(reminders)} 条"
    )
    return len(tasks)


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    lines: list[str] = [f"模式: {'DRY-RUN（只审计不写入）' if dry_run else '执行清理'}", ""]
    affected_memory_users: dict[str, list[str]] = {}
    try:
        async with engine.connect() as conn:
            affected_memory_users = await _clean_memories(conn, lines, dry_run)
            lines.append("")
            await _clean_e2e_reminders(conn, lines, dry_run)
            if not dry_run:
                await conn.commit()

        if not dry_run and affected_memory_users:
            redis = aioredis.from_url(settings.redis_url, decode_responses=True) if settings.redis_url else None
            if redis is not None:
                cleared = 0
                try:
                    for user_id in affected_memory_users:
                        for key in (
                            f"memory:user:{user_id}:chunks",
                            f"memory:user:{user_id}:bio",
                        ):
                            try:
                                if await redis.delete(key):
                                    cleared += 1
                            except Exception:
                                pass
                    lines.append(f"清除 Redis 记忆缓存 keys: {cleared}")
                finally:
                    await redis.aclose()
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Done ({'dry-run' if dry_run else 'applied'}). Report: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
