"""One-shot E2E smoke test for the pending-actions HITL flow against a real DB + running API.

This is NOT part of the unit suite. It exercises the HTTP router + auth + real Postgres
read/write path by creating a PendingAction row and approving it via the API.
Run manually: uv run python tests/e2e_pending_action_smoke.py
Requires the backend running on --port 8090 and a reachable Postgres.
"""

import asyncio
import os
import sys
import uuid

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402
from lib.enums import PendingActionType  # noqa: E402
from services.pending_action_service import create_pending_action  # noqa: E402

BASE_URL = "http://127.0.0.1:8090"
USER_ID = uuid.UUID("49594ac5-c813-419a-8687-edb9f849cedf")
TASK_ID = uuid.UUID("16eb4ff6-636b-4fbb-8c8d-806d8434c4a5")  # not_started task of USER_ID
# Hardcode the working JWT secret from .env so no env-loading surprises.
JWT_SECRET = "5940957016c6a3890eb4fb5f1446c3e15cdd8bb5433b4145eb938f685aa08884"


def make_token() -> str:
    import time

    import jwt

    payload = {
        "sub": str(USER_ID),
        "email": "lifangwang2027@163.com",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


async def create_draft_row() -> uuid.UUID:
    settings: Settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        row = await create_pending_action(
            session,
            user_id=USER_ID,
            action_type=PendingActionType.TASK_UPDATE,
            title="E2E 冒烟：把任务标记完成",
            summary="测试摘要",
            payload={
                "task_id": str(TASK_ID),
                "task_title": "E2E任务",
                "changes": {"status": "done"},
            },
            expires_in_days=1,
        )
        await session.commit()
        await engine.dispose()
        return row.id


async def main() -> None:
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        # 1) approve a nonexistent action → 404
        r1 = await client.post(f"{BASE_URL}/pending-actions/{uuid.uuid4()}/approve", headers=headers)
        print(f"[1] approve nonexistent → {r1.status_code} (expected 404)")

        # 2) create a real draft row in DB
        action_id = await create_draft_row()
        print(f"[2] created draft {action_id}")

        # 3) list pending actions → should include our row
        r3 = await client.get(f"{BASE_URL}/pending-actions", headers=headers)
        items = r3.json()
        ours = [i for i in items if i["id"] == str(action_id)]
        print(f"[3] list status={r3.status_code}, found our draft: {bool(ours)}")
        if ours:
            print(f"    draft status={ours[0]['status']} type={ours[0]['action_type']}")

        # 4) approve it → executes TASK_UPDATE, task becomes done
        r4 = await client.post(f"{BASE_URL}/pending-actions/{action_id}/approve", headers=headers)
        print(f"[4] approve status={r4.status_code}")
        if r4.status_code == 200:
            body = r4.json()
            print(f"    result status={body['result'].get('status')}")

        # 5) approve again → should be 410 (not approvable now)
        r5 = await client.post(f"{BASE_URL}/pending-actions/{action_id}/approve", headers=headers)
        print(f"[5] re-approve → {r5.status_code} (expected 410)")

        # 6) reject a fresh draft (create another for reject test)
        action_id2 = await create_draft_row()
        r6 = await client.post(f"{BASE_URL}/pending-actions/{action_id2}/reject", headers=headers)
        print(f"[6] reject → {r6.status_code} (expected 200)")


if __name__ == "__main__":
    asyncio.run(main())