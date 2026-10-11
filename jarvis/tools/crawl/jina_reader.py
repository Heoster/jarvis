"""Jina Reader (r.jina.ai) — keyless default reader for the crawl chain."""

from __future__ import annotations

import os
from typing import Optional

import httpx

from ..base import tool
from ...utils.logger import get_logger

log = get_logger("tools.crawl.jina")

JINA_READER_BASE = "https://r.jina.ai/"
JINA_SEARCH_BASE = "https://s.jina.ai/"


class CrawlError(RuntimeError):
    """Raised when the reader chain cannot produce usable text."""


def jina_available() -> bool:
    return True  # the public endpoint works without a key (rate limited)


def jina_read(url: str, *, timeout: float = 25.0, max_chars: int = 12000,
              api_key: Optional[str] = None) -> str:
    """Return the page as clean markdown via Jina Reader."""
    if not url or not url.startswith(("http://", "https://")):
        raise CrawlError(f"not a fetchable URL: {url!r}")
    headers = {"Accept": "text/plain", "X-Return-Format": "markdown",
               "User-Agent": "JARVIS/1.0 (+local assistant)"}
    key = api_key or os.environ.get("JINA_API_KEY") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = httpx.get(f"{JINA_READER_BASE}{url}", headers=headers,
                             timeout=timeout, follow_redirects=True)
    except Exception as exc:  # network, DNS, TLS
        raise CrawlError(f"jina reader unreachable: {exc}") from exc
    if response.status_code == 429:
        raise CrawlError("jina reader rate limit hit (free tier: 20 req/min)")
    if response.status_code >= 400:
        raise CrawlError(f"jina reader HTTP {response.status_code}")
    text = (response.text or "").strip()
    if len(text) < 80:
        raise CrawlError("jina reader returned (almost) nothing")
    return text[:max_chars]


def jina_search(query: str, *, timeout: float = 25.0, max_results: int = 5,
                api_key: Optional[str] = None) -> str:
    """s.jina.ai — keyless web search that returns LLM-friendly text."""
    headers = {"Accept": "text/plain", "X-Return-Format": "markdown"}
    key = api_key or os.environ.get("JINA_API_KEY") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    response = httpx.get(f"{JINA_SEARCH_BASE}{query}", headers=headers,
                         timeout=timeout, follow_redirects=True)
    if response.status_code >= 400:
        raise CrawlError(f"jina search HTTP {response.status_code}")
    return (response.text or "").strip()[: max(600, 600 * max_results)]


# ── tool ────────────────────────────────────────────────────────────────
@tool(category="crawl", timeout=60)
def jina_read_url(url: str, max_chars: int = 12000) -> str:
    """Read a web page as clean markdown through Jina Reader (free, no key needed).

    Args:
        url: Page to read
        max_chars: Truncate at this many characters
    """
    try:
        text = jina_read(url, max_chars=max_chars)
    except CrawlError as exc:
        return (f"Jina Reader could not fetch {url}: {exc}\n"
                "Options: retry, add JINA_API_KEY, or use `read_url` (tries the "
                "local extractor and Firecrawl too).")
    return f"**{url}**\n\n{text}\n\n_(via jina reader · {len(text)} chars)_"
