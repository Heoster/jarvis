"""Firecrawl — JavaScript-rendered scraping, 500 free credits (one-time)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ...config import get, load_config
from ...utils.logger import get_logger
from ..base import tool
from ..search.base import SearchError, fetch_json

log = get_logger("crawl.firecrawl")

__all__ = ["firecrawl_scrape", "firecrawl_crawl", "firecrawl_map", "FirecrawlClient"]

API_BASE = "https://api.firecrawl.dev/v1"


def _api_key() -> str:
    return (os.environ.get("FIRECRAWL_API_KEY")
            or str(get(load_config(), "crawl.firecrawl.api_key", "") or ""))


class FirecrawlClient:
    """Raw REST client (avoids the vendor SDK dependency)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or _api_key()

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise SearchError("FIRECRAWL_API_KEY is not set "
                              "(500 free credits at firecrawl.dev)")
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def scrape(self, url: str, *, formats: Optional[List[str]] = None,
               only_main_content: bool = True, timeout: float = 40.0) -> Dict[str, Any]:
        payload = {"url": url,
                   "formats": formats or ["markdown"],
                   "onlyMainContent": only_main_content}
        data = fetch_json(f"{API_BASE}/scrape", provider="firecrawl", method="POST",
                          json_body=payload, headers=self._headers(), timeout=timeout)
        if not data.get("success", True) and data.get("error"):
            raise SearchError(str(data["error"]))
        return data.get("data") or data

    def map_site(self, url: str, *, limit: int = 40) -> List[str]:
        data = fetch_json(f"{API_BASE}/map", provider="firecrawl", method="POST",
                          json_body={"url": url, "limit": limit},
                          headers=self._headers(), timeout=40.0)
        return list((data.get("data") or {}).get("links") or data.get("links") or [])


@tool(category="crawl", timeout=120)
def firecrawl_scrape(url: str, format: str = "markdown",
                     only_main_content: bool = True) -> str:
    """Scrape a page with JavaScript rendering (best for SPAs, docs, infinite scroll).

    Costs 1 of 500 one-time free credits, so prefer `jina_read_url` or `read_url`
    first and only escalate here when those return nothing useful.

    Args:
        url: URL to scrape
        format: "markdown", "html" or "text"
        only_main_content: Strip nav/footer/ads (recommended)
    """
    try:
        data = FirecrawlClient().scrape(url, formats=[format],
                                        only_main_content=only_main_content)
    except SearchError as exc:
        return (f"Firecrawl unavailable: {exc}\n"
                f"Use `read_url` (free local extraction) instead.")
    content = data.get(format) or data.get("markdown") or data.get("content") or ""
    if not content:
        return "Firecrawl returned no content for that URL."
    if len(content) > 10000:
        content = f"{content[:7500]}\n\n[…trimmed…]\n\n{content[-2000:]}"
    meta = data.get("metadata") or {}
    title = meta.get("title") or ""
    return f"**{title or url}**\n{url}\n\n{content}"


@tool(category="crawl", timeout=180)
def firecrawl_crawl(url: str, limit: int = 5) -> str:
    """Crawl several linked pages from a site (costs 1 credit per page).

    Args:
        url: Starting URL
        limit: Maximum number of pages (keep this small — credits are limited)
    """
    try:
        client = FirecrawlClient()
        data = fetch_json(f"{API_BASE}/crawl", provider="firecrawl", method="POST",
                          json_body={"url": url, "limit": max(1, min(limit, 10)),
                                     "scrapeOptions": {"formats": ["markdown"]}},
                          headers=client._headers(), timeout=120.0)
    except SearchError as exc:
        return f"Firecrawl crawl unavailable: {exc}"
    pages = (data.get("data") or {}).get("data") or data.get("data") or []
    if not isinstance(pages, list) or not pages:
        return f"Crawl returned no pages (status: {data.get('status', '?')})."
    chunks = []
    for page in pages[:limit]:
        meta = page.get("metadata") or {}
        body = (page.get("markdown") or "")[:2500]
        chunks.append(f"### {meta.get('title') or page.get('url', '')}\n"
                      f"{page.get('url', '')}\n\n{body}")
    return "\n\n---\n\n".join(chunks)


@tool(category="crawl", timeout=90)
def firecrawl_map(url: str, limit: int = 40) -> str:
    """List the URLs of a site (sitemap-style discovery).

    Args:
        url: Site root
        limit: Maximum URLs to list
    """
    try:
        links = FirecrawlClient().map_site(url, limit=limit)
    except SearchError as exc:
        return f"Firecrawl map unavailable: {exc}"
    if not links:
        return f"No URLs discovered for {url}."
    return f"{len(links)} URLs found on {url}:\n" + "\n".join(links[:limit])
