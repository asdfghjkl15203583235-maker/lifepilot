"""Post-response memory extraction — runs after the user receives the assistant reply.

Keeping this off the LangGraph critical path avoids an extra LLM round-trip blocking HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from agents.tools.memory_tool import add_memory_tool
from config import Settings, chat_llm
from services import memory_noise_filter
from services.memory_noise_filter import correct_habit_category, is_one_off_log
from services.memory_service import (
    find_similar_memories,
    soft_delete_memory,
    strip_superseded_notes,
)

logger = logging.getLogger(__name__)

# Route decision → LLM once per fact. Lightweight call; fails open to a plain ADD.
MEMORY_DECISION_SIMILAR_MEMORIES = 3

MEMORY_EXTRACTION_SYSTEM_PROMPT = """\
You are a memory extraction agent for a personal assistant called Chief of Staff.
Analyze the conversation and identify any new, stable facts about the user worth
remembering long-term.

For each fact, return a JSON array:
[
    {"text": "<1-2 sentence fact>", "category": "<category>"}
]

Categories:
- fact: a stable, general fact about the user (background, habits, circumstances)
- rule: a persistent instruction/rule the user gives the assistant to follow
  ("always remind me X", "never do Y", "in meetings always Z")
- identity: name, location, role, personal details
- preference: communication style, format preferences, likes/dislikes
- goal: active goals, ongoing projects, aspirations
- work: job details, team, company, responsibilities
- episodic: notable events, experiences, interactions

Rules:
- Only extract stable, meaningful facts — not one-off questions or greetings.
- Keep each fact to 1-2 sentences maximum.
- If there are no new facts worth remembering, return an empty array: []
- Do NOT extract facts already present in the [USER BIO] or [RELEVANT MEMORY] \
sections of the conversation (those are already saved).
- Rules explicitly and persistently expressed ("以后帮我……", "记得每次都……") \
MUST be categorized as "rule" so the planner can enforce them later.

DURABLE-ONLY GATE — MANDATORY, HIGHEST PRIORITY:
- Only extract durable facts, preferences, and rules. NEVER extract imperative \
phrases like "请务必记住" or "千万别忘了" as memory content. Those words are the \
user asking you to remember, not the thing to remember.
- If the user's message is purely an instruction to remember something, extract \
the FACT behind it, not the instruction itself. \
"请务必记住我过敏不能吃花生" → text "用户对花生过敏", NOT "请务必记住我过敏不能吃花生".
- An extraction whose "text" still contains 请记住 / 请务必记住 / 千万别忘了 / \
记住 / 别忘了 / 记得 is a hard failure. Strip the command and keep only the fact; \
if nothing factual remains, return nothing for that phrase.

