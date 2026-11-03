"""Core memory operations — DB persistence, vector search, and Redis caching.

This module owns the full lifecycle of user memories and bio caching.
External code should call these functions rather than touching the DB or
Redis keys directly.
"""

import hashlib
import json
import logging
import re
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from lib.enums import MemoryCategory
from models.user_memory import UserMemory
from models.user_profile import UserProfile
from services.embedding_service import generate_embedding
from services.memory_noise_filter import is_transient_emotional_state
from services.redis_client import get_redis

logger = logging.getLogger(__name__)

# ── Third-person normalization gate ──────────────────────────────────────
# 抽取 Prompt 已写了 "THIRD-PERSON REWRITE — MANDATORY"，但 LLM 仍会偶发把第一
# 人称/祈使句原话存进 user_memories（"请记住我最喜欢喝拿铁"）。记忆一旦被污染，
# 会通过检索注入污染后续所有对话。这是确定性兜底：入库前强行掰正成第三人称。
_IMPERATIVE_FRAME_RE = re.compile(
    r"^(?:麻烦)?(?:请|帮我|帮忙|你要|你以后要|以后要)?"
    r"(?:务必|一定|千万|千万要|务必要)?"
    r"(?:记住|记一下|记录一下|记录下来|记得|别忘了|千万别忘了|要记得)"
    r"[:：，,]?\s*"
)
# "我们/咱们" 先归一，再处理单数"我"，避免 "我们" 被截成 "用户们"。
_WE_RE = re.compile(r"我们|咱们")
_MY_RE = re.compile(r"(?<!自)我的")
_I_RE = re.compile(r"(?<!自)我")


def normalize_memory_text_for_storage(text: str) -> str:
    """入库前把第一人称/祈使句记忆确定性归一为第三人称。

    "请记住我最喜欢喝拿铁" → "用户最喜欢喝拿铁"；"我的生日…" → "用户的生日…"。
    对已是第三人称的文本幂等（"用户喜欢…" 原样返回）。
    """
    if not text:
        return text
    result = text.strip()
    # 1) 剥掉命令框架（"请记住""千万别忘了""帮我记一下"…）。
    result = _IMPERATIVE_FRAME_RE.sub("", result).strip()
    result = result.lstrip("：:，,、 ").strip()
    # 2) 复数先归一，避免"我们"→"用户们"。
    result = _WE_RE.sub("用户", result)
    # 3) 所有格与单数主格；`(?<!自)` 避免误伤"自我"。
    result = _MY_RE.sub("用户的", result)
    result = _I_RE.sub("用户", result)
    return result


# ── Superseded-note stripping ────────────────────────────────────────────
# 真实 badcase：用户说"领导是李四（之前张三作废）"，抽取结果把整句存成
# "用户领导是李四（之前张三作废）"。旧值以"备注"形式留在记忆里，检索时
# 会把张三重新注入上下文——等于没改。备注必须在落库前剥掉。
_SUPERSEDED_NOTE_RE = re.compile(
    r"[（(][^（()）]{0,40}?"
    r"(?:作废|已作废|不算数|作数|之前|原来|以前|曾经|记错|搞错|说错|旧的|错误)"
    r"[^（()）]{0,40}?[)）]"
)
# 无括号的尾巴备注："，之前说的张三作废" / "（张三作废）" 已由上面处理。
_SUPERSEDED_TAIL_RE = re.compile(
    r"[，,。;；]?\s*(?:之前|原来|以前|旧)(?:说|讲|提|那个|的)?[^，,。;；]{0,20}?"
    r"(?:作废|不算数|不作数|记错了?|说错了?|搞错了?)\s*$"
)


def strip_superseded_notes(text: str) -> str:
    """剥掉"（之前张三作废）"这类备注，只保留新的正确事实。

    "用户领导是李四（之前张三作废）" → "用户领导是李四"。
    旧的错误值绝不能以备注形式留在记忆里，否则检索会把作废信息重新注入。
    """
    if not text:
        return text
    cleaned = _SUPERSEDED_NOTE_RE.sub("", text)
    cleaned = _SUPERSEDED_TAIL_RE.sub("", cleaned)
    return " ".join(cleaned.split()).strip()


