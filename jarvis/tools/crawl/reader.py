"""``read_url`` — the crawl chain: jina → local extractor → firecrawl.

Order is configurable (``crawl.chain`` in config.yaml).  Every attempt is
recorded so the tool can explain *why* a page could not be read, and a
successful read is optionally indexed into the knowledge base.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..base import tool
from ..search.base import SearchError
from ...config import get as cfg_get
from ...utils.logger import get_logger
from .firecrawl import FirecrawlClient
from .jina_reader import CrawlError, jina_read
from .trafilatura_reader import fetch_and_extract

__all__ = ["CRAWL_TOOLS", "read_url", "read_many", "crawl_tools_report",
           "read_url_text", "reader_chain_report"]

log = get_logger("tools.crawl.reader")

DEFAULT_CHAIN = ["jina", "local", "firecrawl"]
_TITLE_RE = re.compile(r"^\s*Title:\s*(.+)$", re.M)
MIN_USEFUL = 120  # chars — below this we consider the read a failure


#: aliases so either spelling in config.yaml works
_ALIASES = {"jina": "jina", "jina_reader": "jina", "local": "local",
            "local_reader": "local", "trafilatura": "local",
            "firecrawl": "firecrawl"}


def _chain_from_config(config: Optional[Mapping[str, Any]]) -> List[str]:
    """Reader order from ``crawl.chain`` (falls back to ``crawl.provider_order``)."""
    if config is None:
        from ...config import load_config

        config = load_config()
    chain = (cfg_get(config, "crawl.chain")
             or cfg_get(config, "crawl.provider_order")
             or DEFAULT_CHAIN)
    resolved = []
    for item in chain:
        name = _ALIASES.get(str(item).lower())
        if name and name not in resolved:
            resolved.append(name)
    return resolved or list(DEFAULT_CHAIN)


def _resolve_reader(name: str, config: Optional[Mapping[str, Any]]
                    ) -> Callable[[str], str]:
    if name == "jina":
        key = os.environ.get("JINA_API_KEY") or cfg_get(
            config or {}, "crawl.jina_api_key", "")
        return lambda url: jina_read(url, api_key=key or None)
    if name == "local":
        return lambda url: fetch_and_extract(url)
    if name == "firecrawl":
        client = FirecrawlClient(_firecrawl_key(config))

        def _firecrawl(url: str) -> str:
            data = client.scrape(url)
            text = (data.get("markdown") or data.get("text")
                    or data.get("content") or "").strip()
            if len(text) < MIN_USEFUL:
                raise CrawlError("firecrawl returned no useful content")
            return text

        return _firecrawl
    raise CrawlError(f"unknown reader '{name}'")


def _firecrawl_key(config: Optional[Mapping[str, Any]] = None) -> str:
    if config is None:
        from ...config import load_config

        config = load_config()
    return (os.environ.get("FIRECRAWL_API_KEY")
            or str(cfg_get(config, "crawl.firecrawl.api_key", "") or ""))


def read_url_text(url: str, *, config: Optional[Mapping[str, Any]] = None,
                  max_chars: int = 12000, chain: Optional[List[str]] = None,
                  ) -> Dict[str, Any]:
    """Try each reader in order; return the first usable text."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "no URL given", "attempts": [], "text": ""}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")

    names = chain or _chain_from_config(config)
    attempts: List[Dict[str, str]] = []
    for name in names:
        try:
            text = _resolve_reader(name, config)(url) or ""
        except Exception as exc:
            attempts.append({"reader": name, "error": str(exc)})
            log.debug(f"reader {name} failed for {url}: {exc}")
            continue
        if len(text.strip()) < MIN_USEFUL:
            attempts.append({"reader": name, "error": "too little text"})
            continue
        title = ""
        match = _TITLE_RE.search(text[:400])
        if match:
            title = match.group(1).strip()
        attempts.append({"reader": name, "error": ""})
        return {"ok": True, "url": url, "text": text[:max_chars],
                "provider": name, "title": title,
                "chars": len(text), "attempts": attempts}

    summary = "; ".join(f"{a['reader']}: {a['error'] or 'no content'}"
                        for a in attempts) or "no readers configured"
    return {"ok": False, "url": url, "text": "", "provider": "", "title": "",
            "chars": 0, "attempts": attempts,
            "error": f"could not read {url} ({summary})"}


