"""真实链路验证：记忆检索必须能召回「用户的直属领导是李四」。

不复用任何 mock：直连 Postgres + 真实 voyageai key，验证"用户问领导"时
retrieve_memory 是否真的把那条记忆带回来。向量服务失败时也要靠关键词兜底
召回，这正是 Bug 1 的验收点。
"""

import asyncio
import sys
import uuid

sys.path.insert(0, ".")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import get_settings
from db.session import create_async_engine_from_settings
from services import memory_service
from services.embedding_service import init_embedding_client


async def main() -> None:
    settings = get_settings()
    init_embedding_client(settings)
    engine = create_async_engine_from_settings(settings)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT user_id FROM user_memories "
                    "WHERE is_active = TRUE AND text LIKE '%领导%' LIMIT 1"
                )
            )
        ).first()
        if row is None:
            print("NO_LEADER_MEMORY")
            return
        user_id = row[0]
        print(f"user={user_id}")

        for query in (
            "你知道我那个领导嘛？",
            "你记得他叫啥么？",
            "我领导叫什么名字",
        ):
            chunks = await memory_service.retrieve_memory(
                session, settings, user_id, query,
            )
            hit = [c for c in chunks if "领导" in c]
            print(f"\nQ: {query}")
            print(f"  召回 {len(chunks)} 条，其中含「领导」{len(hit)} 条")
            for item in hit:
                print(f"  HIT: {item}")
            if not hit:
                print("  MISS —— 仍然答不出领导")

        # 无关查询不应把情绪噪音带回来
        noise = await memory_service.retrieve_memory(
            session, settings, user_id, "查一下火星明天的天气",
        )
        emotional = [c for c in noise if "情绪" in c or "压力" in c]
        print(f"\n无效情绪记忆混入: {len(emotional)} 条 (应为 0)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
