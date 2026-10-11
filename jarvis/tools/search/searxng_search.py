"""SearXNG — self-hosted metasearch: unlimited, private, keyless."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from ...config import get, load_config
from ...utils.logger import get_logger
from ..base import tool
from .base import SearchError, SearchResult, fetch_json

log = get_logger("search.searxng")

__all__ = ["searxng_search", "SearxngClient", "searxng_available"]


def _base_url() -> str:
    return (os.environ.get("SEARXNG_BASE_URL")
            or str(get(load_config(), "search.searxng.base_url", "") or "")).rstrip("/")


class SearxngClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or _base_url()).rstrip("/")

    def search(self, query: str, *, num: int = 5, language: str = "en",
               categories: str = "general", time_range: str = "") -> Dict[str, Any]:
        if not self.base_url:
            raise SearchError("SearXNG base_url is not configured")
        params: Dict[str, Any] = {"q": query, "format": "json", "language": language,
                                 "categories": categories, "safesearch": 1}
        if time_range:
            params["time_range"] = time_range
        return fetch_json(urljoin(self.base_url, "/search"), provider="searxng",
                          params=params, timeout=20.0)

    def as_results(self, query: str, num: int = 5, **kwargs: Any) -> List[SearchResult]:
        data = self.search(query, num=num, **kwargs)
        results: List[SearchResult] = []
        for item in (data.get("results") or [])[:num]:
            results.append(SearchResult(
                title=item.get("title", ""), url=item.get("url", ""),
                snippet=item.get("content", ""), source="searxng",
                score=float(item.get("score", 0) or 0),
                published=str(item.get("publishedDate", "") or ""),
                extra={"engines": item.get("engines") or []}))
        return results


def searxng_available() -> bool:
    if not _base_url():
        return False
    try:
        import httpx

        with httpx.Client(timeout=3.0) as client:
            response = client.get(urljoin(_base_url(), "/healthz"))
        return response.status_code < 400
    except Exception:
        return False


@tool(category="search", timeout=45)
def searxng_search(query: str, num: int = 5, time_range: str = "") -> str:
    """Search through a self-hosted SearXNG instance (unlimited, no API key).

    Use this as the workhorse when you do not want to spend Tavily/Serper
    credits. Needs a running instance:
    `docker run -d -p 8080:8080 searxng/searxng` and JSON output enabled.

    Args:
        query: Search query
        num: Number of results
        time_range: "", "day", "week", "month", "year"
    """
    try:
        results = SearxngClient().as_results(query, num=num, time_range=time_range)
    except SearchError as exc:
        return (f"SearXNG unavailable: {exc}\n"
                f"Start one with: docker run -d -p 8080:8080 searxng/searxng")
    if not results:
        return "SearXNG returned no results."
    return "\n".join(result.line(index) for index, result in enumerate(results, 1))
