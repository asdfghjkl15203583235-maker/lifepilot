"""E2E 验证：模糊时间智能解析。

发送 "稍后帮我把水电费交了"，验证 planner 落库任务：
  1. project == "生活"
  2. due_at 非空（≈ 当前 +1 小时，模糊词"稍后"）
跑法：uv run python e2e/e2e_planner_fuzzy_time.py
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings  # noqa: E402
from db.session import create_async_engine_from_settings  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402
from services.agent_runner import invoke_chat_agent_state  # noqa: E402

PROMPT = "稍后帮我把水电费交了"
USER_ID = uuid.UUID("49594ac5-c813-419a-8687-edb9f849cedf")
REPORT_PATH = Path(__file__).resolve().parent / "e2e_planner_fuzzy_time_report.txt"


async def main() -> int:
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    lines: list[str] = [f"消息：{PROMPT}", f"user: {USER_ID}", ""]
    ok = False
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            before = await session.execute(
                text("SELECT count(*) FROM tasks WHERE user_id = :uid"), {"uid": str(USER_ID)}
            )
            count_before = before.scalar()
            lines.append(f"运行前任务数：{count_before}")

            try:
                state = await invoke_chat_agent_state(
                    settings=settings,
                    session=session,
                    user_id=USER_ID,
                    message_payloads=[{"role": "user", "content": PROMPT}],
                    model=settings.openrouter_default_model,
                )
                lines.append("回答（截断）：" + (state.get("final_answer") or state.get("error") or "")[:200])
                await session.commit()
            except Exception as exc:
                lines.append(f"graph 运行异常：{exc}")
                await session.rollback()

            # 取最新几条任务，找标题含"水电"的这条
            rows = await session.execute(
                text(
                    "SELECT title, project, status, due_at FROM tasks "
                    "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 5"
                ),
                {"uid": str(USER_ID)},
            )
            latest = rows.fetchall()
            lines.append("\n最近任务（倒序）：")
            for title, project, status, due_at in latest:
                lines.append(f"  project={project} | status={status} | due={due_at} | {title}")

            water = [r for r in latest if "水电" in (r[0] or "")]
            if not water:
                lines.append("\n未找到「水电」任务，FAIL")
                ok = False
            else:
                title, project, _, due_at = water[0]
                lines.append(f"\n水电任务: {title}")
                lines.append(f"  project={project} (期望 生活): {project == '生活'}")
                if due_at is None:
                    lines.append("  由于 due_at 为空，FAIL（模糊词必须有绝对时间戳）")
                    ok = False
                else:
                    due_dt = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    window_ok = now - timedelta(minutes=30) <= due_dt <= now + timedelta(hours=2)
                    lines.append(
                        f"  due={due_dt}，now={now}，落在 [now-30m, now+2h] 内: {window_ok}"
                    )
                    ok = (project == "生活") and window_ok
            lines.append(f"\n结论：{'PASS' if ok else 'FAIL'}")
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Done. Report written to {REPORT_PATH}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))