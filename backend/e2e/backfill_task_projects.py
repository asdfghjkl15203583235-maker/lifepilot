"""一次性回填历史任务的空 project。

Planner 曾经在 LLM 没给出 `project` 时直接落空字符串，导致前端项目过滤按钮
漏掉这些任务（它们在"全部"里可见，切到任一项目就消失）。代码侧已改为
`validate_task_project(...) or classify_task_project(...)`，这里补历史数据。

运行：
    .venv/Scripts/python.exe e2e/backfill_task_projects.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402
from db.session import get_async_session_maker, init_database_engine  # noqa: E402
from services.task_project_classifier import classify_task_project  # noqa: E402

REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_task_projects_report.txt")


async def main() -> None:
    settings = get_settings()
    init_database_engine(settings)
    maker = get_async_session_maker()
    lines: list[str] = []

    async with maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, title FROM tasks "
                    "WHERE project IS NULL OR project = '' ORDER BY created_at"
                )
            )
        ).fetchall()

        for task_id, title in rows:
            project = classify_task_project(title or "")
            await session.execute(
                text("UPDATE tasks SET project = :p WHERE id = :i"),
                {"p": project, "i": task_id},
            )
            line = f"{project}\t{title}"
            lines.append(line)
            print(line)
        await session.commit()

    summary = f"回填 {len(rows)} 条空 project 任务"
    print("\n" + summary)
    with open(REPORT, "w", encoding="utf-8") as handle:
        handle.write(summary + "\n\n" + "\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
