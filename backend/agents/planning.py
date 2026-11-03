"""Small planning helpers shared by planner, supervisor, and graph routing.

Tasks in LifePilot plans carry `depends_on` as a list of step ids. `task_layers`
returns the same set grouped by dependency depth so the supervisor can feed one
parallel layer at a time, and `is_ready` tells whether a task's dependencies
have all completed.
"""

from typing import Any


def task_layers(tasks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not tasks:
        return []
    by_id = {str(t.get("id") or t.get("task_key") or ""): t for t in tasks}
    memo: dict[str, int] = {}

    def depth(task: dict[str, Any]) -> int:
        key = str(task.get("id") or task.get("task_key") or "")
        if key in memo:
            return memo[key]
        memo[key] = 0
        max_parent = 0
        for dep in task.get("depends_on") or []:
            parent = by_id.get(str(dep))
            if parent is not None:
                max_parent = max(max_parent, 1 + depth(parent))
        memo[key] = max_parent
        return max_parent

    layered: dict[int, list[dict[str, Any]]] = {}
    for task in tasks:
        layered.setdefault(depth(task), []).append(task)
    return [layered[depth] for depth in sorted(layered)]


def is_ready(task: dict[str, Any], completed_ids: set[str]) -> bool:
    return all(str(dep) in completed_ids for dep in (task.get("depends_on") or []))