# ── Pure-instruction gate ────────────────────────────────────────────────
# 用户说"请务必记住"时，抽取 LLM 偶尔会把整句指令当成记忆存下来。指令本身
# 没有信息量，落库只会污染检索。剥离命令框架后若几乎什么都不剩，直接丢弃。
_MEMORY_DIRECTIVE_WORDS: tuple[str, ...] = (
    "请务必记住",
    "请一定要记住",
    "千万别忘了",
    "千万不要忘了",
    "别忘记了",
    "别忘了",
    "帮我记住",
    "帮我记一下",
    "请记住",
    "记住",
    "记一下",
    "记得",
)
_MIN_MEANINGFUL_MEMORY_CHARS = 4
_NON_MEANINGFUL_CHARS_RE = re.compile(r"[\s，,。.！!？?、：:；;“”\"'（）()【】]+")


def is_pure_memory_instruction(text: str) -> bool:
    """文本是否只是"记住…"这类指令、其背后没有可保存的事实。

    "请务必记住" → True（剥完什么都不剩）；
    "请务必记住我对花生过敏" → False（剩下的是真正的事实）。
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    residue = stripped
    for word in sorted(_MEMORY_DIRECTIVE_WORDS, key=len, reverse=True):
        residue = residue.replace(word, " ")
    residue = _NON_MEANINGFUL_CHARS_RE.sub("", residue)
    return len(residue) < _MIN_MEANINGFUL_MEMORY_CHARS


# ── Redis key helpers ─────────────────────────────────────────────────────

BIO_CACHE_TTL_SECONDS = 86_400  # 24 hours
SESSION_IDLE_TIMEOUT_SECONDS = 1_800  # 30 minutes
TOP_MEMORY_CHUNKS = 8
MAX_INJECTED_TOKENS = 600
APPROX_CHARS_PER_TOKEN = 4  # conservative estimate for English text


def _bio_key(user_id: uuid.UUID) -> str:
    return f"memory:user:{user_id}:bio"


def _chunk_pattern(user_id: uuid.UUID) -> str:
    return f"memory:user:{user_id}:chunks:*"


def _chunks_key(user_id: uuid.UUID, query: str) -> str:
    """Per-query cache key.

    旧实现只按 user_id 缓存（`...:chunks`），于是用户第一次检索的结果会被复用
    给之后 30 分钟内的**每一次**提问。用户问"你记得我领导吗"时拿到的仍是上一轮
    查天气时缓存的 chunks —— 库里明明有"用户的直属领导是李四"，模型看到的却是
    天气记忆，只能回答"我没有存过"。命中率低一点无所谓，答错是致命的。
    """
    digest = hashlib.sha1((query or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"memory:user:{user_id}:chunks:{digest}"


def _session_key(user_id: uuid.UUID) -> str:
    return f"memory:user:{user_id}:session_id"


# ── Session management ────────────────────────────────────────────────────


async def check_or_start_session(user_id: uuid.UUID) -> bool:
    """Return True when a *new* session has started (caches should refresh).

    A session expires after SESSION_IDLE_TIMEOUT_SECONDS of inactivity.
    If no Redis is available we always treat the request as a fresh session
    so that data is fetched from Postgres on every first message.
    """
    redis_client = get_redis()
    if redis_client is None:
        return True

    current_session = await redis_client.get(_session_key(user_id))
    if current_session is not None:
        await redis_client.expire(_session_key(user_id), SESSION_IDLE_TIMEOUT_SECONDS)
        return False

    new_session_id = str(uuid.uuid4())
    await redis_client.set(
        _session_key(user_id), new_session_id, ex=SESSION_IDLE_TIMEOUT_SECONDS,
    )
    await _clear_chunk_cache(user_id)
    return True


async def _clear_chunk_cache(user_id: uuid.UUID) -> None:
    """Drop every per-query chunk cache entry for one user.

    缓存键按 query 分片（见 `_chunks_key`），所以失效必须扫前缀，不能只删一个键。
    """
    redis_client = get_redis()
    if redis_client is None:
        return
    pattern = _chunk_pattern(user_id)
    keys = [key async for key in redis_client.scan_iter(match=pattern)]
    if keys:
        await redis_client.delete(*keys)


# ── Bio retrieval (cache-first) ──────────────────────────────────────────


async def get_user_bio(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> str | None:
    """Fetch the user's bio, preferring the Redis cache."""
    redis_client = get_redis()
    if redis_client is not None:
        cached = await redis_client.get(_bio_key(user_id))
        if cached is not None:
            return cached

    result = await session.execute(
        select(UserProfile.bio).where(UserProfile.user_id == user_id),
    )
    bio = result.scalar_one_or_none()

    if redis_client is not None and bio:
        await redis_client.set(_bio_key(user_id), bio, ex=BIO_CACHE_TTL_SECONDS)

    return bio


