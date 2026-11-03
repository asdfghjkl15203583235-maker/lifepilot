"""评测 1：模糊时间解析。

输入「我待会儿要去拿快递」后必须：
  1. 落库一条标题含「快递」的任务，且 due_at ≈ now + 1 小时（误差 ≤ 5 分钟）；
  2. 落库一条对应的 pending reminder，remind_at 落在 [due_at - 默认提前量, due_at]
     窗口内（误差 ≤ 5 分钟），即提醒必须与这个任务严格对应。
"""

import unittest
import uuid
from datetime import timedelta

from sqlalchemy import text

from tests.evaluation.helpers import (
    as_utc,
    create_eval_user,
    delete_eval_user,
    eval_session,
    now_utc,
    run_chat,
)


class FuzzyTimeEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_tear_afterwards_creates_due_at_and_reminder(self) -> None:
        thread_id = f"eval-fuzzy-{uuid.uuid4().hex[:12]}"
        async with eval_session() as (settings, session):
            user_id = await create_eval_user(session)
            try:
                started_at = now_utc()
                _state, answer = await run_chat(
                    settings,
                    session,
                    user_id,
                    "我待会儿要去拿快递",
                    thread_id=thread_id,
                )

                tasks = (
                    await session.execute(
                        text(
                            "SELECT id, title, due_at FROM tasks "
                            "WHERE user_id = :uid AND title LIKE '%快递%' "
                            "ORDER BY created_at DESC"
                        ),
                        {"uid": user_id},
                    )
                ).fetchall()
                self.assertTrue(tasks, f"未落库含「快递」的任务。最终回答：{answer}")

                task_id, title, due_at = tasks[0]
                self.assertIsNotNone(due_at, f"任务「{title}」缺少 due_at")

                expected = started_at + timedelta(hours=1)
                skew = abs((as_utc(due_at) - expected).total_seconds())
                self.assertLessEqual(
                    skew,
                    300,
                    f"due_at={due_at} 应约等于 now+1h（{expected}），实际偏差 {skew:.0f}s",
                )

                reminders = (
                    await session.execute(
                        text(
                            "SELECT title, remind_at, status FROM user_reminders "
                            "WHERE user_id = :uid AND (title LIKE '%快递%' OR task_id = :tid) "
                            "ORDER BY created_at DESC"
                        ),
                        {"uid": user_id, "tid": task_id},
                    )
                ).fetchall()
                self.assertTrue(reminders, f"未生成提醒记录。最终回答：{answer}")
                remind_title, remind_at, status = reminders[0]
                self.assertIn(status, ("pending", "sent"))
                self.assertIsNotNone(remind_at)
                # Two legitimate paths: the planner schedules a deadline reminder
                # `reminder_default_lead_minutes` before due_at, while the direct
                # create_reminder tool mirrors due_at exactly. Accept both, with
                # the same 5-minute tolerance used above.
                lead_minutes = max(int(getattr(settings, "reminder_default_lead_minutes", 0) or 0), 0)
                earliest = as_utc(due_at) - timedelta(minutes=lead_minutes, seconds=300)
                latest = as_utc(due_at) + timedelta(seconds=300)
                self.assertGreaterEqual(
                    as_utc(remind_at),
                    earliest,
                    f"提醒时间 {remind_at} 早于配置的提前量窗口（{earliest}）",
                )
                self.assertLessEqual(
                    as_utc(remind_at),
                    latest,
                    f"提醒时间 {remind_at} 晚于任务 due_at {due_at}",
                )
            finally:
                await delete_eval_user(session, user_id, thread_ids=[thread_id])


if __name__ == "__main__":
    unittest.main()