FORBIDDEN TO SAVE (single-occurrence items) — these are NOT memories:
- ONE-OFF TASKS / ERRANDS with a specific time: "明天早上八点改简历"、\
"待会去拿奶茶"、"今天九点参加腾讯会议"、"下午取快递"。These are reminders/\
scheduled tasks, not long-term memory. Never store them, even as "episodic".
- SCHEDULED EVENTS / MEETINGS / DATES with a fixed time or date, unless they reveal\
a lasting preference, identity fact, or rule ("我每天都在XX开会" IS a fact;\
"明天下午3点参加XX会议" is NOT).
- MOMENTARY TRANSACTIONS: "今天抢到了免单"、"点了杯拿铁"。A purchase/one-time\
action is not a memory; only a REPEATED habit is ("我每天喝燕麦拿铁" IS a pref).
- Transient state: "今天很忙"、"这周要赶项目"。Recurring conditions may be facts,\
single-day states must not be stored.
- "episodic" category should essentially never be used — a memory must help the \
assistant personalize across sessions for weeks/months, not capture yesterday's log.
- CATEGORY RULE FOR HABITS (mandatory): a lasting personal habit or recurring \
leisure behavior is a "preference", NEVER "episodic". "爱玩手机"、"喜欢打游戏"、\
"爱刷短视频"、"喜欢吃辣"、"每天熬夜打游戏" all describe who the user *is* — \
classify them as "preference". "episodic" is only for a single dated event \
("昨天参加了毕业典礼"), and single dated events are usually not worth saving at all.
- When a sentence describes how the user usually spends time or what they \
habitually do, prefer "preference" over "fact". The difference: "爱玩手机" → \
preference; "我住在晋中学院" → identity.
- When in doubt, prefer NOT saving: false positives pollute retrieval and dedup far \
more than a missed trivial fact.
- **EXPLICIT PREFERENCE — MANDATORY, HIGHEST PRIORITY**: whenever the user states \
a preference about themselves in the first person ("我最喜欢…", "我最爱…", \
"我更偏好…", "我更喜欢…", "我不喜欢…", "I love/prefer…"), you MUST extract it \
and categorize it as "preference". This rule overrides the "when in doubt, do not \
save" guidance — an explicit first-person preference is NEVER a one-off log, even \
if it mentions a specific item. Dropping it is a hard failure.
- A newly stated explicit preference SUPERSEDES an older preference in the same \
domain (e.g. previously "最喜欢拿铁", now "现在更爱美式") — extract the new one; \
the memory decision step will retire the stale one.

EMOTIONAL STATE — NEVER A LONG-TERM MEMORY:- NEVER extract transient or one-off emotional states: "今天心情崩溃"、\
"近期压力很大"、"最近情绪低落"、"这周好焦虑"、"我很难过" are momentary feelings, \
not facts about the user. Saving them makes the assistant treat the user as \
permanently distressed on every later turn.
- Only a RECURRING, long-standing trait may be kept, and only when the user \
states it as a pattern ("我长期焦虑，一直在看医生"、"我压力大的时候习惯去跑步"). \
A single bad day is never a memory.
- If the user is distressed, the correct behavior is to respond with empathy in \
the conversation — NOT to write "用户情绪低落" into memory.

SYSTEM NOISE — ABSOLUTELY FORBIDDEN TO SAVE:
- NEVER extract anything that describes the assistant's or a tool's internal
  execution: API calls, request/response logs, database or Notion/Google/Feishu
  operations, MCP tool invocations, agent/planner steps, token counts, retries,
  errors, latency, document/page IDs, or "已调用 XX 接口"/"已创建 Notion 页面"/
  "API 返回 200" style statements.
- These are execution traces, not facts about the user. If a sentence is about
  what the SYSTEM did (rather than what the USER is like), it is not a memory.
- Only the user's own message content can be a source of facts; the assistant's
  reply is context, never evidence.

THIRD-PERSON REWRITE — MANDATORY:
- Never copy the user's imperative/request wording verbatim. Rewrite every
  extracted fact as an objective third-person statement about the user.
- "请记住我喜欢喝拿铁" → "用户喜欢喝拿铁", NOT "请记住用户喜欢喝拿铁".
- Remove command frames entirely: "请记住"、"帮我记住"、"记一下"、"记住"、
  "别忘了"、"你以后要" are NOT part of the fact. Keep only the underlying fact.
- The same applies to assistant-facing instructions: "以后每次都提醒我开会"
  → text "用户希望每次开会前收到提醒"; keep category "rule".
- Any extracted "text" that still starts with or contains a second-person
  command frame ("请记住…", "帮我…", "你要…") is a hard failure and must be
  rewritten before output.

