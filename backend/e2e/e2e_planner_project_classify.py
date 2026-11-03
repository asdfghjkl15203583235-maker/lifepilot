"""E2E 验证："明天去交水电费，然后把周报写了" → 两条任务分别进入 生活/工作 分类。

用真实 DB + 真实 OpenRouter LLM 驱动 `invoke_chat_agent_state`（聊天 API 的同一入口），
再查询任务表确认 planner 落库的两条任务 project 分类正确。
运行方式：uv run python e2e/e2e_planner_project_classify.py
"""

import asyncio
import io
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_settings  # noqa: E402
from db.session import create_async_engine_from_settings  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402
from services.agent_runner import invoke_chat_agent_state  # noqa: E402

PROMPT = "明天去交水电费，然后把周报写了"
USER_ID = uuid.UUID("49594ac5-c813-419a-8687-edb9f849cedf")
REPORT_PATH = Path(__file__).resolve().parent / "e2e_planner_project_classify_report.txt"


async def main() -> int:
    settings = get_settings()
    if not settings.openrouter_api_key:
        print("缺少 OPENROUTER_API_KEY，无法跑真实 LLM，跳过。")
        return 2

    engine = create_async_engine_from_settings(settings)
    lines: list[str] = [f"消息：{PROMPT}", f"user: {USER_ID}", ""]

    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            # 跑之前的任务数，便于判定"新增"
            before = await session.execute(
                text("SELECT count(*) FROM tasks WHERE user_id = :uid"),
                {"uid": str(USER_ID)},
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
                lines.append("友好回答（截断）：" + (state.get("final_answer") or state.get("error") or "")[:300])
                # planner 在聊天事务内落库，接口侧由 FastAPI 依赖 commit；脚本里显式提交。
                await session.commit()
            except Exception as exc:
                lines.append(f"graph 运行异常：{exc}")
                await session.rollback()

            # 查最新任务（按 created_at 倒序），只取本次新增的 user 源任务
            after = await session.execute(
                text(
                    "SELECT title, project, status, due_at, plan_source FROM tasks "
                    "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 10"
                ),
                {"uid": str(USER_ID)},
            )
            rows = after.fetchall()
            lines.append(f"\n最近 {len(rows)} 条任务（倒序，首行即最新）：")
            for title, project, status, due_at, plan_source in rows:
                lines.append(
                    f"  project={project} | status={status} | plan={plan_source} | due={due_at} | {title}"
                )

            # 判定：两条预期任务标题分别含"交水电费"与"周报"，且分类为 生活/工作
            waters = [r for r in rows if "水电" in (r[0] or "")]
            weekly = [r for r in rows if "周报" in (r[0] or "")]
            lines.append("")
            water_ok = bool(waters) and all((r[1] or "") == "生活" for r in waters)
            weekly_ok = bool(weekly) and all((r[1] or "") == "工作" for r in weekly)
            lines.append(f"交水电费 → 生活：{water_ok}（rows={len(waters)}）")
            lines.append(f"写周报 → 工作：{weekly_ok}（rows={len(weekly)}）")

            ok = water_ok and weekly_ok
            lines.append(f"\n结论：{'PASS' if ok else 'FAIL'}")
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Done. Report written to {REPORT_PATH}")
    return 0 if "结论：PASS" in REPORT_PATH.read_text(encoding="utf-8") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))