# ── Memory retrieval (cache-first, pgvector fallback) ─────────────────────


# 向量检索的距离上限：0 = 完全相同，1 = 正交。超过这个值就是在"硬凑"，
# 把无关记忆塞进上下文只会让模型跑题（"心情不好先喝杯拿铁"就是这么来的）。
MAX_MEMORY_DISTANCE = 0.75


async def retrieve_memory(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    query: str,
) -> list[str]:
    """Return memory chunks relevant to *query*.

    三层兜底，任何一层都不能让用户看到"我没存过"：
      1. 向量检索（语义相近，带距离阈值，不硬凑无关记忆）；
      2. 关键词检索（"领导"这种字面词，向量挂了或阈值没命中时兜底）；
      3. Redis 缓存按 query 分片，避免上一轮的结果污染这一轮。
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return []

    redis_client = get_redis()
    cache_key = _chunks_key(user_id, clean_query)
    if redis_client is not None:
        cached = await redis_client.get(cache_key)
        if cached is not None:
            return json.loads(cached)

    chunks = await _semantic_memory_search(session, settings, user_id, clean_query)
    # 关键词结果永远参与合并，而不是只在向量检索返回空时才兜底。语义检索对
    # "我领导叫啥"这种追问常把天气记忆排到前面（都聊过），一旦非空就再也轮不到
    # 字面命中「领导」的那条 —— 用户就会听到"我没存过"。
    literal = await _keyword_memory_search(session, user_id, clean_query)
    merged: list[str] = []
    for text in (*chunks, *literal):
        if text and text not in merged:
            merged.append(text)
    chunks = merged[: TOP_MEMORY_CHUNKS * 2]

    # 一次性情绪永远不进上下文：它既不是事实，还会让模型对每一轮都"先安慰再说"。
    chunks = [text for text in chunks if not is_transient_emotional_state(text)]

    if redis_client is not None and chunks:
        await redis_client.set(
            cache_key, json.dumps(chunks, ensure_ascii=False),
            ex=SESSION_IDLE_TIMEOUT_SECONDS,
        )
    return chunks


async def _semantic_memory_search(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    query: str,
) -> list[str]:
    """Vector search. Embedding failures degrade to [] so keyword search runs."""
    try:
        query_embedding = await generate_embedding(query, model=settings.voyageai_model)
    except Exception as exc:
        logger.warning("Memory embedding failed, falling back to keyword search: %s", exc)
        return []

    try:
        distance = UserMemory.embedding.cosine_distance(query_embedding)
        result = await session.execute(
            select(UserMemory.text)
            .where(
                UserMemory.user_id == user_id,
                UserMemory.is_active.is_(True),
                distance < MAX_MEMORY_DISTANCE,
            )
            .order_by(distance)
            .limit(TOP_MEMORY_CHUNKS),
        )
        return [row[0] for row in result.all()]
    except Exception as exc:
        logger.warning("Memory vector search failed, falling back to keyword search: %s", exc)
        return []


def _query_terms(query: str) -> list[str]:
    """Split a query into CJK bigrams + ASCII words for LIKE matching."""
    terms: list[str] = [word for word in re.findall(r"[A-Za-z0-9_]{2,}", query)]
    cjk = "".join(re.findall(r"[一-鿿]", query))
    if len(cjk) == 1:
        terms.append(cjk)
    else:
        terms.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
    # 去重并保序；单字 bigram 噪音太大，只留 2 字以上。
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered[:12]


async def _keyword_memory_search(
    session: AsyncSession,
    user_id: uuid.UUID,
    query: str,
) -> list[str]:
    """Literal fallback: '领导' must find '用户的直属领导是李四' without embeddings."""
    terms = _query_terms(query)
    if not terms:
        return []
    result = await session.execute(
        select(UserMemory.text)
        .where(
            UserMemory.user_id == user_id,
            UserMemory.is_active.is_(True),
            or_(*(UserMemory.text.ilike(f"%{term}%") for term in terms)),
        )
        .order_by(UserMemory.created_at.desc())
        .limit(TOP_MEMORY_CHUNKS),
    )
    return [row[0] for row in result.all()]


# ── Memory insertion ──────────────────────────────────────────────────────


async def add_memory(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    text: str,
    category: MemoryCategory,
) -> UserMemory:
    """Embed *text* via VoyageAI, persist to Postgres, and append to Redis cache.

    Idempotent for exact-text repeats: if the same (user, normalized text,
    category) row is already active, return it without inserting a duplicate.
    Guards against the post-chat extraction chain saving the same fact every
    time the user mentions it (e.g. "我喜欢拿铁" across several days).

    Returns None when the text is a pure instruction ("请务必记住") with no
    fact behind it — callers must handle that case.
    """
    # 纯指令没有任何事实，落库只会污染后续检索，直接丢弃。
    if is_pure_memory_instruction(text):
        logger.info("Dropped pure memory instruction (no fact): %.60s", text)
        return None  # type: ignore[return-value]

    # 入库前确定性归一为第三人称：即使上游 LLM 发疯存了第一人称/祈使句，
    # 落库的文本也一定是"用户…"形式。
    text = normalize_memory_text_for_storage(text)
    # 旧值绝不能以备注形式留在记忆里（"领导是李四（之前张三作废）"）：检索会把
    # 作废的张三重新注入上下文，等于没改。
    text = strip_superseded_notes(text)
    if not text:
        return None  # type: ignore[return-value]

    existing = await _find_exact_active_memory(session, user_id, text, category.value)
    if existing is not None:
        logger.info("Skipped duplicate memory for user %s [%s]", user_id, category.value)
        return existing

    embedding = await generate_embedding(text, model=settings.voyageai_model)

    memory = UserMemory(
        user_id=user_id,
        text=text,
        embedding=embedding,
        category=category.value,
        is_active=True,
    )
    session.add(memory)
    await session.flush()
    await session.refresh(memory)

    redis_client = get_redis()
    if redis_client is not None:
        # 新记忆可能改变任意 query 的检索结果，整片失效而不是只改一个键。
        await _clear_chunk_cache(user_id)

    logger.info("Saved memory %s for user %s [%s]", memory.id, user_id, category.value)
    return memory


# ── Memory deduplication ──────────────────────────────────────────────────

DEDUP_SIMILARITY_THRESHOLD = 0.92


def _normalize_memory_text(text: str) -> str:
    """Whitespace-collapse for exact-dup matching (English/CJK safe)."""
    return " ".join(text.strip().split())


async def _find_exact_active_memory(
    session: AsyncSession,
    user_id: uuid.UUID,
    text: str,
    category_value: str,
) -> UserMemory | None:
    normalized = _normalize_memory_text(text)
    if not normalized:
        return None
    result = await session.execute(
        select(UserMemory).where(
            UserMemory.user_id == user_id,
            UserMemory.is_active.is_(True),
            UserMemory.category == category_value,
            UserMemory.text == normalized,
        )
        .order_by(UserMemory.created_at.desc())
        .limit(1),
    )
    return result.scalar_one_or_none()


async def deduplicate_memories(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """Soft-delete semantically duplicate memories for *user_id*.

    Keeps the most recent (first encountered when sorted desc by created_at)
    and deactivates older near-duplicates above the cosine threshold.
    Returns the count of deactivated rows.
    """
    result = await session.execute(
        select(UserMemory)
        .where(UserMemory.user_id == user_id, UserMemory.is_active.is_(True))
        .order_by(UserMemory.created_at.desc()),
    )
    memories = result.scalars().all()
    if len(memories) < 2:
        return 0

    ids_to_deactivate: set[uuid.UUID] = set()
    kept: list[UserMemory] = []

    for memory in memories:
        if memory.id in ids_to_deactivate:
            continue
        is_duplicate = False
        for representative in kept:
            similarity = _cosine_similarity(
                list(memory.embedding), list(representative.embedding),
            )
            if similarity > DEDUP_SIMILARITY_THRESHOLD:
                ids_to_deactivate.add(memory.id)
                is_duplicate = True
                break
        if not is_duplicate:
            kept.append(memory)

    if ids_to_deactivate:
        await session.execute(
            update(UserMemory)
            .where(UserMemory.id.in_(ids_to_deactivate))
            .values(is_active=False),
        )
        logger.info(
            "Deactivated %d duplicate memories for user %s",
            len(ids_to_deactivate), user_id,
        )

    return len(ids_to_deactivate)


# ── Similar-memory lookup (for incremental ADD/UPDATE/DELETE decisions) ─────


async def find_similar_memories(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    query: str,
    limit: int = 3,
) -> list[tuple[uuid.UUID, str]]:
    """Return (id, text) pairs for the most semantically similar active memories.

    Used by the post-chat decision step to pick which old memory an UPDATE or
    DELETE should target. Unlike ``retrieve_memory`` it returns ids, not just
    text, so the caller can soft-delete the specific row.
    """
    query_embedding = await generate_embedding(query, model=settings.voyageai_model)
    result = await session.execute(
        select(UserMemory.id, UserMemory.text)
        .where(UserMemory.user_id == user_id, UserMemory.is_active.is_(True))
        .order_by(UserMemory.embedding.cosine_distance(query_embedding))
        .limit(limit),
    )
    return [(row[0], row[1]) for row in result.all()]


async def soft_delete_memory(
    session: AsyncSession,
    user_id: uuid.UUID,
    memory_id: uuid.UUID,
) -> bool:
    """Mark a memory inactive (soft delete). Returns True if a row was updated."""
    result = await session.execute(
        update(UserMemory)
        .where(UserMemory.user_id == user_id, UserMemory.id == memory_id)
        .values(is_active=False),
    )
    deleted = result.rowcount is not None and result.rowcount > 0
    if deleted:
        # 只改库不清缓存 = 软删根本没生效：缓存里的旧值还会被注入 30 分钟，
        # 用户看到的就是"库里已删、AI 嘴里还有"。
        await _clear_chunk_cache(user_id)
    return deleted


# ── Cache invalidation ────────────────────────────────────────────────────


async def invalidate_bio_cache(user_id: uuid.UUID) -> None:
    redis_client = get_redis()
    if redis_client is not None:
        await redis_client.delete(_bio_key(user_id))


async def invalidate_chunks_cache(user_id: uuid.UUID) -> None:
    await _clear_chunk_cache(user_id)


# ── System prompt builder ─────────────────────────────────────────────────


def build_memory_prompt(bio: str | None, chunks: list[str]) -> str:
    """Format bio + memory chunks for injection into the system prompt.

    Respects MAX_INJECTED_TOKENS budget (~600 tokens). The header is not
    cosmetic: without it the model treats retrieved memory as conversation
    content and either denies having it ("我没有存过") or volunteers it in
    unrelated turns. Memory is silent background knowledge, used only when the
    user's question actually needs it.
    """
    max_chars = MAX_INJECTED_TOKENS * APPROX_CHARS_PER_TOKEN
    # 没有任何记忆时不要注入空标题：一段"你有长期记忆"的废话只会让模型
    # 假装记得，或者反过来主动说"我记得你…"。
    chunks = [text for text in (chunks or []) if text and text.strip()]
    if not bio and not chunks:
        return ""

    parts: list[str] = ["[LONG-TERM MEMORY — background knowledge about this user]"]
    parts.append(
        "以下是你此前记录的用户信息。它们是事实，不是猜测：\n"
        "- 用户问\"你知道/记得…吗\"时，先看这里；其中有答案就直接回答，"
        "**绝不允许**说\"我没有存过\"\"我不记得\"。\n"
        "- 与此轮问题无关的记忆保持沉默，不要主动提及、不要据此改变语气或加安慰。\n"
        "- 若两条记忆相互矛盾（例如同一事实有两个值），优先采用更新的那条，"
        "并且不要复述另一个旧值。"
    )
    used = len(parts[0]) + len(parts[1])

    if bio:
        bio_section = f"[USER BIO]\n{bio}"
        used += len(bio_section)
        parts.append(bio_section)

    if chunks:
        parts.append("[RELEVANT MEMORY]")
        used += len("[RELEVANT MEMORY]") + 1
        for chunk in chunks:
            candidate = f"- {chunk}"
            if used + len(candidate) > max_chars:
                break
            parts.append(candidate)
            used += len(candidate) + 1

    return "\n".join(parts)


# ── Planner rules ──────────────────────────────────────────────────────────


async def get_planner_rules(
    session: AsyncSession,
    user_id: uuid.UUID,
    limit: int = 20,
) -> list[str]:
    """Return active 'rule'-category memory texts for planner prompt injection."""
    result = await session.execute(
        select(UserMemory.text)
        .where(
            UserMemory.user_id == user_id,
            UserMemory.is_active.is_(True),
            UserMemory.category == MemoryCategory.RULE.value,
        )
        .order_by(UserMemory.created_at.desc())
        .limit(limit),
    )
    return [row[0] for row in result.all()]


# ── Internal helpers ──────────────────────────────────────────────────────


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
