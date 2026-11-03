"""Generate a concise conversation title from the first user message.

Uses a lightweight LLM call that degrades to a first-sentence truncation so the
title flow never blocks or throws. Off the SSE critical path — callers invoke it
inside a deferred task or with try/except anyway.
"""

import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from config import Settings, chat_llm

logger = logging.getLogger(__name__)

# Lightweight model for the title pass. Defaults to the chat model so no
# provider-specific model id is hardcoded (a hardcoded one 400s on any other
# provider); set OPENROUTER_UTILITY_MODEL to a cheaper model when available.
TITLE_LLM_MODEL: str | None = None


def _title_model(settings: Settings) -> str | None:
    return TITLE_LLM_MODEL or getattr(settings, "openrouter_utility_model", None)

TITLE_MIN_LENGTH = 4
TITLE_MAX_LENGTH = 40

# Fallback truncation bounds (plain first-user-text, no LLM needed).
TRUNCATE_MAX_CHARS = 30

_PLACEHOLDER_TITLE_RE = re.compile(r"^(对话\s*\d+|无标题对话|untitled|\s*)$", re.IGNORECASE)

_TITLE_SYSTEM_PROMPT = """\
You generate a short, descriptive title for a chat conversation.
Given the user's first message, return ONLY a 4-20 character Chinese title that
summarizes the user's intent. Do not include quotes, punctuation, or labels.
Do not mention "用户" or "对话". Output just the title text and nothing else.
"""


def is_placeholder_title(title: str | None) -> bool:
    """True when *title* is empty or still an auto-assigned placeholder.

    Placeholders ("对话 3", "无标题对话") must be replaced by a real title derived
    from the conversation's first user message. Explicit user-typed titles pass
    through untouched.
    """
    if not title:
        return True
    return bool(_PLACEHOLDER_TITLE_RE.match(title.strip()))


def truncate_title_from_text(text: str) -> str:
    """Deterministic fallback: first line of *text* clipped to TRUNCATE_MAX_CHARS."""
    first_line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return first_line[:TRUNCATE_MAX_CHARS].strip()


def _normalize_title(raw: str) -> str:
    cleaned = (raw or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Strip accidental quotes/quotation marks the LLM may add despite the prompt.
    cleaned = cleaned.strip('"\'`“”‘’ ')
    if len(cleaned) > TITLE_MAX_LENGTH:
        cleaned = cleaned[:TITLE_MAX_LENGTH].rstrip()
    return cleaned


async def generate_conversation_title(settings: Settings | None, first_user_text: str) -> str | None:
    """Return an LLM-suggested title, or None on any failure (caller truncates)."""
    text = (first_user_text or "").strip()
    if not text:
        return None
    if settings is None or not settings.openrouter_api_key:
        return None

    llm = chat_llm(settings, model=_title_model(settings))
    try:
        response = await llm.ainvoke(
            [
                SystemMessage(content=_TITLE_SYSTEM_PROMPT),
                HumanMessage(content=text),
            ],
        )
    except Exception:
        logger.warning("Conversation title LLM call failed; falling back to truncation", exc_info=True)
        return None

    raw = response.content if isinstance(response.content, str) else ""
    return _normalize_title(raw) or None


async def resolve_conversation_title(
    settings: Settings | None,
    first_user_text: str,
    *,
    explicit_title: str | None = None,
) -> str | None:
    """Resolve the title to persist: explicit > LLM-suggested > truncation."""
    if explicit_title is not None and not is_placeholder_title(explicit_title):
        return explicit_title.strip() if explicit_title.strip() else None

    llm_title = await generate_conversation_title(settings, first_user_text) if settings else None
    if llm_title and len(llm_title) >= TITLE_MIN_LENGTH:
        return llm_title

    truncated = truncate_title_from_text(first_user_text)
    return truncated or None