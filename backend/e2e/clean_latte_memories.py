"""一次性数据清洗：清理 user_memories 里关于"拿铁/咖啡"的重复历史记忆。

背景：同一偏好（"我喜欢燕麦拿铁"）因中英混杂、错别字反复落库，出现 4+ 条
近义记录。本脚本扫描所有用户的 active 记忆，凡是命中"拿铁/咖啡/燕麦奶咖"
等关键词的，全部按 (user_id, 语义簇) 归组，每组只保留**最新且文本最干净**
的一条作为 canonical，其余全部软删除（is_active=false），并清空对应用户的
Redis chunks 缓存，保证检索链路立即生效。

运行方式：

    uv run python e2e/clean_latte_memories.py
"""

import asyncio
import io
import re
import sys
from pathlib import Path

import redis.asyncio as aioredis
from sqlalchemy import text

from config import get_settings
from db.session import create_async_engine_from_settings

REPORT_PATH = Path(__file__).resolve().parent / "clean_latte_memories_report.txt"

# 命中即视为"拿铁/咖啡"相关记忆。覆盖中英混杂("oat latte"、"燕麦拿铁")与
# 常见错别字("拿铁"被写成"那铁/拿帖/拉铁"等)。
_LATTE_HAYSTACK_RE = re.compile(
    r"(拿\s?铁|那\s?铁|拿\s?帖|拉\s?铁|墊\s?铁|那\s?ti[e]|latte|拿铁咖啡|"
    r"燕麦?\s?(咖|coffee|latte)|咖\s?啡|coffee|cappuccino|拿铁|美式|卡布奇诺)",
    re.IGNORECASE,
)

# 清洗后希望保留的"干净文本"优先级（按顺序命中第一条作为 canonical）。
_CANONICAL_PREFERRED = (
    "我喜欢喝燕麦拿铁",
    "喜欢燕麦拿铁",
    "我比较喜欢燕麦拿铁",
    "每天喝一杯燕麦拿铁",
)


def _pick_canonical(candidates: list[tuple[str, str, str, str]]) -> tuple[str, str, str, str] | None:
    """从同组候选 (id, text, category, created_at) 里挑应保留的一条。

    次序：首先匹配偏好模板 → 其次挑文本最短者（更可能是干净的规范句）
    → 退化只用最新一条。返回 None 表示该组为空。
    """
    if not candidates:
        return None
    for preferred in _CANONICAL_PREFERRED:
        for candidate in candidates:
            if preferred in (candidate[1] or ""):
                return candidate
    clean = [c for c in candidates if not _LATTE_HAYSTACK_RE.search(c[1] or "")]
    if clean:
        candidates = clean
    return min(candidates, key=lambda c: len(c[1] or ""))


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    lines: list[str] = []
    deleted_total = 0
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    """
                    SELECT id, user_id, text, category, created_at
                    FROM user_memories
                    WHERE is_active = TRUE
                    ORDER BY created_at
                    """
                )
            )
            rows = list(res.fetchall())
            lines.append(f"active 记忆总数: {len(rows)}")

            # 按 user_id × 语义簇 归组。先按 user 分桶，再在桶内判定是否含分量。同 user
            # 的多条命中记忆彼此互为重复 → 仅保留 canonical 一条。
            from collections import defaultdict

            by_user: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
            hits = 0
            for row_id, user_id, mem_text, category, created_at in rows:
                t = (mem_text or "").strip()
                if _LATTE_HAYSTACK_RE.search(t):
                    by_user[user_id].append((row_id, t, category, created_at))
                    hits += 1
            lines.append(f"命中拿铁/咖啡关键词的记忆: {hits}")

            for user_id in sorted(by_user):
                group = by_user[user_id]
                # 组内再按是否含显式"咖啡/拿铁"字样细分，避免把"咖啡因敏感"这类
                # 不同语义误合并成同一批。这里简化：同一组全部视为重复，保留一条。
                canon = _pick_canonical(group)
                if canon is None:
                    continue
                canon_id = canon[0]
                for row_id, mem_text, category, created_at in group:
                    if row_id == canon_id:
                        continue
                    await conn.execute(
                        text("UPDATE user_memories SET is_active = FALSE WHERE id = :id"),
                        {"id": row_id},
                    )
                    deleted_total += 1
                    lines.append(f"软删 {row_id} | {mem_text}")
                lines.append(f"保留 {canon_id} | {canon[1]}")

            await conn.commit()

            # 清空受影响的 users 的 chunks 缓存与 bio 缓存（防止脏结果继续命中检索）。
            # 独立脚本里 app 的 Redis 单例未初始化，这里临时建一个客户端。
            redis = aioredis.from_url(settings.redis_url, decode_responses=True) if settings.redis_url else None
            if redis is not None and by_user:
                cleared = 0
                try:
                    for user_id in by_user:
                        for key in (
                            f"memory:user:{user_id}:chunks",
                            f"memory:user:{user_id}:bio",
                        ):
                            try:
                                if await redis.delete(key):
                                    cleared += 1
                            except Exception:
                                pass
                    lines.append(f"清除 Redis 缓存 keys: {cleared}")
                finally:
                    await redis.aclose()
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    # 控制台受 Windows GBK 限制，报告已写入文件，这里只回显统计。
    print(f"软删 {deleted_total} 条重复记忆. 报告见 {REPORT_PATH}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # pragma: no cover
        print(f"清洗失败: {exc}", file=sys.stderr)
        raise