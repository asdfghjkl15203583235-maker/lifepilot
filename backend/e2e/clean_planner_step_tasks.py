"""一次性数据清洗：删除 Planner 内部执行步骤误落库产生的脏任务。

修复前 planner 会把每一个非 direct 步骤都写进 tasks 表，于是"查询天气""搜索
收件箱""撰写邮件"这类**图内部步骤**出现在用户的任务看板上，同一句"查询山西
榆次的天气情况"重复好几行。本次只删两类行：

1. 2026-10-28 那批榆次天气/TikTok 邮件的 planner 内部步骤（plan_id 以
   plan- 开头或 plan-yuci- 开头）；
2. planner 生成的只读执行步骤（查看/查询/搜索/总结…），它们只是图内部的
   取数手段，从来不该出现在看板上；
3. 明确是自动化测试造出来的行（E2E任务-/E2E确认-/E2E聊天改状态-/
   UI确认自动刷新-/刷新链路验证-/计时验证-/最终验证-/横幅验证-/
   页面计时-/侧栏刷新复现-，以及"将任务「…」标记为已完成"这类内部动作）。

用户自己创建或明确要求提醒的任务一律保留。运行方式：

    uv run python e2e/clean_planner_step_tasks.py
"""

import asyncio
import sys
from pathlib import Path

from sqlalchemy import bindparam, text

from config import get_settings
from db.session import create_async_engine_from_settings

REPORT_PATH = Path(__file__).resolve().parent / "clean_planner_step_tasks_report.txt"

# 自动化测试留下的标题前缀，全部是合成数据。
TEST_TITLE_PREFIXES = (
    "E2E任务-",
    "E2E确认-",
    "E2E聊天改状态-",
    "UI确认自动刷新-",
    "刷新链路验证-",
    "计时验证-",
    "最终验证-",
    "横幅验证-",
    "页面计时-",
    "侧栏刷新复现-",
)
# planner 内部动作：把某个已有任务改状态，本身不该成为看板上的一行。
INTERNAL_ACTION_PREFIXES = (
    "将任务「",
    "将待办事项「",
)
# planner 内部的只读步骤：查询/总结/搜索类动作只是执行手段，不是用户的待办。
# 仅在 plan_source='planner' 时套用，用户手写的任务不受影响。
READ_ONLY_STEP_PREFIXES = (
    "查看",
    "查询",
    "搜索",
    "总结",
    "浏览",
    "列出",
    "获取",
)


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    lines: list[str] = []
    deleted: list[str] = []
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, title, plan_id, plan_source FROM tasks ORDER BY created_at"
                    )
                )
            ).fetchall()

            targets: list[tuple] = []
            for row_id, title, plan_id, plan_source in rows:
                clean_title = (title or "").strip()
                if plan_id and (
                    plan_id.startswith("plan-")
                    or plan_id.startswith("plan-yuci-")
                ):
                    targets.append((row_id, clean_title, f"planner-internal:{plan_id}"))
                elif clean_title.startswith(TEST_TITLE_PREFIXES):
                    targets.append((row_id, clean_title, "test-artifact"))
                elif clean_title.startswith(INTERNAL_ACTION_PREFIXES):
                    targets.append((row_id, clean_title, "internal-action"))
                elif plan_source == "planner" and clean_title.startswith(
                    READ_ONLY_STEP_PREFIXES
                ):
                    targets.append((row_id, clean_title, "planner-read-only-step"))

            lines.append(f"tasks 总数: {len(rows)}")
            lines.append(f"待删除: {len(targets)}")
            if targets:
                await conn.execute(
                    text("DELETE FROM tasks WHERE id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": [item[0] for item in targets]},
                )
            for _, title, reason in targets:
                deleted.append(f"[{reason}] {title}")
            lines.extend(deleted)

            remaining = (
                await conn.execute(text("SELECT count(*) FROM tasks"))
            ).scalar_one()
            lines.append(f"删除后剩余: {remaining}")
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Done. Deleted {len(deleted)} rows. Report: {REPORT_PATH}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # pragma: no cover
        print(f"清洗失败: {exc}", file=sys.stderr)
        raise
