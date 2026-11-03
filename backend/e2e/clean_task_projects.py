"""一次性数据清洗：把历史写死为 project="LifePilot" 的任务按标题语义重分类。

Planner 此前落库时把 project 写死为 "LifePilot"，导致任务看板顶部分类按钮
只有一项。本次按标题语义把每行归到 工作/生活/学习/其他 之一，幂等且只
影响 project="LifePilot" 的行。运行方式：

    uv run python e2e/clean_task_projects.py
"""

import asyncio
import io
import sys
from pathlib import Path

from sqlalchemy import text

from config import get_settings
from db.session import create_async_engine_from_settings
from services.task_project_classifier import classify_task_project

REPORT_PATH = Path(__file__).resolve().parent / "clean_task_projects_report.txt"


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    lines: list[str] = []
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text("SELECT id, title FROM tasks WHERE project = 'LifePilot' ORDER BY created_at")
            )
            rows = list(res.fetchall())
            lines.append(f"待清洗 rows: {len(rows)}")
            if not rows:
                lines.append("无需清洗（无 project='LifePilot' 数据）")
            for row_id, title in rows:
                new_project = classify_task_project(title or "")
                await conn.execute(
                    text("UPDATE tasks SET project = :p WHERE id = :id"),
                    {"p": new_project, "id": row_id},
                )
                lines.append(f"{new_project} <- {title}")
            await conn.commit()
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    # 控制台受 Windows GBK 限制，报告里也是同样的行；这里只回显统计。
    print(f"Done. Report written to {REPORT_PATH}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # pragma: no cover
        print(f"清洗失败: {exc}", file=sys.stderr)
        raise