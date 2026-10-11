"""Serper.dev — Google results, 2 500 free credits (one-time)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ...config import get, load_config
from ...utils.logger import get_logger
from ..base import tool
from .base import SearchError, SearchResult, fetch_json

log = get_logger("search.serper")

__all__ = ["serper_search", "SerperClient"]

API_URL = "https://google.serper.dev/search"


def _api_key() -> str:
    return (os.environ.get("SERPER_API_KEY")
            or str(get(load_config(), "search.serper.api_key", "") or ""))


class SerperClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or _api_key()

    def search(self, query: str, *, num: int = 5, gl: str = "in",
               hl: str = "en", kind: str = "search") -> Dict[str, Any]:
        if not self.api_key:
            raise SearchError("SERPER_API_KEY is not set (free credits at serper.dev)")
        return fetch_json(f"https://google.serper.dev/{kind}", provider="serper",
                          method="POST", json_body={"q": query, "num": max(1, min(num, 20)),
                                                    "gl": gl, "hl": hl},
                          headers={"X-API-KEY": self.api_key}, timeout=20.0)

    def as_results(self, query: str, **kwargs: Any) -> List[SearchResult]:
        data = self.search(query, **kwargs)
        results: List[SearchResult] = []
        if data.get("answerBox"):
            box = data["answerBox"]
            results.append(SearchResult(
                title=box.get("title", "Answer box"),
                url=box.get("link", ""),
                snippet=box.get("answer") or box.get("snippet", ""),
                source="serper", score=1.0, extra={"kind": "answer"}))
        knowledge = data.get("knowledgeGraph") or {}
        if knowledge.get("description"):
            results.append(SearchResult(
                title=knowledge.get("title", "Knowledge graph"),
                url=knowledge.get("descriptionLink", "") or "",
                snippet=knowledge["description"], source="serper", score=0.95,
                extra={"kind": "knowledge"}))
        for item in data.get("organic") or []:
            results.append(SearchResult(
                title=item.get("title", ""), url=item.get("link", ""),
                snippet=item.get("snippet", ""), source="serper",
                score=float(item.get("position", 0) or 0) and
                1.0 / float(item["position"]),
                published=str(item.get("date", "") or "")))
        return results


@tool(category="search", timeout=45)
def serper_search(query: str, num: int = 5) -> str:
    """Search Google via Serper (free credits) and return organic results.

    Use when Tavily is out of credits, or when you specifically want Google's
    ranking. Costs 1 of 2 500 one-time free credits.

    Args:
        query: Search query
        num: Number of results (1-20)
    """
    try:
        data = SerperClient().search(query, num=num)
    except SearchError as exc:
        return f"Serper unavailable: {exc}"
    lines: List[str] = []
    if data.get("answerBox"):
        box = data["answerBox"]
        lines.append(f"**Answer box:** {box.get('answer') or box.get('snippet', '')}\n")
    for index, item in enumerate(data.get("organic") or [], 1):
        lines.append(f"{index}. **{item.get('title', '')}**\n"
                     f"   {item.get('link', '')}\n   {(item.get('snippet') or '')[:250]}")
    people = data.get("peopleAlsoAsk") or []
    if people:
        lines.append("\n**People also ask:**")
        for item in people[:4]:
            lines.append(f"- {item.get('question')}")
    return "\n".join(lines) or "No results found."