def read_url_or_raise(url: str, *, config: Optional[Mapping[str, Any]] = None,
                      max_chars: int = 12000) -> Dict[str, Any]:
    """Like :func:`read_url_text` but raises :class:`SearchError` on failure."""
    result = read_url_text(url, config=config, max_chars=max_chars)
    if not result["ok"]:
        raise SearchError(result.get("error") or f"could not read {url}")
    return result


def reader_chain_report(config: Optional[Mapping[str, Any]] = None) -> str:
    """Human-readable status of every reader in the chain (used by doctor)."""
    order = _chain_from_config(config)
    notes = {
        "jina": "keyless public endpoint (20 req/min without JINA_API_KEY)",
        "local": "stdlib+trafilatura direct fetch — always available",
        "firecrawl": "needs FIRECRAWL_API_KEY (500 free credits)",
    }
    lines = ["**Crawl chain:**"]
    for index, name in enumerate(order):
        problem = ""
        if name == "firecrawl" and not _firecrawl_key(config):
            problem = "no API key (free at firecrawl.dev)"
        lines.append(f"{index + 1}. `{name}` — {'⚠ ' + problem if problem else '✓ ready'} "
                     f"({notes.get(name, 'custom reader')})")
    return "\n".join(lines)


# ── tools ────────────────────────────────────────────────────────────────
@tool(category="crawl", timeout=90)
def read_url(url: str, max_chars: int = 12000, save_to_knowledge: bool = False,
             reader: str = "") -> str:
    """Open a web page and return its readable text.

    Args:
        url: Page to read (scheme optional)
        max_chars: Truncate the text at this many characters
        save_to_knowledge: Also index the text into the permanent knowledge base
        reader: Force one reader (jina/local/firecrawl); empty = automatic chain
    """
    from ...config import load_config

    config = load_config()
    result = read_url_text(url, config=config, max_chars=max_chars,
                           chain=[reader] if reader else None)
    if not result["ok"]:
        attempted = " | ".join(f"{a['reader']}: {a['error']}" for a in result["attempts"])
        return (False, f"Could not read {url or '(no url)'}.\n{attempted}\n"
                "Options: check the URL, or set FIRECRAWL_API_KEY for the cloud reader.")
    text = result["text"]
    header = f"**{result['title'] or result['url']}**\n{result['url']}"
    footer = f"\n\n_({result['provider']} · {result['chars']} chars)_"
    if save_to_knowledge:
        try:
            from ...memory.memory_manager import get_memory_manager

            added = get_memory_manager(config).add_knowledge(
                text, source=result["url"], topic=result["title"][:60])
            footer += f" · saved {added} chunk(s) to knowledge"
        except Exception as exc:
            log.warning(f"knowledge save failed: {exc}")
            footer += " · knowledge save failed"
    return f"{header}\n\n{text}{footer}"


@tool(category="crawl", timeout=120)
def read_many(urls: str, max_chars: int = 4000) -> str:
    """Read several web pages in one go (newline or comma separated).

    Args:
        urls: URLs, one per line or comma separated (max 6)
        max_chars: Character budget per page
    """
    from ...config import load_config

    config = load_config()
    items = [u.strip() for u in re.split(r"[,\n]+", urls or "") if u.strip()]
    if not items:
        return "No URLs given."
    if len(items) > 6:
        items = items[:6]
    blocks = []
    for url in items:
        result = read_url_text(url, config=config, max_chars=max_chars)
        if result["ok"]:
            blocks.append(f"## {result['title'] or url}\n{result['url']}\n\n{result['text']}")
        else:
            blocks.append(f"## {url}\n_✗ {result.get('error', 'unreadable')}_")
    return "\n\n---\n\n".join(blocks)


@tool(category="crawl", timeout=30)
def crawl_tools_report() -> str:
    """Show which page readers are available and in what order they are tried."""
    from ...config import load_config

    config = load_config()
    report = reader_chain_report(config)
    report += ("\n\n**Firecrawl tools:** scrape / crawl / map (1 credit each, "
               "500 free one-time).")
    report += ("\n\n_Tip: `read_url` walks the whole chain automatically; "
               "`jina_read_url` and `local_read_url` force one reader._")
    return report


CRAWL_TOOLS = [read_url, read_many, crawl_tools_report]
