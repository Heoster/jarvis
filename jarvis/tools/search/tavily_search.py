"""Tavily AI Search — 1 000 free credits/month, quota-guarded.

Tavily is the best free option for *answers* (it returns a synthesised answer
plus clean snippets), which suits a 3B model far better than ten raw links.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

from ...config import get, load_config
from ...utils.logger import get_logger
from ..base import tool
from .base import SearchError, SearchResult, fetch_json

log = get_logger("search.tavily")

__all__ = ["tavily_search", "tavily_answer", "TavilyClient"]

API_URL = "https://api.tavily.com/search"


def _api_key() -> str:
    return (os.environ.get("TAVILY_API_KEY")
            or str(get(load_config(), "search.tavily.api_key", "") or ""))


class TavilyClient:
    """Minimal API client (no vendor SDK needed)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or _api_key()

    def search(self, query: str, *, max_results: int = 5,
               search_depth: str = "basic", include_answer: bool = True,
               topic: str = "general", include_domains: Optional[List[str]] = None,
               exclude_domains: Optional[List[str]] = None) -> Dict[str, Any]:
        if not self.api_key:
            raise SearchError("TAVILY_API_KEY is not set (free key at tavily.com)")
        payload: Dict[str, Any] = {
            "api_key": self.api_key, "query": query,
            "max_results": max(1, min(int(max_results), 10)),
            "search_depth": search_depth if search_depth in ("basic", "advanced")
            else "basic",
            "include_answer": bool(include_answer), "topic": topic,
        }
        if include_domains:
            payload["include_domains"] = include_domains[:10]
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains[:10]
        return fetch_json(API_URL, provider="tavily", method="POST",
                          json_body=payload, timeout=25.0)

    def as_results(self, query: str, **kwargs: Any) -> List[SearchResult]:
        data = self.search(query, **kwargs)
        results: List[SearchResult] = []
        answer = data.get("answer")
        if answer:
            results.append(SearchResult(title="Tavily answer", url="", snippet=answer,
                                        source="tavily", score=1.0,
                                        extra={"kind": "answer"}))
        for item in data.get("results") or []:
            results.append(SearchResult(
                title=item.get("title", ""), url=item.get("url", ""),
                snippet=item.get("content", "") or item.get("raw_content", "")[:400],
                source="tavily", score=float(item.get("score", 0) or 0),
                published=str(item.get("published_date", "") or "")))
        return results


@tool(category="search", timeout=60)
def tavily_search(query: str, max_results: int = 5, depth: str = "basic") -> str:
    """Search the web with Tavily (free tier) and get an AI-summarised answer.

    Best for: factual questions, current events, "what is / who is / latest".
    Costs 1 of 1 000 free monthly credits.

    Args:
        query: What to search for
        max_results: How many results (1-10)
        depth: "basic" (1 credit) or "advanced" (2 credits, deeper crawl)
    """
    try:
        client = TavilyClient()
        data = client.search(query, max_results=max_results, search_depth=depth)
    except SearchError as exc:
        return f"Tavily unavailable: {exc}\nFalling back is automatic — try `search_web`."
    parts: List[str] = []
    if data.get("answer"):
        parts.append(f"**Answer:** {data['answer']}\n")
    for index, item in enumerate(data.get("results") or [], 1):
        parts.append(f"{index}. **{item.get('title', '')}**\n"
                     f"   {item.get('url', '')}\n"
                     f"   {(item.get('content') or '')[:300]}")
    used = (data.get("response_time") or 0)
    parts.append(f"\n_({len(data.get('results') or [])} results · "
                 f"tavily/{depth} · {used}s · 1 credit used)_")
    return "\n".join(parts)


@tool(category="search", timeout=60)
def tavily_answer(question: str) -> str:
    """Ask Tavily for a direct, sourced answer (cheapest useful call: 1 credit).

    Args:
        question: The question to answer
    """
    try:
        data = TavilyClient().search(question, max_results=3, include_answer=True)
    except SearchError as exc:
        return f"Tavily unavailable: {exc}"
    sources = "\n".join(f"- {r.get('title')}: {r.get('url')}"
                        for r in (data.get("results") or [])[:3])
    return f"{data.get('answer') or '(no answer)'}\n\nSources:\n{sources}"
