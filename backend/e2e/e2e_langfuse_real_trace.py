"""Real end-to-end verification: one chat turn → Langfuse cloud trace → fetch it back.

Runs the real graph (real DeepSeek API + real Postgres), waits for the exporter to
flush, then reads the trace back from the Langfuse public API so the result is
verified from Langfuse's side, not from our own logs.

NOTE: `/api/public/traces` and `/api/public/observations` were removed (HTTP 410);
the supported listing endpoint is `/api/public/v2/observations`. Its `fields=`
param is a whitelist projection that does NOT include `name`/`sessionId`/`userId`,
so the request deliberately omits `fields` to get those columns.

Usage: .venv/Scripts/python.exe e2e/e2e_langfuse_real_trace.py
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# psycopg's async driver refuses the Windows ProactorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

USER_MESSAGE = "你好"


def _api_get(path: str, params: dict | None = None) -> dict:
    public_key = os.environ["LANGFUSE_PUBLIC_KEY"].strip().strip('"')
    secret_key = os.environ["LANGFUSE_SECRET_KEY"].strip().strip('"')
    base = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/")
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    request = urllib.request.Request(
        f"{base}{path}{query}",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


async def main() -> None:
    from langfuse import get_client

    from config import get_settings
    from services.langfuse_tracing import _export_settings_to_env
    from tests.evaluation.helpers import (
        create_eval_user,
        delete_eval_user,
        eval_session,
        run_chat,
    )

    settings = get_settings()
    _export_settings_to_env(settings)
    client = get_client()
    print("langfuse auth_check:", client.auth_check())

    thread_id = "langfuse-verify-conversation"
    async with eval_session() as (settings, session):
        user_id = await create_eval_user(session)
        try:
            state, answer = await run_chat(
                settings, session, user_id, USER_MESSAGE, thread_id=thread_id
            )
            print("user   :", USER_MESSAGE)
            print("answer :", (answer or "<empty>")[:200])
            print("error  :", state.get("error"))
        finally:
            client.flush()
            await asyncio.sleep(3)
            client.flush()
            await delete_eval_user(session, user_id, thread_ids=[thread_id])
            print("eval user id:", user_id)

    print("\n--- fetching traces from Langfuse API ---")
    now = dt.datetime.now(dt.timezone.utc)
    params = {
        "size": 50,
        "fromStartTime": (now - dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toStartTime": (now + dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Export is asynchronous: the span can land a few seconds after flush().
    found, rows = None, []
    for attempt in range(10):
        rows = _api_get("/api/public/v2/observations", params).get("data") or []
        roots = [r for r in rows if r.get("parentObservationId") is None]
        found = next((r for r in roots if r.get("sessionId") == thread_id), None)
        if found is not None:
            break
        print(f"  (attempt {attempt + 1}: {len(roots)} roots, not yet visible)")
        await asyncio.sleep(3)
    if found is None:
        print(f"\n(no root observation matched session {thread_id})")
        return

    trace_id = found["traceId"]
    print(f"\n=== MATCHED TRACE {trace_id} ===")
    print("root name      :", found.get("name"))
    print("root userId    :", found.get("userId"))
    print("root sessionId :", found.get("sessionId"))
    siblings = [r for r in rows if r.get("traceId") == trace_id]
    print(f"observations   : {len(siblings)}")
    for obs in siblings:
        print(f"  - [{obs.get('type')}] {obs.get('name')}")
    print("\nUI URL:", f"https://cloud.langfuse.com/trace/{trace_id}")


if __name__ == "__main__":
    asyncio.run(main())
