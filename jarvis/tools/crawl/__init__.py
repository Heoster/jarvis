"""Crawl/read tools: Jina Reader, local extractor, Firecrawl."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .reader import CRAWL_TOOLS

__all__ = ["register_tools", "CRAWL_TOOLS"]


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    from .firecrawl import firecrawl_crawl, firecrawl_map, firecrawl_scrape
    from .jina_reader import jina_read_url
    from .trafilatura_reader import local_read_url

    tools = list(CRAWL_TOOLS) + [jina_read_url, local_read_url, firecrawl_scrape,
                                 firecrawl_crawl, firecrawl_map]
    seen, unique = set(), []
    for tool in tools:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        unique.append(tool)
    return registry.register_all(unique, "crawl")
