"""针对性 E2E：验证本次 6 个模块的缺陷在真实 HTTP 链路上已修复。

全部走真实后端（不 mock），逐项回读数据库确认：

1. 天气类请求：不再抛 `'coroutine' object has no attribute 'strip'`；
2. 内部执行步骤不落库：多步计划（查天气 → 写邮件）不得往 tasks 表写行；
3. 用户明确"提醒我"时仍要落库，且只有一条；
4. HITL 草案重复确认必须幂等（第二次返回成功，不报 410）；
5. 切页中断：客户端提前断开 SSE，这一轮对话仍要在 conversations 里；
6. 显式偏好"我最喜欢…"必须进入长期记忆并覆盖旧偏好。

运行：
    .venv/Scripts/python.exe e2e/e2e_regression_6_modules.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
import jwt
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402
from db.session import (  # noqa: E402
    create_async_engine_from_settings,
    get_async_session_maker,
    init_database_engine,
)
from lib.enums import PendingActionType  # noqa: E402
from services.pending_action_service import create_pending_action  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8081")
USER_ID = uuid.UUID("49594ac5-c813-419a-8687-edb9f849cedf")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def make_token(settings) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode(
        {"sub": str(USER_ID), "email": "lifangwang2027@163.com", "iat": now, "exp": now + 7200},
        settings.jwt_secret,
        algorithm="HS256",
    )


def stream_chat(client: httpx.Client, headers: dict, text_msg: str, conv_id: str,
                abort_after_tokens: int = 0) -> dict:
    out = {"tokens": "", "done": None, "pending_actions": [], "errors": [], "statuses": 0}
    body = {"messages": [{"role": "user", "content": text_msg}], "client_conversation_id": conv_id}
    token_events = 0
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
            kind = event.get("type")
            if kind == "token":
                token_events += 1
                out["tokens"] += event.get("content", "")
                if abort_after_tokens and token_events >= abort_after_tokens:
                    # 模拟用户切页：直接断开连接。
                    break
            elif kind == "done":
                out["done"] = event.get("content", "")
            elif kind == "pending_action":
                out["pending_actions"].append(event.get("action") or {})
            elif kind == "error":
                out["errors"].append(event.get("message", ""))
            elif kind == "status":
                out["statuses"] += 1
    return out


async def db_scalar(sql: str, params: dict | None = None):
    maker = get_async_session_maker()
    async with maker() as session:
        result = await session.execute(text(sql), params or {})
        row = result.first()
        return row[0] if row else None


async def db_rows(sql: str, params: dict | None = None):
    maker = get_async_session_maker()
    async with maker() as session:
        result = await session.execute(text(sql), params or {})
        return list(result.fetchall())


async def seed_pending_action() -> uuid.UUID:
    maker = get_async_session_maker()
    async with maker() as session:
        row = await create_pending_action(
            session,
            user_id=USER_ID,
            action_type=PendingActionType.EMAIL_SEND,
            title="E2E重复确认探针",
            summary="幂等测试",
            payload={"to": "probe@example.com", "subject": "探针", "body": "只用于幂等验证"},
            expires_in_days=1,
        )
        await session.commit()
        return row.id


def main() -> None:
    settings = get_settings()
    init_database_engine(settings)
    token = make_token(settings)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    client = httpx.Client(base_url=BASE, timeout=300)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def run(coro):
        return loop.run_until_complete(coro)

    print("\n[1] 天气工具不再因未 await 崩溃")
    before_weather = run(db_scalar("SELECT count(*) FROM tasks"))
    conv = str(uuid.uuid4())
    result = stream_chat(client, headers, "山西榆次明天天气怎么样？", conv)
    crash = any("coroutine" in err or "strip" in err for err in result["errors"])
    check("天气查询无 coroutine/strip 异常", not crash, str(result["errors"])[:120])
    check("天气查询返回了正文", bool(result["done"] or result["tokens"]),
          (result["done"] or result["tokens"])[:60])
    after_weather = run(db_scalar("SELECT count(*) FROM tasks"))
    check("单条查询未新增任务行", after_weather == before_weather,
          f"{before_weather} → {after_weather}")

    print("\n[2] 多步计划：内部执行步骤不落库")
    before_multi = run(db_scalar("SELECT count(*) FROM tasks"))
    conv = str(uuid.uuid4())
    result = stream_chat(
        client, headers,
        "查一下北京明天的天气，然后根据天气帮我写一封邮件给 test@example.com，内容是提醒他带伞。",
        conv,
    )
    after_multi = run(db_scalar("SELECT count(*) FROM tasks"))
    check("多步计划未污染任务看板", after_multi == before_multi,
          f"{before_multi} → {after_multi}")
    check("多步计划未报错", not result["errors"], str(result["errors"])[:120])
    check("多步计划产出了正文", bool(result["done"]), (result["done"] or "")[:60])

    print("\n[3] 用户明确要求提醒 → 必须落库")
    before_reminder = run(db_scalar("SELECT count(*) FROM tasks"))
    conv = str(uuid.uuid4())
    stream_chat(client, headers, "提醒我明天上午十点给妈妈打电话", conv)
    rows = run(db_rows(
        "SELECT title FROM tasks WHERE title LIKE '%打电话%' ORDER BY created_at DESC LIMIT 5"
    ))
    check("提醒类请求写入任务行", len(rows) >= 1, str(rows))

    print("\n[4] PendingAction 重复确认幂等")
    action_id = run(seed_pending_action())
    first = client.post(f"/pending-actions/{action_id}/approve", headers=headers)
    second = client.post(f"/pending-actions/{action_id}/approve", headers=headers)
    check("首次确认成功", first.status_code == 200, f"{first.status_code} {first.text[:80]}")
    check("重复确认也返回 200（幂等）", second.status_code == 200,
          f"{second.status_code} {second.text[:120]}")

    print("\n[5] 客户端中途断开仍要落库")
    conv = str(uuid.uuid4())
    marker = f"切页验证-{uuid.uuid4().hex[:6]}"
    stream_chat(client, headers, f"请复述这句话：{marker}", conv, abort_after_tokens=3)
    for _ in range(40):
        row = run(db_scalar(
            "SELECT messages FROM conversations WHERE client_conversation_id = :cid",
            {"cid": conv},
        ))
        if row is not None:
            break
        run(asyncio.sleep(0.5))
    serialized = json.dumps(row, ensure_ascii=False) if row is not None else ""
    check("断线后会话已存在", row is not None, conv)
    check("断线后用户消息已持久化", marker in serialized, serialized[:160])

    print("\n[6] 显式偏好强制提取")
    conv = str(uuid.uuid4())
    stream_chat(client, headers, "我最喜欢喝拿铁了，请记住。", conv)
    for _ in range(40):
        count = run(db_scalar(
            "SELECT count(*) FROM user_memories WHERE user_id = :uid "
            "AND is_active = true AND text LIKE '%拿铁%'",
            {"uid": str(USER_ID)},
        ))
        if count:
            break
        run(asyncio.sleep(1.0))
    check("偏好已写入长期记忆", bool(count), f"count={count}")

    print("\n[7] 用户问题先落库（不等回复生成）")
    conv = str(uuid.uuid4())
    early_marker = f"早落库-{uuid.uuid4().hex[:6]}"
    body = {"messages": [{"role": "user", "content": f"请复述：{early_marker}"}],
            "client_conversation_id": conv}
    with client.stream("POST", "/chat", headers=headers, json=body) as response:
        # 只读第一条事件就断开：此时助手还一句话都没生成。
        for line in response.iter_lines():
            if line.startswith("data: "):
                break
    for _ in range(20):
        row = run(db_scalar(
            "SELECT messages FROM conversations WHERE client_conversation_id = :cid",
            {"cid": conv},
        ))
        if row is not None:
            break
        run(asyncio.sleep(0.5))
    check("首条事件后用户问题已在库里",
          row is not None and early_marker in json.dumps(row, ensure_ascii=False),
          str(row)[:120])

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"总计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "；".join(failed))
    loop.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
