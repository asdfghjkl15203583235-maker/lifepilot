"""E2E 验证（T2）：planner 把时间词剥进 due_at 后，direct_reply 仍必须建提醒。

复现原始 bug：用户说 "我待会儿还要把快递放回宿舍" → 走 direct 路由 →
planner 检测到时间线索，把 "待会儿" 剥进 due_at，intent 变成 "把快递放回宿舍"。
修复前：direct_reply 只读 intent，intent 里已无时间词 → 不建提醒，时间整条丢失。

断言（真实 DB，真实 LLM）：
  1. 生成一条标题含 "快递" 的 task，且 due_at 非空（≈ now+1h）；
  2. 生成一条 user_reminders，标题含 "快递"，remind_at ≈ 同一时刻；
  3. 最终回答里提到提醒/时间，而不是假装只是闲聊。

跑法：uv run python e2e/e2e_direct_due_at_reminder.py
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from config import get_settings  # noqa: E402
from db.session import create_async_engine_from_settings  # noqa: E402
from services.agent_runner import invoke_chat_agent_state  # noqa: E402

PROMPT = "我待会儿还要把快递放回宿舍"
USER_ID = uuid.UUID("49594ac5-c813-419a-8687-edb9f849cedf")
REPORT_PATH = Path(__file__).resolve().parent / "e2e_direct_due_at_reminder_report.txt"


async def main() -> int:
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    lines: list[str] = [f"消息：{PROMPT}", f"user: {USER_ID}", ""]
    ok = False
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            try:
                state = await invoke_chat_agent_state(
                    settings=settings,
                    session=session,
                    user_id=USER_ID,
                    message_payloads=[{"role": "user", "content": PROMPT}],
                    model=settings.openrouter_default_model,
                )
                answer = state.get("final_answer") or state.get("error") or ""
                lines.append("回答（截断）：" + answer[:300])
                await session.commit()
            except Exception as exc:
                lines.append(f"graph 运行异常：{exc}")
                await session.rollback()
                answer = ""

            rows = await session.execute(
                text(
                    "SELECT title, project, due_at FROM tasks "
                    "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 8"
                ),
                {"uid": str(USER_ID)},
            )
            tasks = rows.fetchall()
            lines.append("\n最近任务（倒序）：")
            for title, project, due_at in tasks:
                lines.append(f"  project={project} | due={due_at} | {title}")

            rems = await session.execute(
                text(
                    "SELECT title, remind_at, status FROM user_reminders "
                    "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 8"
                ),
                {"uid": str(USER_ID)},
            )
            reminders = rems.fetchall()
            lines.append("\n最近提醒（倒序）：")
            for title, remind_at, status in reminders:
                lines.append(f"  status={status} | remind_at={remind_at} | {title}")

            now = datetime.now(timezone.utc)
            task_hit = [t for t in tasks if "快递" in (t[0] or "")]
            rem_hit = [r for r in reminders if "快递" in (r[0] or "")]

            lines.append("")
            lines.append(f"任务命中「快递」：{len(task_hit)} 条")
            lines.append(f"提醒命中「快递」：{len(rem_hit)} 条")

            task_ok = bool(task_hit) and task_hit[0][2] is not None
            rem_ok = bool(rem_hit)
            window_ok = False
            if task_hit and task_hit[0][2] is not None:
                due = task_hit[0][2]
                due = due if due.tzinfo else due.replace(tzinfo=timezone.utc)
                window_ok = now - timedelta(minutes=30) <= due <= now + timedelta(hours=3)
                lines.append(f"任务 due={due}，落在 [now-30m, now+3h]: {window_ok}")
            answer_ok = any(k in answer for k in ("提醒", "安排", "记在", "到点"))

            ok = task_ok and rem_ok and window_ok and answer_ok
            lines.append(
                f"task_ok={task_ok} rem_ok={rem_ok} window_ok={window_ok} answer_ok={answer_ok}"
            )
            lines.append(f"\n结论：{'PASS' if ok else 'FAIL'}")
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Done. Report written to {REPORT_PATH}")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