Language (MANDATORY — do not violate this):
- Output each extracted fact in the SAME primary language the user uses to write.
- This user primarily writes in Chinese, so every "text" you output MUST be \
simplified Chinese (简体中文).
- STRICTLY FORBIDDEN: mixing English words/sentences into the Chinese output \
(no "中英混杂"). Do NOT write "用户在杭州工作，prefers X". Write clean Chinese.
- Keep numeric/technical terms that have no natural Chinese equivalent, but \
otherwise write pure Chinese.
- This is a hard rule. Any output text that mixes languages or switches to \
English is a violation and must be rewritten in pure Chinese.
"""


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Parse a JSON array from LLM output, tolerating markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    parsed = json.loads(cleaned)
    if isinstance(parsed, list):
        return parsed
    return []


def is_explicit_preference(text: str) -> bool:
    """用户是否在第一人称明确表达了好恶（"我最喜欢喝拿铁"）。"""
    return memory_noise_filter.is_explicit_preference(text)


def _explicit_preference_sentences(message_payloads: list[dict[str, Any]]) -> list[str]:
    """从用户消息里挑出显式偏好句，供 LLM 漏抽时确定性补抽。"""
    found: list[str] = []
    for message in message_payloads:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for raw in re.split(r"[。！？!?\n]+", content):
            sentence = raw.strip()
            if sentence and is_explicit_preference(sentence) and sentence not in found:
                found.append(sentence[:120])
    return found


def _missing_explicit_preferences(
    facts: list[dict[str, Any]],
    message_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """LLM 漏掉的显式偏好 → 补成 preference 事实。

    "我最喜欢喝拿铁" 这种句子是用户对自己的定义，漏抽会让助手永远不知道用户
    的口味；而"更爱美式"能被抽到，就会出现新旧偏好只留一半的割裂状态。
    """
    known = " ".join(str(fact.get("text") or "") for fact in facts)
    extra: list[dict[str, Any]] = []
    for sentence in _explicit_preference_sentences(message_payloads):
        # 一句话可能包含多个偏好（"我最喜欢拿铁，不过现在更爱美式"），逐个分句
        # 判断是否已被 LLM 覆盖；只被覆盖一半时，另一半仍要补上。
        for clause in re.split(r"[，,、；;]", sentence):
            tokens = [
                token
                for token in re.split(
                    r"[和与的了吧呢啊]|喜欢|喜爱|偏好|偏爱|讨厌|最爱|更爱|我", clause
                )
                if len(token.strip()) >= 2
            ]
            # 只要实词里有一个已经被抽到，就认为这半句已覆盖（LLM 常改写措辞，
            # 不必逐字一致）；避免把同一偏好重复写两遍。
            if not tokens or any(token.strip() in known for token in tokens):
                continue
            extra.append({"text": clause.strip() or sentence, "category": "preference"})
    return extra


def _conversation_text_from_payloads(
    message_payloads: list[dict[str, Any]],
    final_answer: str,
) -> str:
    lines: list[str] = []
    for message in message_payloads:
        role = message.get("role", "")
        content = message.get("content", "")
        if isinstance(content, str) and content:
            lines.append(f"{role}: {content}")
    if final_answer.strip():
        lines.append(f"assistant: {final_answer}")
    return "\n".join(lines)


# Decision step (Mem0-style incremental memory). Given the new fact and the
# top-K similar existing memories, the LLM returns one of:
#   ADD    — insert as a brand-new memory
#   UPDATE — the new fact supersedes an old memory → soft-delete old + insert new
#   DELETE — the new fact invalidates an old memory → soft-delete old, no insert
#   NOOP   — full duplicate of an existing memory → skip entirely
MEMORY_DECISION_SYSTEM_PROMPT = """\
You maintain a user's long-term memory for a personal assistant.
A NEW fact was extracted from a conversation, and the assistant found up to \
{sampled} similar memories the user already has.

Decide which of these four actions best describes the relationship:
- ADD: the new fact is genuinely new and should be stored as-is.
- UPDATE: the new fact supersedes / replaces an older memory (e.g. the user \
used to like latte but now dislikes it, or previously preferred latte and now \
prefers americano, or the user corrects a fact: "不对，我的领导是李四" when the \
old memory says 张三). Pick the single old memory to replace by its id. That old \
memory will be soft-deleted and the new fact inserted. A correction ALWAYS \
supersedes: never answer ADD while leaving the outdated value active, and never \
keep the old value as a note inside the new text.
- DELETE: the new fact invalidates an older memory (it is now false / no longer \
applies) with nothing new to save in its place. Pick the single old memory to \
delete by its id. Nothing is inserted.
- NOOP: the new fact duplicates an existing memory (same information, trivial \
wording difference) — nothing to do.

