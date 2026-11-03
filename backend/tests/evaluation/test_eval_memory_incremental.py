"""评测 3：记忆增量更新（Mem0 风格 ADD/UPDATE/DELETE/NOOP）。

输入 1「我喜欢喝拿铁」，输入 2「我其实更喜欢喝美式」后必须：
  - active 偏好里不存在「拿铁」（旧记忆被 UPDATE 软删）；
  - active 偏好里存在「美式」；
  - 旧「拿铁」行仍在库中但 is_active=false（证明是软删而非没写过）。
"""

import unittest
import uuid

from sqlalchemy import text

from tests.evaluation.helpers import (
    create_eval_user,
    delete_eval_user,
    eval_session,
    run_chat,
)


class MemoryIncrementalEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_preference_supersedes_old_one(self) -> None:
        thread_1 = f"eval-mem-1-{uuid.uuid4().hex[:12]}"
        thread_2 = f"eval-mem-2-{uuid.uuid4().hex[:12]}"
        async with eval_session(memory=True) as (settings, session):
            user_id = await create_eval_user(session)
            try:
                _state, answer_1 = await run_chat(
                    settings,
                    session,
                    user_id,
                    "我喜欢喝拿铁",
                    inline_memory_extraction=True,
                    thread_id=thread_1,
                )
                latte_active_1 = await _count_memories(session, user_id, "拿铁", active=True)
                self.assertGreaterEqual(
                    latte_active_1,
                    1,
                    f"第一轮没有写入「拿铁」偏好，无法验证增量更新。回答：{answer_1}",
                )

                _state, answer_2 = await run_chat(
                    settings,
                    session,
                    user_id,
                    "我其实更喜欢喝美式",
                    inline_memory_extraction=True,
                    thread_id=thread_2,
                )

                latte_active = await _count_memories(session, user_id, "拿铁", active=True)
                americano_active = await _count_memories(session, user_id, "美式", active=True)
                latte_inactive = await _count_memories(session, user_id, "拿铁", active=False)

                self.assertEqual(
                    latte_active,
                    0,
                    f"旧「拿铁」记忆仍为 active（未 UPDATE 软删）。第二轮回答：{answer_2}",
                )
                self.assertGreaterEqual(
                    americano_active,
                    1,
                    f"未写入新「美式」偏好。第二轮回答：{answer_2}",
                )
                self.assertGreaterEqual(
                    latte_inactive,
                    1,
                    "没有找到被软删的「拿铁」行（可能被硬删或根本没写入）",
                )
            finally:
                await delete_eval_user(session, user_id, thread_ids=[thread_1, thread_2])


async def _count_memories(session, user_id, keyword: str, *, active: bool) -> int:
    result = await session.execute(
        text(
            "SELECT count(*) FROM user_memories "
            "WHERE user_id = :uid AND is_active = :active AND text LIKE :pattern"
        ),
        {"uid": user_id, "active": active, "pattern": f"%{keyword}%"},
    )
    return int(result.scalar_one())


if __name__ == "__main__":
    unittest.main()
