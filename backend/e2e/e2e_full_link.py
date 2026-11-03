"""全链路 E2E：真实 HTTP 打运行中的后端，覆盖认证 → 聊天编排 → 各代理路由 →
HITL 草案 → 任务/提醒/通知/会话/Notion 全流程。

不启动应用、不 mock：每一项都是真实落地到 Postgres/Redis/外部服务并回读验证。
运行：.venv/Scripts/python.exe e2e/e2e_full_link.py [--base http://127.0.0.1:8001]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402
from db.session import get_async_session_maker, init_database_engine  # noqa: E402
from lib.enums import PendingActionType  # noqa: E402
from services.pending_action_service import create_pending_action  # noqa: E402
from services.reminder_service import process_due_reminders  # noqa: E402

USER_ID = uuid.UUID("49594ac5-c813-419a-8687-edb9f849cedf")
RESULTS: list[tuple[str, bool, str]] = []

# One loop for the whole run: the asyncpg engine binds to the loop it was created
# on, so separate asyncio.run() calls would leave a dead proactor behind.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


def run_async(coro):
    return LOOP.run_until_complete(coro)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def make_token(settings) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(USER_ID), "email": "lifangwang2027@163.com", "iat": now, "exp": now + 7200},
        settings.jwt_secret,
        algorithm="HS256",
    )


async def seed_draft(action_type, payload, title) -> uuid.UUID:
    init_database_engine(get_settings())
    maker = get_async_session_maker()
    async with maker() as session:
        row = await create_pending_action(
            session, user_id=USER_ID, action_type=action_type, title=title,
            summary="E2E", payload=payload, expires_in_days=1,
        )
        await session.commit()
        return row.id


def stream_chat(client: httpx.Client, headers: dict, text: str, conv_id: str) -> dict:
    """POST /chat and collect every SSE event into a dict of type -> payload + text."""
    out = {"events": [], "tokens": "", "done": None, "pending_actions": [], "errors": []}
    body = {"messages": [{"role": "user", "content": text}], "client_conversation_id": conv_id}
    with client.stream("POST", "/chat", headers=headers, json=body) as response:
        if response.status_code != 200:
            out["errors"].append(f"HTTP {response.status_code}")
            return out
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            out["events"].append(event.get("type"))
            kind = event.get("type")
            if kind == "token":
                out["tokens"] += event.get("content", "")
            elif kind == "done":
                out["done"] = event.get("content", "")
            elif kind == "pending_action":
                out["pending_actions"].append(event.get("action") or {})
            elif kind == "error":
                out["errors"].append(event.get("message", ""))
    return out


def run(base: str) -> None:
    settings = get_settings()
    token = make_token(settings)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    client = httpx.Client(base_url=base, timeout=300)

    print("\n[1] 健康检查与认证")
    r = client.get("/health")
    check("GET /health 返回 200", r.status_code == 200, r.text[:80])
    r = client.get("/auth/me", headers=headers)
    check("GET /auth/me 识别当前用户", r.status_code == 200 and r.json().get("id") == str(USER_ID),
          f"{r.status_code} {r.text[:80]}")
    r = client.get("/tasks")
    check("未带 token 的 /tasks 返回 401", r.status_code == 401, str(r.status_code))
    r = client.get("/tasks", headers={"Authorization": "Bearer not-a-jwt"})
    check("伪造 token 的 /tasks 返回 401", r.status_code == 401, str(r.status_code))

    print("\n[2] 任务 CRUD 与扫描规划")
    title = f"E2E任务-{uuid.uuid4().hex[:6]}"
    due = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    r = client.post("/tasks", headers=headers, json={"title": title, "status": "not_started",
                                                     "project": "其他", "due_at": due})
    check("POST /tasks 创建任务", r.status_code == 201, f"{r.status_code} {r.text[:100]}")
    task = r.json() if r.status_code == 201 else {}
    task_id = task.get("id")

    r = client.get("/tasks", headers=headers)
    check("GET /tasks 能读回新任务", r.status_code == 200 and any(t["id"] == task_id for t in r.json()),
          str(r.status_code))

    r = client.get("/tasks/scan-plan", headers=headers)
    plan = r.json() if r.status_code == 200 else {}
    in_plan = any(item["id"] == task_id for bucket in ("upcoming", "ready", "overdue", "blocked")
                  for item in plan.get(bucket, []))
    check("GET /tasks/scan-plan 收录该任务", r.status_code == 200 and in_plan, str(plan.get("summary")))

    r = client.patch(f"/tasks/{task_id}", headers=headers, json={"status": "done"})
    check("PATCH /tasks 改状态", r.status_code == 200 and r.json().get("status") == "done", str(r.status_code))

    print("\n[3] HITL 草案：确认执行 / 拒绝 / 重复确认")
    probe_title = f"E2E确认-{uuid.uuid4().hex[:6]}"
    r = client.post("/tasks", headers=headers, json={"title": probe_title, "status": "not_started",
                                                     "project": "其他"})
    probe = r.json()
    action_id = run_async(seed_draft(
        PendingActionType.TASK_UPDATE,
        {"task_id": probe["id"], "task_title": probe_title, "changes": {"status": "done"}},
        f"把「{probe_title}」标记完成",
    ))
    r = client.get("/pending-actions", headers=headers)
    check("GET /pending-actions 列出草案",
          r.status_code == 200 and any(a["id"] == str(action_id) for a in r.json()), str(r.status_code))

    started = time.perf_counter()
    r = client.post(f"/pending-actions/{action_id}/approve", headers=headers)
    approve_ms = (time.perf_counter() - started) * 1000
    check("确认草案执行成功", r.status_code == 200 and r.json().get("result", {}).get("status") == "done",
          f"{r.status_code} {approve_ms:.0f}ms {r.text[:100]}")
    check("确认接口响应 < 1000ms", approve_ms < 1000, f"{approve_ms:.0f}ms")

    r = client.get("/tasks", headers=headers)
    refreshed = next((t for t in r.json() if t["id"] == probe["id"]), None)
    check("确认后立即读到已落库的新状态", refreshed is not None and refreshed["status"] == "done",
          refreshed["status"] if refreshed else "missing")

    r = client.get("/tasks/scan-plan", headers=headers)
    check("确认后 scan-plan 同步生效", r.status_code == 200, str(r.json().get("summary")))

    r = client.post(f"/pending-actions/{action_id}/approve", headers=headers)
    check("重复确认返回 410", r.status_code == 410, str(r.status_code))

    reject_id = run_async(seed_draft(
        PendingActionType.TASK_DELETE,
        {"task_id": probe["id"], "task_title": probe_title},
        f"删除「{probe_title}」",
    ))
    r = client.post(f"/pending-actions/{reject_id}/reject", headers=headers)
    check("拒绝草案返回成功", r.status_code == 200 and r.json().get("status") == "rejected", str(r.status_code))
    r = client.get("/tasks", headers=headers)
    check("拒绝后任务仍然存在", any(t["id"] == probe["id"] for t in r.json()), "task kept")

    print("\n[4] 聊天编排：意图路由")
    conv = f"e2e-{uuid.uuid4()}"
    r = stream_chat(client, headers, "你好，今天过得怎么样？", conv)
    check("闲聊走 direct 且返回正文", bool(r["done"]) and not r["errors"],
          f"done={len(r['done'] or '')}字 errors={r['errors']}")

    chat_title = f"E2E聊天改状态-{uuid.uuid4().hex[:6]}"
    r = client.post("/tasks", headers=headers, json={"title": chat_title, "status": "not_started",
                                                     "project": "其他"})
    chat_task = r.json() if r.status_code == 201 else {}
    chat_run = stream_chat(client, headers, f"把任务「{chat_title}」标记为已完成", conv)
    # 必须真的产出草案：曾因分类器把"任务"判给 notion，这句变成 "Searching
    # Notion..." 并回一句"没找到它"，任务纹丝不动、也没有草案。
    draft_types = [a.get("actionType") for a in chat_run["pending_actions"]]
    check("改任务生成 HITL 草案（不直接执行）", "task_update" in draft_types,
          f"drafts={draft_types} done={(chat_run['done'] or '')[:40]}")

    r = client.get("/tasks", headers=headers)
    before_approve = next((t for t in r.json() if t["id"] == chat_task.get("id")), None)
    check("草案未确认前任务状态不变",
          before_approve is not None and before_approve["status"] == "not_started",
          before_approve["status"] if before_approve else "missing")

    chat_draft_id = next(
        (a.get("id") for a in chat_run["pending_actions"] if a.get("actionType") == "task_update"),
        None,
    )
    if chat_draft_id:
        r = client.post(f"/pending-actions/{chat_draft_id}/approve", headers=headers)
        check("聊天草案可确认执行", r.status_code == 200, str(r.status_code))
        r = client.get("/tasks", headers=headers)
        after_chat = next((t for t in r.json() if t["id"] == chat_task.get("id")), None)
        check("确认后聊天任务变为 done",
              after_chat is not None and after_chat["status"] == "done",
              after_chat["status"] if after_chat else "missing")
    else:
        check("聊天草案可确认执行", False, "没有拿到 task_update 草案 id")

    r = stream_chat(client, headers, "今天天气怎么样？", conv)
    check("生活类查询返回内容", bool(r["done"]) and not r["errors"], f"{len(r['done'] or '')}字")

    print("\n[5] 会话持久化")
    r = client.post("/conversations", headers=headers,
                    json={"title": "E2E 会话", "messages": [{"role": "user", "content": "hello"}],
                          "client_conversation_id": conv})
    check("POST /conversations 持久化会话", r.status_code in (200, 201), str(r.status_code))
    conv_row = r.json() if r.status_code in (200, 201) else {}
    r = client.get(f"/conversations/{conv_row.get('id')}", headers=headers)
    check("GET /conversations/{id} 读回消息",
          r.status_code == 200 and len(r.json().get("messages", [])) >= 1, str(r.status_code))
    r = client.delete(f"/conversations/{conv_row.get('id')}", headers=headers)
    check("DELETE /conversations/{id} 删除会话", r.status_code in (200, 204), str(r.status_code))

    print("\n[6] 提醒与通知投递")
    remind_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    r = client.post("/reminders", headers=headers, json={"title": "E2E 提醒", "remind_at": remind_at})
    check("POST /reminders 创建提醒", r.status_code == 201, f"{r.status_code} {r.text[:100]}")
    reminder = r.json() if r.status_code == 201 else {}

    async def tick() -> int:
        maker = get_async_session_maker()
        async with maker() as session:
            delivered = await process_due_reminders(session, datetime.now(timezone.utc))
            await session.commit()
            return delivered

    delivered = run_async(tick())
    check("到期提醒被投递", delivered >= 1, f"delivered={delivered}")

    r = client.get("/notifications", headers=headers)
    has_alert = any(n.get("reminder_id") == reminder.get("id") for n in r.json())
    check("通知收件箱出现提醒卡片", r.status_code == 200 and has_alert, str(r.status_code))
    r = client.get("/notifications/unread-count", headers=headers)
    check("未读数接口可用", r.status_code == 200, r.text[:60])

    r = client.post(f"/reminders/{reminder.get('id')}/snooze", headers=headers, json={"minutes": 15})
    check("稍后提醒重新武装", r.status_code == 200 and r.json().get("status") == "pending", str(r.status_code))

    print("\n[7] 记忆与 Notion")
    r = client.get("/memory", headers=headers)
    check("GET /memory 可读", r.status_code == 200, str(r.status_code))
    r = client.get("/notion/status", headers=headers)
    check("GET /notion/status 可读", r.status_code == 200, r.text[:80])

    print("\n[8] 清理")
    r = client.delete(f"/tasks/{task_id}", headers=headers)
    check("DELETE /tasks 清理测试任务", r.status_code in (200, 204), str(r.status_code))
    if chat_task.get("id"):
        client.delete(f"/tasks/{chat_task['id']}", headers=headers)

    client.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'='*64}\n全链路测试：{passed}/{total} 通过")
    failures = [name for name, ok, _ in RESULTS if not ok]
    if failures:
        print("失败项：")
        for name in failures:
            print(f"  - {name}")
    print("=" * 64)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    run(args.base)
    sys.exit(0 if all(ok for _, ok, _ in RESULTS) else 1)