Return STRICTLY a JSON object, no other text:
{{"action": "ADD"|"UPDATE"|"DELETE"|"NOOP", "target_memory_id": "<uuid or null>"}}

Rules:
- For UPDATE/DELETE you MUST set target_memory_id to the id of the memory being \
replaced/deleted, picked from the provided similar memories.
- For ADD/NOOP set target_memory_id to null.
- Only one action. No explanations. Pure JSON.
"""


_PREFERENCE_WORDS = (
    "喜欢", "偏好", "口味", "爱吃", "爱喝", "爱用", "钟爱",
    "最爱", "更爱", "更喜欢", "更偏好", "不喜欢", "讨厌",
)
# OpenRouter answers 402 "in_flight_budget_exhausted" with Retry-After when the
# account's concurrent-request budget is momentarily used up. Memory extraction
# runs right after the chat reply, i.e. exactly when those requests are still
# settling, so treat 402/429/5xx as retryable instead of losing the fact.
_TRANSIENT_STATUS_CODES = {402, 429, 500, 502, 503, 504}
_MEMORY_LLM_RETRIES = 3
_MEMORY_LLM_BACKOFF_SECONDS = 20.0


def _is_transient_llm_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status in _TRANSIENT_STATUS_CODES


async def _ainvoke_with_retry(llm: ChatOpenAI, messages: list[Any]) -> Any:
    delay = _MEMORY_LLM_BACKOFF_SECONDS
    for attempt in range(_MEMORY_LLM_RETRIES + 1):
        try:
            return await llm.ainvoke(messages)
        except Exception as exc:
            if attempt >= _MEMORY_LLM_RETRIES or not _is_transient_llm_error(exc):
                raise
            logger.warning(
                "Memory LLM transient error (status=%s); retrying in %.0fs",
                getattr(exc, "status_code", "?"),
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120.0)
_REPLACEMENT_WORDS = (
    "更喜欢", "改成", "换成", "改为", "不再", "现在喜欢", "其实喜欢",
    "转而", "改喝", "改吃", "改穿", "改用",
)
# 用户显式纠错："不对，我的领导是李四""记错了，我住浦东"。
# 这类输入必须让旧记录真正失效，而不是在新记忆里写一句"之前张三作废"。
_CORRECTION_WORDS = (
    "不对", "不是", "记错", "搞错", "说错", "其实是", "实际上是", "应该是",
    "改一下", "修改", "更正", "纠正", "更正一下", "纠正一下", "重新说",
    "现在改成", "已经换成", "换人", "换了", "不再是", "作废",
)
# 同一维度的锚点：新事实与旧事实只要共享锚点，就是在改同一件事。
# 部门/职位等岗位维度共用一组锚点，避免"领导张三 → 领导李四"被判成两条独立事实。
_DIMENSION_ANCHORS: tuple[tuple[str, ...], ...] = (
    ("领导", "老板", "上司", "主管", "汇报对象", "manager", "boss"),
    ("生日", "出生日期", "出生"),
    ("住址", "住在", "家住", "地址", "小区"),
    ("公司", "单位", "就职", "上班"),
    ("职位", "岗位", "职称", "title"),
    ("电话", "手机号", "联系方式"),
    ("邮箱", "电子邮件", "email"),
    ("学校", "大学", "母校", "就读"),
)
def _shared_dimension(new_text: str, old_text: str) -> bool:
    """两条记忆是否在描述同一个维度（领导/生日/住址/公司/职位…）。"""
    for group in _DIMENSION_ANCHORS:
        if any(word in new_text for word in group) and any(word in old_text for word in group):
            return True
    return False


def _deterministic_correction_target(
    new_text: str,
    similar: list[tuple[uuid.UUID, str]],
    *,
    user_text: str = "",
) -> uuid.UUID | None:
    """纠错输入挑出必须失效的旧记录（同维度、且不是同一句话）。

    真实 badcase：用户说"领导是李四（之前张三作废）"，旧事实"领导是张三"
    仍然 is_active=True，检索会把张三重新注入，等于没改。只要新事实带纠错词
    （不对/记错了/其实是/修改…）且与某条旧记忆同维度，那条旧记忆就必须失效。

    纠错词必须在**用户原话**里找：抽取结果会被要求改写成客观的三陈述
    "用户领导是李四"，指令性的"不对/记错了"已被剥掉，只看新文本会漏判。
    """
    haystack = f"{new_text} {user_text}"
    if not any(word in haystack for word in _CORRECTION_WORDS):
        return None
    candidates = [
        memory_id
        for memory_id, old_text in similar
        if _shared_dimension(new_text, old_text) and old_text.strip() != new_text.strip()
    ]
    # 多候选时交给决策 LLM（它能看到措辞），避免误删同维度但不同主题的旧值。
    return candidates[0] if len(candidates) == 1 else None


# Shared domain anchors tell us two preferences are about the same thing
# (drink vs. sport vs. music), so a replacement only deletes the right memory.
_DOMAIN_WORDS = (
    "喝", "吃", "咖啡", "茶", "饮料", "酒", "音乐", "歌", "电影", "书",
    "运动", "跑步", "健身", "住", "城市", "颜色", "风格", "口味", "穿",
    "游戏", "工作", "旅行",
)


def _deterministic_supersede_target(
    new_text: str,
    similar: list[tuple[uuid.UUID, str]],
) -> uuid.UUID | None:
    """Pick the old preference a replacement fact should soft-delete.

    The decision LLM occasionally labels "我其实更喜欢喝美式" as ADD because
    liking both drinks is technically possible. For an explicit replacement
    ("更喜欢/改成/不再") about the same domain, the older preference is stale —
    resolve it deterministically instead of hoping the LLM notices.
    """
    if not any(word in new_text for word in _PREFERENCE_WORDS):
        return None
    # 显式偏好（"我最喜欢…""我更爱…"）本身就是一次表态，同领域的旧偏好已经
    # 过时，直接覆盖；不需要依赖"改成/不再"这类替换词。记忆会把用户原话改写
    # 成"用户最喜欢…"，所以这里同时认第三人称的强调形式。
    if (
        not any(word in new_text for word in _REPLACEMENT_WORDS)
        and not is_explicit_preference(new_text)
        and not memory_noise_filter.is_strong_preference(new_text)
    ):
        return None
    candidates = [
        memory_id
        for memory_id, old_text in similar
        if any(word in old_text for word in _PREFERENCE_WORDS)
        and any(word in new_text and word in old_text for word in _DOMAIN_WORDS)
    ]
    # Ambiguous (several plausible targets) → let the LLM decision stand.
    return candidates[0] if len(candidates) == 1 else None


def _coerce_uuid(raw: Any) -> uuid.UUID | None:
    """Coerce an LLM-provided id to a UUID, or None when malformed."""
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        logger.warning("Memory decision returned malformed target id: %r", raw)
        return None


def _parse_decision(raw: str) -> dict[str, Any] | None:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    action = str(parsed.get("action", "")).strip().upper()
    if action not in {"ADD", "UPDATE", "DELETE", "NOOP"}:
        return None
    target = parsed.get("target_memory_id")
    return {"action": action, "target_memory_id": target if isinstance(target, str) else None}


async def _decide_memory_action(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    new_text: str,
    similar: list[tuple[uuid.UUID, str]],
    *,
    decision_llm: ChatOpenAI | None = None,
) -> dict[str, Any] | None:
    """Call the lightweight LLM to classify ADD/UPDATE/DELETE/NOOP.

    Returns None on LLM/parse failure so the caller falls back to a plain ADD.
    """
    if not similar:
        return {"action": "ADD", "target_memory_id": None}

    llm = decision_llm or chat_llm(settings)
    similar_lines = "\n".join(
        f"- id: {memory_id} | text: {text}" for memory_id, text in similar
    )
    human = (
        f"NEW FACT: {new_text}\n\n"
        f"EXISTING SIMILAR MEMORIES ({len(similar)}):\n{similar_lines}"
    )
    messages = [
        SystemMessage(
            content=MEMORY_DECISION_SYSTEM_PROMPT.format(
                sampled=MEMORY_DECISION_SIMILAR_MEMORIES,
            ),
        ),
        HumanMessage(content=human),
    ]
    try:
        response = await _ainvoke_with_retry(llm, messages)
        raw = response.content if isinstance(response.content, str) else "{}"
    except Exception:
        logger.warning("Memory decision LLM call failed; falling back to ADD for %r", new_text[:60])
        return None
    return _parse_decision(raw)


async def run_post_chat_memory_extraction(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    message_payloads: list[dict[str, Any]],
    final_answer: str,
    *,
    extraction_llm: ChatOpenAI | None = None,
    decision_llm: ChatOpenAI | None = None,
) -> None:
    """LLM scan + incremental persist facts; caller owns transaction (commit/rollback).

    Each extracted fact first retrieves its top-{sampled} similar existing
    memories and asks the decision LLM for ADD/UPDATE/DELETE/NOOP. A failed
    decision step falls back to a plain insert, so the pipeline is never blocked.
    """
    if not settings.database_url or not settings.voyageai_api_key:
        return

    conversation_text = _conversation_text_from_payloads(message_payloads, final_answer)
    if not conversation_text.strip():
        return

    llm = extraction_llm or chat_llm(settings)
    extraction_messages = [
        SystemMessage(content=MEMORY_EXTRACTION_SYSTEM_PROMPT),
        HumanMessage(content=conversation_text),
    ]

    try:
        response = await _ainvoke_with_retry(llm, extraction_messages)
        raw = response.content if isinstance(response.content, str) else "[]"
        facts = _extract_json_array(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Memory extraction returned non-JSON; skipping persist")
        return
    except Exception:
        logger.exception("Post-chat memory extraction LLM call failed for user %s", user_id)
        return

    # Prompt 只是第一道防线：LLM 会漏掉"我最喜欢喝拿铁"这类显式偏好，
    # 却抽到"更爱美式"，导致新旧偏好只剩一半。这里确定性补齐。
    for extra in _missing_explicit_preferences(facts, message_payloads):
        logger.info("Backfilled missed explicit preference: %.60s", extra["text"])
        facts.append(extra)

    # 用户原话：纠错词（"不对""记错了"）只存在于原话里，抽取结果会被改写成
    # 客观陈述，纠错判定必须回看原文，否则旧记录永远不会被真正失效。
    user_text = " ".join(
        str(message.get("content") or "")
        for message in message_payloads
        if message.get("role") == "user"
    )

    for fact in facts:
        # "领导是李四（之前张三作废）" 里的备注必须在检索相似记忆之前就剥掉：
        # 否则作废的张三会因为出现在新文本里而拉高相似度，反过来干扰决策。
        text = strip_superseded_notes(fact.get("text", "").strip())
        category = fact.get("category", "").strip()
        if not text or not category:
            continue
        # Prompt 只是第一道防线：LLM 仍会把"爱玩手机"这类长期习惯归成 episodic。
        # 代码兜底把它改回 preference，避免习惯被降级成一次性流水账。
        category = correct_habit_category(text, category)
        # 显式第一人称偏好必须落在 preference：被 LLM 标成 fact/episodic 时，
        # 检索会把它当背景事实，而不是可持续生效的偏好。
        if is_explicit_preference(text) and category != "preference":
            logger.info("Corrected explicit preference category %s -> preference", category)
            category = "preference"

        # 代码闸门：一次性流水账（"明天早上八点改简历""点了杯拿铁""今天很忙"）
        # 绝不进长期记忆。Prompt 约束只是第一道防线，LLM 仍会违规，落库前必须
        # 确定性拦截 —— 记忆一旦被污染，会通过检索影响后续所有对话。
        if is_one_off_log(text):
            logger.info("Skipped one-off log (not a long-term memory): %.60s", text)
            continue

        similar: list[tuple[uuid.UUID, str]] = []
        try:
            similar = await find_similar_memories(
                session,
                settings,
                user_id,
                text,
                limit=MEMORY_DECISION_SIMILAR_MEMORIES,
            )
            decision = await _decide_memory_action(
                session,
                settings,
                user_id,
                text,
                similar,
                decision_llm=decision_llm,
            )
        except Exception:
            logger.warning("Failed to run memory decision for %r; falling back to ADD", text, exc_info=True)
            decision = None

        action = (decision or {}).get("action", "ADD")
        target_id = (decision or {}).get("target_memory_id")

        # Fail-safe for explicit preference replacement: the decision LLM can
        # reasonably answer ADD ("the user may like both"), which would leave the
        # stale preference active and let retrieval surface contradictory facts.
        if action == "ADD" and category == "preference":
            override = _deterministic_supersede_target(text, similar)
            if override is not None:
                action = "UPDATE"
                target_id = str(override)
                logger.info("Preference replacement detected; forcing UPDATE for %.60s", text)

        # Fail-safe for factual correction: "领导是李四（之前张三作废）" must retire
        # the old row. The decision LLM often answers ADD because both facts look
        # true in isolation; the correction words make the intent unambiguous, so
        # the old same-dimension memory is deactivated deterministically.
        if action in {"ADD", "NOOP"}:
            correction = _deterministic_correction_target(text, similar, user_text=user_text)
            if correction is not None:
                action = "UPDATE"
                target_id = str(correction)
                logger.info("Fact correction detected; forcing UPDATE for %.60s", text)

        logger.info("Memory decision for %.60s: %s (target=%s)", text, action, target_id)

        try:
            if action == "DELETE":
                # Only skip the insert when a real target was deleted. Malformed or
                # absent id → fall through to ADD so a fact is never silently dropped
                # because the decision LLM returned a bad id.
                if _coerce_uuid(target_id) is not None:
                    await soft_delete_memory(session, user_id, _coerce_uuid(target_id))
                    logger.info("Memory DELETE decided, no insert: %.60s", text)
                    continue
            if action == "NOOP":
                logger.info("Memory NOOP (duplicate) decided: %.60s", text)
                continue
            # ADD, UPDATE, or failed decision → soft-delete target for UPDATE, then insert.
            if action == "UPDATE" and _coerce_uuid(target_id) is not None:
                await soft_delete_memory(session, user_id, _coerce_uuid(target_id))
            await add_memory_tool(session, settings, user_id, text, category)
        except Exception:
            logger.warning("Failed to save extracted memory: %s", text, exc_info=True)


async def run_post_chat_memory_extraction_deferred(
    user_id: uuid.UUID,
    message_payloads: list[dict[str, Any]],
    final_answer: str,
) -> None:
    """Open a fresh DB session — for FastAPI BackgroundTasks after the response is sent."""
    from config import get_settings
    from db.session import get_async_session_maker

    settings = get_settings()
    if not settings.database_url or not settings.voyageai_api_key:
        return

    session_maker = get_async_session_maker()
    if session_maker is None:
        logger.warning("Deferred memory extraction skipped: database session factory missing")
        return

    async with session_maker() as session:
        try:
            await run_post_chat_memory_extraction(
                session,
                settings,
                user_id,
                message_payloads,
                final_answer,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Deferred post-chat memory extraction failed for user %s", user_id)
