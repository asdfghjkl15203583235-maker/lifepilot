"""真实 HTTP 多轮对话验证：上下文隔离（Module 3）。

同一会话里连续三轮不同话题，检查：
1. 每轮回复只回应本轮问题，不复述上一轮的邮件/天气；
2. 回复里不出现工具原始输出（JSON / "以下是已读取到的邮件正文" 等）；
3. 第三轮简单问题（1+1）回复应极短。

运行：
    .venv/Scripts/python.exe e2e/e2e_context_isolation.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import httpx
import jwt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8081")
USER_ID = uuid.UUID("49594ac5-c813-419a-8687-edb9f849cedf")

RESULTS: list[tuple[str, bool, str]] = []

# 工具原始输出泄漏的典型指纹
LEAK_MARKERS = (
    "以下是已读取到的邮件正文",
    "tool_call",
    "tool_calls",
    "\"status\":",
    "{\"ok\"",
    "{\"success\"",
    "Traceback",
)


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


def stream_chat(client: httpx.Client, headers: dict, text_msg: str, conv_id: str) -> str:
    out = {"tokens": "", "done": None, "errors": []}
    body = {"messages": [{"role": "user", "content": text_msg}], "client_conversation_id": conv_id}
    with client.stream("POST", "/chat", headers=headers, json=body) as response:
        if response.status_code != 200:
            out["errors"].append(f"HTTP {response.status_code}")
            return "".join(out["errors"])
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "token":
                out["tokens"] += event.get("content", "")
            elif kind == "done":
                out["done"] = event.get("content", "")
            elif kind == "error":
                out["errors"].append(event.get("message", ""))
    return out["done"] or out["tokens"] + (f"\nERR:{out['errors']}" if out["errors"] else "")


def main() -> None:
    settings = get_settings()
    token = make_token(settings)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    client = httpx.Client(base_url=BASE, timeout=300)
    conv = str(uuid.uuid4())

    print("\n[1] 第 1 轮：查邮件")
    r1 = stream_chat(client, headers, "帮我看看最近三天有没有新邮件", conv)
    print(f"  → {r1[:300]}")
    check("第 1 轮有回复", bool(r1.strip()), r1[:80])
    check("第 1 轮无工具原始输出泄漏",
          not any(m in r1 for m in LEAK_MARKERS),
          next((m for m in LEAK_MARKERS if m in r1), ""))

    print("\n[2] 第 2 轮：查天气（不得复述邮件）")
    r2 = stream_chat(client, headers, "上海明天天气怎么样？", conv)
    print(f"  → {r2[:300]}")
    check("第 2 轮有回复", bool(r2.strip()), r2[:80])
    check("第 2 轮不复述上一轮邮件",
          not any(w in r2 for w in ("邮件", "收件箱", "inbox", "未读")),
          next((w for w in ("邮件", "收件箱", "inbox", "未读") if w in r2), ""))
    check("第 2 轮无「另外补充」式串场",
          "另外补充" not in r2 and "顺便提" not in r2,
          r2[:120] if ("另外补充" in r2 or "顺便提" in r2) else "")

    print("\n[3] 第 3 轮：1+1（必须极短，且不串场）")
    r3 = stream_chat(client, headers, "1+1 等于几？", conv)
    print(f"  → {r3[:300]}")
    check("第 3 轮答案正确", "2" in r3, r3[:80])
    check("第 3 轮不复述天气/邮件",
          not any(w in r3 for w in ("邮件", "收件箱", "天气", "气温", "降水")),
          next((w for w in ("邮件", "收件箱", "天气", "气温", "降水") if w in r3), ""))
    check("第 3 轮长度克制", len(r3.strip()) <= 40, f"len={len(r3.strip())}")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"总计 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "；".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
