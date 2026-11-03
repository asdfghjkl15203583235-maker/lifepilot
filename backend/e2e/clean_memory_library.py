"""模块四：记忆库智能清洗脚本（禁止全删）。

背景：记忆库里混入大量第一人称、祈使句和系统噪音，且同一事实因措辞差异反复
落库。本脚本对**现有数据**做一次清洗：
  1. 物理删除噪音记录（含"请记住/千万别忘了/Notion 活动/已调用接口"等）；
  2. 归一后合并近重复项，只保留一条 canonical；
  3. 其余第一人称记录统一改写为第三人称。

设计原则（禁止全删）：
  - 只有明确命中噪音模式的记录才删除，第一人称记录改写而非删除；
  - 默认 dry-run，只审计不写入；确认报告后再加 `--apply` 执行；
  - 每个删除项都带原因，报告逐条列出，可人工复核。

运行方式（先审计，确认后再执行）：

    uv run python e2e/clean_memory_library.py            # dry-run
    uv run python e2e/clean_memory_library.py --apply     # 真的物理删除
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import redis.asyncio as aioredis
from sqlalchemy import text

from config import get_settings
from db.session import create_async_engine_from_settings
from services.memory_noise_filter import is_transient_emotional_state
from services.memory_service import normalize_memory_text_for_storage

REPORT_PATH = Path(__file__).resolve().parent / "clean_memory_library_report.txt"

# 系统噪音：描述的是"系统/助手/工具做了什么"，不是"用户是什么样的"。
# 命中即物理删除——这些内容通过检索注入会持续污染后续所有对话。
_NOISE_PATTERNS = (
    (re.compile(r"请记住|记住[我咱]|千万别忘了|别忘了|帮我记[一下]?|记一下"), "祈使/命令框架"),
    (re.compile(r"notion\s*活动|notion\s*页面|已调用\s*notion|notion\s*接口|工作区.*(查看|更新)", re.IGNORECASE), "Notion/系统活动噪音"),
    (re.compile(r"已调用.{0,8}(接口|工具|api)|api\s*返回|接口返回|http\s*\d{3}", re.IGNORECASE), "接口/执行日志噪音"),
    (re.compile(r"已创建.{0,6}(notion|页面|数据库)|已更新.{0,6}(页面|数据库)", re.IGNORECASE), "系统执行痕迹"),
)


def is_system_noise(text: str) -> bool:
    """这条记忆是不是该物理删除的系统噪音/命令框架？"""
    if not text:
        return False
    return any(pattern.search(text) for pattern, _ in _NOISE_PATTERNS)


def noise_reason(text: str) -> str | None:
    if not text:
        return None
    for pattern, reason in _NOISE_PATTERNS:
        if pattern.search(text):
            return reason
    # 一次性情绪不是长期事实：「用户近期情绪低落」会通过检索注入，让模型对
    # 之后每一轮都"先安慰再说"，甚至在查天气时硬扯一句"心情不好喝杯拿铁"。
    if is_transient_emotional_state(text):
        return "一次性情绪状态噪音"
    return None


def normalize_to_third_person(text: str) -> str:
    """复用入库闸门的同一套归一逻辑，保证线上线下一致。"""
    return normalize_memory_text_for_storage(text)


def plan_cleanup(records: list[dict]) -> dict:
    """根据 active 记忆记录算出清洗计划（纯函数，可单测）。

    records: [{"id", "text", "category"}, ...]
    returns: {"delete": [{"id","text","原因"}], "rewrite": [{"id","text"}], "keep": [...]}
    """
    delete: list[dict] = []
    rewrite: list[dict] = []
    keep: list[dict] = []
    seen_normalized: dict[tuple[str, str], str] = {}
    obsolete_tokens: list[str] = []

    for record in records:
        rid = str(record.get("id"))
        raw = (record.get("text") or "").strip()
        category = record.get("category") or ""

        reason = noise_reason(raw)
        if reason:
            delete.append({"id": rid, "text": raw, "原因": reason})
            continue

        normalized = normalize_to_third_person(raw)
        # 备注里的旧值必须从文本里剥掉，而不是连新值一起删：
        # "用户的直属领导是李四（此前记录的张三已作废）" → "用户的直属领导是李四"。
        stripped, tokens = strip_note(normalized)
        obsolete_tokens.extend(tokens)
        final_text = stripped or normalized
        key = (category, final_text)
        if key in seen_normalized:
            delete.append({
                "id": rid,
                "text": raw,
                "原因": f"与 {seen_normalized[key]} 归一后重复，合并",
            })
            continue
        seen_normalized[key] = rid

        if final_text and final_text != raw:
            rewrite.append({"id": rid, "text": final_text})
            keep.append({"id": rid, "text": final_text})
        else:
            keep.append({"id": rid, "text": raw})

    # 备注点名作废的旧值，必须物理删除；否则新旧两条事实会同时被检索注入。
    keep, extra_deletes = _retire_superseded_values(keep, obsolete_tokens)
    delete.extend(extra_deletes)
    return {"delete": delete, "rewrite": rewrite, "keep": keep}


# 作废备注：括号里的，以及句尾无括号的。
_NOTE_IN_PARENS_RE = re.compile(
    r"[（(]([^（()）]{0,60}?)[)）]"
)
_NOTE_MARKERS = (
    "作废", "已作废", "不算数", "不作数", "记错", "搞错", "说错",
    "此前记录", "之前记录", "原来的", "以前",
)
_NOTE_TOKEN_NOISE = (
    "此前记录", "之前记录", "以前记录", "已作废", "作废", "不算数", "不作数",
    "记错了", "记错", "搞错了", "搞错", "说错了", "说错", "原来的", "原来",
    "之前的", "之前", "以前的", "以前", "已", "的", "了", "那个",
)


def strip_note(text: str) -> tuple[str, list[str]]:
    """剥掉作废备注，并返回备注里被宣告作废的旧值。

    "用户的直属领导是李四（此前记录的张三已作废）。"
      → ("用户的直属领导是李四。", ["张三"])

    为什么不能整条删除：这条记录里**同时**装着新值和旧值。直接删掉会连"李四"
    一起丢掉，库里只剩错误的"张三"，用户下次问领导就更错了。
    """
    if not text:
        return text, []

    obsolete: list[str] = []

    def _take(match: re.Match[str]) -> str:
        inner = match.group(1)
        if not any(marker in inner for marker in _NOTE_MARKERS):
            return match.group(0)
        token = inner
        for noise in sorted(_NOTE_TOKEN_NOISE, key=len, reverse=True):
            token = token.replace(noise, " ")
        token = " ".join(token.split()).strip(" ，,。.;；:：")
        if 1 <= len(token) <= 20:
            obsolete.append(token)
        return ""

    cleaned = _NOTE_IN_PARENS_RE.sub(_take, text)
    cleaned = " ".join(cleaned.split())
    cleaned = re.sub(r"([，,；;])\s*([。.])", r"\2", cleaned)
    return cleaned, obsolete


def _retire_superseded_values(
    keep: list[dict],
    obsolete: list[str],
) -> tuple[list[dict], list[dict]]:
    """删掉被备注宣告作废的旧值。

    只按"备注里明确点名的旧值"删，不做维度相似度猜测——按维度锚点删会误伤
    "老板邮箱""求职回执""常用邮箱"这类完全无关的事实，把用户的真实记忆删光。
    """
    tokens = [token for token in obsolete if token]
    if not tokens:
        return keep, []

    retired: list[dict] = []
    survivors: list[dict] = []
    for item in keep:
        text = item["text"]
        # 未剥干净的备注行（理论上不会）不算"旧值残留"，避免误删新值。
        _, own_tokens = strip_note(text)
        if own_tokens:
            survivors.append(item)
            continue
        hit = next((token for token in tokens if token in text), None)
        if hit is None:
            survivors.append(item)
            continue
        retired.append({
            "id": item["id"],
            "text": text,
            "原因": f"已被更新的记录宣告作废（旧值：{hit}）",
        })
    return survivors, retired


async def _fetch_active(conn) -> list[dict]:
    res = await conn.execute(
        text(
            """
            SELECT id, user_id, text, category
            FROM user_memories
            WHERE is_active = TRUE
            ORDER BY user_id, created_at
            """
        )
    )
    return [
        {"id": row[0], "user_id": row[1], "text": row[2], "category": row[3]}
        for row in res.fetchall()
    ]


async def main() -> None:
    apply = "--apply" in sys.argv
    settings = get_settings()
    engine = create_async_engine_from_settings(settings)
    lines: list[str] = [f"模式: {'APPLY（物理写入）' if apply else 'DRY-RUN（只审计不写入）'}", ""]
    affected_users: set[str] = set()
    try:
        async with engine.connect() as conn:
            records = await _fetch_active(conn)
            lines.append(f"active 记忆总数: {len(records)}")

            by_user: dict[str, list[dict]] = {}
            for record in records:
                by_user.setdefault(str(record["user_id"]), []).append(record)

            total_delete = total_rewrite = 0
            for user_id, user_records in by_user.items():
                plan = plan_cleanup(user_records)
                if not (plan["delete"] or plan["rewrite"]):
                    continue
                affected_users.add(user_id)
                lines.append(f"\nuser {user_id}:")
                for item in plan["delete"]:
                    lines.append(f"  [删除] {item['id']} | {item['原因']} | {item['text']}")
                    total_delete += 1
                    if apply:
                        await conn.execute(
                            text("DELETE FROM user_memories WHERE id = :id"),
                            {"id": item["id"]},
                        )
                for item in plan["rewrite"]:
                    lines.append(f"  [改写] {item['id']} → {item['text']}")
                    total_rewrite += 1
                    if apply:
                        await conn.execute(
                            text("UPDATE user_memories SET text = :text WHERE id = :id"),
                            {"text": item["text"], "id": item["id"]},
                        )

            lines.append(
                f"\n{'已物理删除' if apply else '将物理删除'} {total_delete} 条；"
                f"{'已改写' if apply else '将改写'} {total_rewrite} 条"
            )
            if apply:
                await conn.commit()

            if apply and affected_users and settings.redis_url:
                redis = aioredis.from_url(settings.redis_url, decode_responses=True)
                cleared = 0
                try:
                    for user_id in affected_users:
                        # 检索缓存现在按 query 分片（memory:user:{id}:chunks:*），
                        # 必须扫前缀清除，否则旧值仍会被注入 30 分钟。
                        try:
                            keys = [key async for key in redis.scan_iter(match=f"memory:user:{user_id}:chunks:*")]
                            keys.append(f"memory:user:{user_id}:bio")
                            if keys:
                                cleared += await redis.delete(*keys)
                        except Exception:
                            pass
                    lines.append(f"清除 Redis 记忆缓存 keys: {cleared}")
                finally:
                    await redis.aclose()
    finally:
        await engine.dispose()

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Done ({'applied' if apply else 'dry-run'}). Report: {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
