"""Tavily-backed web search for the `search` agent.

旅行规划的"推荐理由"如果只靠模型常识，就会写成一搜就能找到的通用攻略。这里接
Tavily 把真实网页摘要拿回来，作为 Context 交给合并节点。

失败语义要诚实：没有 key / 请求失败时抛出 `WebSearchError`，由 agent 转成
"本次联网检索失败"，绝不允许模型假装搜索过。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from config import Settings

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
WEB_SEARCH_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 400


class WebSearchError(RuntimeError):
    """Raised when a web search cannot be completed."""


def _normalize_result(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {
        "title": str(row.get("title") or "").strip()[:200],
        "url": str(row.get("url") or "").strip()[:500],
        "content": str(row.get("content") or "").strip()[:MAX_SNIPPET_CHARS],
        "score": row.get("score"),
    }


async def search_web(
    settings: Settings,
    query: str,
    *,
    max_results: int | None = None,
) -> dict[str, Any]:
    """Run one Tavily search and return a compact, model-friendly payload."""
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        raise WebSearchError("搜索关键词为空")
    api_key = (getattr(settings, "tavily_api_key", None) or "").strip()
    if not api_key:
        raise WebSearchError("未配置 TAVILY_API_KEY，无法联网检索")

    limit = max_results or getattr(settings, "web_search_max_results", DEFAULT_MAX_RESULTS)
    payload = {
        "api_key": api_key,
        "query": cleaned_query,
        "search_depth": "basic",
        "include_answer": True,
        "max_results": max(1, min(int(limit), 10)),
    }
    try:
        async with httpx.AsyncClient(timeout=WEB_SEARCH_TIMEOUT_SECONDS) as client:
            response = await client.post(TAVILY_SEARCH_URL, json=payload)
    except httpx.HTTPError as exc:
        raise WebSearchError(f"联网检索请求失败：{exc}") from exc

    if response.status_code >= 400:
        detail = response.text[:300]
        logger.warning("Tavily search failed (%s): %s", response.status_code, detail)
        raise WebSearchError(f"联网检索返回 HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise WebSearchError("联网检索返回了非 JSON 响应") from exc

    results = [
        normalized
        for normalized in (_normalize_result(row) for row in (data.get("results") or []))
        if normalized.get("url") or normalized.get("content")
    ]
    return {
        "provider": "tavily",
        "query": cleaned_query,
        "answer": str(data.get("answer") or "").strip()[: MAX_SNIPPET_CHARS * 2],
        "results": results,
    }


def format_search_result(payload: dict[str, Any]) -> str:
    """Render a search payload as plain text for the merge prompt / agent result."""
    query = payload.get("query") or ""
    lines = [f"联网检索「{query}」："]
    answer = payload.get("answer")
    if answer:
        lines.append(f"摘要：{answer}")
    results = payload.get("results") or []
    if not results:
        lines.append("没有检索到可用网页结果。")
        return "\n".join(lines)
    for index, row in enumerate(results, 1):
        title = row.get("title") or "（无标题）"
        url = row.get("url") or ""
        content = row.get("content") or ""
        lines.append(f"{index}. {title}")
        if content:
            lines.append(f"   {content}")
        if url:
            lines.append(f"   来源：{url}")
    return "\n".join(lines)
