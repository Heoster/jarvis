"""Search tools: Tavily, Serper, SearXNG, DuckDuckGo, Jina + parallel fan-out."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .parallel_search import SEARCH_TOOLS

__all__ = ["register_tools", "SEARCH_TOOLS"]


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    from .serper_search import serper_search
    from .searxng_search import searxng_search
    from .tavily_search import tavily_answer, tavily_search

    tools = list(SEARCH_TOOLS) + [tavily_search, tavily_answer, serper_search,
                                  searxng_search]
    seen, unique = set(), []
    for tool in tools:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        unique.append(tool)
    return registry.register_all(unique, "search")
