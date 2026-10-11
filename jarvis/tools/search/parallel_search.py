"""Parallel, multi-engine search + the ``search_web`` orchestrator.

``search_web`` is the tool the LLM actually calls.  It walks the configured
provider order (tavily → serper → searxng → ddg → jina) and returns the first
useful answer, so a spent free tier never breaks a task — it just changes where
the answer comes from.  ``parallel_search`` fans out to several engines at once
and merges/dedupes the results, which is what research tasks want.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...config import load_config
from ...utils.logger import get_logger
from ..base import failure, tool
from .base import (
    SearchError, SearchQuotaExceeded, SearchResult, dedupe_results, format_results,
    provider_available,
)

log = get_logger("search.parallel")

__all__ = ["search_web", "parallel_search", "search_news", "search_providers"]

_CHAIN = ("tavily", "serper", "searxng", "ddg", "jina")


def _config() -> Mapping[str, Any]:
    return load_config()


def _order() -> List[str]:
    config = _config()
    order = ((config.get("search") or {}).get("provider_order") or list(_CHAIN))
    return [str(name).lower() for name in order] or list(_CHAIN)


def _run_provider(provider: str, query: str, max_results: int) -> List[SearchResult]:
    """Dispatch to one provider (imported lazily to keep startup fast)."""
    if provider == "tavily":
        from .tavily_search import TavilyClient

        return TavilyClient().as_results(query, max_results=max_results)
    if provider == "serper":
        from .serper_search import SerperClient

        return SerperClient().as_results(query, num=max_results)
    if provider == "searxng":
        from .searxng_search import SearxngClient

        return SearxngClient().as_results(query, num=max_results)
    if provider == "ddg":
        return _duckduckgo(query, max_results)
    if provider == "jina":
        return _jina(query, max_results)
    raise SearchError(f"unknown search provider '{provider}'")


def _duckduckgo(query: str, max_results: int = 5) -> List[SearchResult]:
    """Keyless HTML endpoint — the always-there baseline."""
    from .base import fetch_text

    config = _config()
    ddg_cfg = (config.get("search") or {}).get("duckduckgo") or {}
    if ddg_cfg.get("enabled") is False:
        raise SearchError("duckduckgo disabled in config")
    base = str(ddg_cfg.get("base_url") or "https://html.duckduckgo.com/html/")
    page = fetch_text(base, provider="ddg", method="POST", data={"q": query},
                      timeout=20.0)
    results: List[SearchResult] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
        r'.*?(?:class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
        re.S | re.I)
    for match in pattern.finditer(page):
        url = html.unescape(match.group("url"))
        if url.startswith("//duckduckgo.com/l/"):
            from urllib.parse import parse_qs, unquote, urlparse

            query_string = urlparse("https:" + url).query
            params = parse_qs(query_string)
            url = unquote(params.get("uddg", [""])[0]) or url
        title = re.sub(r"<[^>]+>", "", html.unescape(match.group("title") or "")).strip()
        snippet = re.sub(r"<[^>]+>", "",
                         html.unescape(match.group("snippet") or "")).strip()
        if url and title:
            results.append(SearchResult(title=title, url=url, snippet=snippet,
                                        source="duckduckgo"))
        if len(results) >= max_results:
            break
    if not results:
        raise SearchError("duckduckgo returned no parsable results")
    return results


def _jina(query: str, max_results: int = 5) -> List[SearchResult]:
    """Jina Reader search endpoint (s.jina.ai) — keyless."""
    import os

    from .base import fetch_text

    config = _config()
    base = str(((config.get("search") or {}).get("jina") or {}).get(
        "base_url") or "https://s.jina.ai")
    headers = {"Accept": "application/json", "X-Return-Format": "json"}
    if os.environ.get("JINA_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
    raw = fetch_text(f"{base.rstrip('/')}/{query}", provider="jina",
                     headers=headers, timeout=25.0)
    results: List[SearchResult] = []
    try:
        import json

        payload = json.loads(raw)
        for item in (payload.get("data") or [])[:max_results]:
            results.append(SearchResult(
                title=item.get("title", ""), url=item.get("url", ""),
                snippet=(item.get("description") or item.get("content") or "")[:400],
                source="jina"))
    except Exception:
        # plain-text fallback: lines look like "Title\nURL\ncontent…"
        blocks = [block.strip() for block in raw.split("\n\n") if block.strip()]
        for block in blocks[:max_results]:
            lines = block.splitlines()
            title = lines[0][:120] if lines else ""
            url = next((line.strip() for line in lines if line.strip().startswith("http")),
                       "")
            results.append(SearchResult(title=title, url=url,
                                        snippet=" ".join(lines[1:])[:300],
                                        source="jina"))
    if not results:
        raise SearchError("jina returned nothing usable")
    return results


@tool(category="search", timeout=90)
def search_web(query: str, max_results: int = 5, prefer: str = "") -> str:
    """Search the web. This is the default search tool — use it for facts, news,
    prices, docs, "who/what/when", anything you cannot answer from memory.

    It automatically uses whichever free provider still has quota, so you never
    need to pick one. Results come back with titles, URLs and snippets.

    Args:
        query: Search query — be specific, include names/dates
        max_results: How many results to return (1-10)
        prefer: Optional provider hint: "tavily", "serper", "searxng", "ddg", "jina"
    """
    order = _order()
    if prefer and prefer.lower() in order:
        order = [prefer.lower()] + [name for name in order if name != prefer.lower()]
    attempts: List[str] = []
    for provider in order:
        available, detail = provider_available(provider)
        if not available:
            attempts.append(f"{provider}: {detail}")
            continue
        try:
            results = _run_provider(provider, query, max(1, min(int(max_results), 10)))
        except SearchQuotaExceeded as exc:
            attempts.append(f"{provider}: {exc}")
            continue
        except SearchError as exc:
            attempts.append(f"{provider}: {exc}")
            continue
        except Exception as exc:  # unexpected provider bug
            attempts.append(f"{provider}: {type(exc).__name__}: {exc}")
            continue
        if not results:
            attempts.append(f"{provider}: no results")
            continue
        header = f"**Search results for “{query}”** _(via {provider})_"
        footer = ""
        if attempts:
            footer = "\n\n_fallbacks tried: " + "; ".join(attempts[-3:]) + "_"
        return format_results(dedupe_results(results), header=header) + footer

    # A total failure is a failure: report ok=False so callers (agents, the API
    # server) never mistake "no provider answered" for a successful search.
    return (False, "No search provider could answer right now:\n- "
            + "\n- ".join(attempts)
            + "\n\nOptions: add TAVILY_API_KEY / SERPER_API_KEY, run SearXNG "
              "(`docker run -d -p 8080:8080 searxng/searxng`), or check the network.")


@tool(category="search", timeout=120)
async def parallel_search(query: str, engines: str = "", max_results_per_engine: int = 4) -> str:
    """Search several engines at once and merge the results (best for research).

    Use when you want breadth or a balanced view: it queries Tavily, Serper,
    SearXNG, DuckDuckGo and Jina concurrently (whichever are configured), then
    deduplicates by URL.

    Args:
        query: Search query
        engines: Comma-separated subset, e.g. "tavily,ddg" (default: configured engines)
        max_results_per_engine: Results per engine
    """
    config = _config()
    configured = ((config.get("search") or {}).get("parallel") or {}).get("engines")
    names = [name.strip().lower() for name in engines.split(",") if name.strip()] \
        or [str(name).lower() for name in (configured or _order())]
    names = [name for name in names if name in _CHAIN][:5] or ["ddg"]

    async def one(name: str) -> Tuple[str, List[SearchResult]]:
        try:
            results = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _run_provider(name, query, max_results_per_engine))
            return name, results
        except Exception as exc:
            log.debug(f"parallel search {name} failed: {exc}")
            return name, []

    gathered = await asyncio.gather(*(one(name) for name in names))
    merged: List[SearchResult] = []
    per_engine: List[str] = []
    for name, results in gathered:
        per_engine.append(f"{name}:{len(results)}")
        for result in results:
            result.extra["engine"] = name
            merged.append(result)
    deduped = dedupe_results(merged)
    if not deduped:
        return (f"No engine returned results for “{query}” ({', '.join(per_engine)}). "
                f"Try `search_web` or `search_news`.")
    by_engine: Dict[str, int] = {}
    for result in deduped:
        engine = str(result.extra.get("engine", "?"))
        by_engine[engine] = by_engine.get(engine, 0) + 1
    header = (f"**Merged results for “{query}”** — "
              f"{len(deduped)} unique from {', '.join(per_engine)}")
    body = format_results(deduped[:12], max_chars=5000)
    return f"{header}\n\n{body}"


@tool(category="search", timeout=90)
def search_news(query: str, max_results: int = 5) -> str:
    """Search for recent news on a topic (uses Tavily's news topic when available).

    Args:
        query: Topic to search news for
        max_results: Number of headlines
    """
    try:
        from .tavily_search import TavilyClient

        client = TavilyClient()
        if client.api_key:
            results = client.as_results(query, max_results=max_results, topic="news",
                                        search_depth="basic")
            return format_results(dedupe_results(results),
                                  header=f"**News for “{query}”** _(tavily)_")
    except Exception as exc:
        log.debug(f"tavily news failed: {exc}")
    try:
        from .serper_search import SerperClient

        client = SerperClient()
        if client.api_key:
            results = client.as_results(query, num=max_results, kind="news")
            return format_results(dedupe_results(results),
                                  header=f"**News for “{query}”** _(serper/news)_")
    except Exception as exc:
        log.debug(f"serper news failed: {exc}")
    # `search_web` is a Tool object — invoke it properly so failures propagate
    fallback = search_web.invoke({"query": query, "max_results": max_results})
    if fallback.ok:
        return fallback.content
    return failure(fallback.error or "news search failed: no provider answered")


@tool(category="search", timeout=20)
def search_providers() -> str:
    """Show which search providers are available, their quota and their status."""
    from ...utils.quota import get_quota_manager

    manager = get_quota_manager()
    lines = ["Search providers (in fallback order):"]
    for provider in _order():
        available, detail = provider_available(provider)
        state = manager.state(provider)
        quota = ("unlimited" if state.unlimited
                 else f"{state.used}/{state.limit} {state.unit}s per {state.period}")
        lines.append(f"  {'✓' if available else '·'} {provider:<9} {quota:<28} {detail}")
    return "\n".join(lines)


SEARCH_TOOLS = [search_web, parallel_search, search_news, search_providers]
