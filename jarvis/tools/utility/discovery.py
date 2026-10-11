"""Tool discovery — so a selectively-bound agent can still reach everything.

The brain binds only 5–9 tools per turn to save context, which means the model
sometimes needs something it was not given.  ``list_available_tools`` closes that
loop: it lists names + one-line descriptions on demand, and
``tool_help`` explains how to call one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from ...utils.logger import get_logger
from ...utils.sanitizer import truncate
from ..base import tool

log = get_logger("utility.discovery")

__all__ = ["list_available_tools", "tool_help", "DISCOVERY_TOOLS"]


def _registry():
    from ..registry import ToolRegistry
    from ...config import load_config

    registry = getattr(_registry, "_cache", None)
    if registry is None:
        registry = ToolRegistry()
        registry.load_all_default_tools(load_config(), verbose=False)
        _registry._cache = registry  # type: ignore[attr-defined]
    return registry


@tool(category="utility", timeout=20)
def list_available_tools(category: str = "") -> str:
    """List JARVIS's tools (optionally just one category) — use when you need a
    capability you were not given.

    Categories: browser, screen, memory, search, crawl, code, file, utility,
    communication.

    Args:
        category: Optional filter, e.g. "screen" or "browser"
    """
    registry = _registry()
    if category:
        tools = registry.get_by_category(category.strip().lower())
        if not tools:
            return (f"No tools in category '{category}'. Available: "
                    f"{', '.join(registry.categories())}")
    else:
        tools = registry.get_all_tools()
    lines = [f"{len(tools)} tool(s)" + (f" in '{category}'" if category else "") + ":"]
    for entry in tools:
        summary = truncate((entry.description or "").split("\n")[0], 90)
        lines.append(f"  {entry.name:<26} [{entry.category}] {summary}")
    return "\n".join(lines)


@tool(category="utility", timeout=20)
def tool_help(tool_name: str) -> str:
    """Explain how to call a specific tool (arguments + example).

    Args:
        tool_name: Exact tool name, e.g. "cursor_click"
    """
    registry = _registry()
    entry = registry.get(tool_name.strip())
    if entry is None:
        return (f"No tool named '{tool_name}'. "
                f"Run list_available_tools to see the options.")
    schema = entry.args_schema
    lines = [f"**{entry.name}** [{entry.category}]",
             truncate(entry.description or "", 600), "", "Arguments:"]
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not properties:
        lines.append("  (none)")
    for name, spec in properties.items():
        flag = "required" if name in required else f"optional={spec.get('default', '—')}"
        lines.append(f"  {name}: {spec.get('type', 'any')} ({flag})"
                     + (f" — {spec['description']}" if spec.get("description") else ""))
    return "\n".join(lines)


DISCOVERY_TOOLS = [list_available_tools, tool_help]
