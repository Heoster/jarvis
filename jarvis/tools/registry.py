"""Tool registry — discovery, categorisation, execution, stats.

The registry is what the brain binds to.  It also owns *parallel execution*:
independent tool calls in a single model turn are run concurrently with
``asyncio.gather``, which matters a lot on a slow CPU because search + crawl +
screenshot otherwise serialise into seconds of waiting.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .base import Tool, ToolResult, describe_return

log = get_logger("tools.registry")

__all__ = ["ToolRegistry", "ToolCallRecord"]


@dataclass
class ToolCallRecord:
    name: str
    args: Dict[str, Any]
    ok: bool
    elapsed_ms: int
    result_preview: str = ""


class ToolRegistry:
    """Holds every tool JARVIS can call."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._categories: Dict[str, List[str]] = {}
        self.history: List[ToolCallRecord] = []
        self._disabled: set[str] = set()

    # ── registration ───────────────────────────────────────────────────────
    def register(self, item: Any, category: Optional[str] = None) -> Tool:
        tool_obj = item if isinstance(item, Tool) else Tool(item, category=category or "utility")
        if category:
            tool_obj.category = category
        if tool_obj.name in self._tools:
            log.debug(f"replacing tool {tool_obj.name}")
            self._categories.get(self._tools[tool_obj.name].category, []).remove(
                tool_obj.name) if tool_obj.name in self._categories.get(
                self._tools[tool_obj.name].category, []) else None
        self._tools[tool_obj.name] = tool_obj
        self._categories.setdefault(tool_obj.category, [])
        if tool_obj.name not in self._categories[tool_obj.category]:
            self._categories[tool_obj.category].append(tool_obj.name)
        return tool_obj

    def register_all(self, items: Iterable[Any], category: Optional[str] = None) -> int:
        count = 0
        for item in items:
            if item is None:
                continue
            self.register(item, category)
            count += 1
        return count

    def unregister(self, name: str) -> bool:
        tool_obj = self._tools.pop(name, None)
        if tool_obj:
            self._categories.get(tool_obj.category, []).remove(name) \
                if name in self._categories.get(tool_obj.category, []) else None
            return True
        return False

    def disable(self, *names: str) -> None:
        self._disabled.update(names)

    def enable(self, *names: str) -> None:
        for name in names:
            self._disabled.discard(name)

    # ── introspection ──────────────────────────────────────────────────────
    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def get_all_tools(self) -> List[Tool]:
        return [t for name, t in self._tools.items() if name not in self._disabled]

    def get_by_category(self, category: str) -> List[Tool]:
        return [self._tools[n] for n in self._categories.get(category, [])
                if n in self._tools and n not in self._disabled]

    def categories(self) -> Dict[str, int]:
        return {cat: len(names) for cat, names in sorted(self._categories.items())}

    def schemas(self, tools: Optional[Sequence[Tool]] = None) -> List[Dict[str, Any]]:
        return [t.schema() for t in (tools or self.get_all_tools())]

    def names(self) -> List[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    # ── execution ──────────────────────────────────────────────────────────
    async def execute(self, tool_name: str, args: Optional[Mapping[str, Any]] = None, *,
                      timeout: Optional[float] = None) -> ToolResult:
        """Execute one tool, recording the call for the audit trail."""
        tool_obj = self._tools.get(tool_name)
        if tool_obj is None or tool_name in self._disabled:
            available = ", ".join(self.names()[:40])
            return ToolResult(ok=False, tool=tool_name,
                              error=f"unknown tool '{tool_name}'. Available: {available}")
        start = time.perf_counter()
        try:
            limit = timeout or tool_obj.timeout
            if limit:
                result = await asyncio.wait_for(tool_obj.ainvoke(args), timeout=limit)
            else:
                result = await tool_obj.ainvoke(args)
        except asyncio.TimeoutError:
            result = ToolResult(ok=False, tool=tool_name,
                                error=f"tool '{tool_name}' timed out after {limit}s")
        except Exception as exc:  # pragma: no cover - defensive
            result = ToolResult(ok=False, tool=tool_name,
                                error=f"{type(exc).__name__}: {exc}")
        elapsed = int((time.perf_counter() - start) * 1000)
        result.elapsed_ms = result.elapsed_ms or elapsed
        self.history.append(ToolCallRecord(
            name=tool_name, args=dict(args or {}), ok=result.ok,
            elapsed_ms=result.elapsed_ms,
            result_preview=describe_return(result.content or result.error, max_chars=160)))
        if len(self.history) > 500:
            self.history = self.history[-500:]
        return result

    async def execute_many(self, calls: Sequence[Mapping[str, Any]], *,
                           parallel: bool = True,
                           max_concurrency: int = 4) -> List[ToolResult]:
        """Run several tool calls, concurrently when they are independent."""
        if not calls:
            return []
        if not parallel or len(calls) == 1:
            results: List[ToolResult] = []
            for call in calls:
                results.append(await self.execute(str(call.get("name", "")),
                                                  call.get("args") or {}))
            return results

        semaphore = asyncio.Semaphore(max_concurrency)

        async def run(call: Mapping[str, Any]) -> ToolResult:
            async with semaphore:
                return await self.execute(str(call.get("name", "")),
                                          call.get("args") or {})

        return list(await asyncio.gather(*(run(call) for call in calls)))

    def execute_sync(self, tool_name: str, args: Optional[Mapping[str, Any]] = None) -> ToolResult:
        tool_obj = self._tools.get(tool_name)
        if tool_obj is None:
            return ToolResult(ok=False, tool=tool_name,
                              error=f"unknown tool '{tool_name}'")
        return tool_obj.invoke(args)

    # ── loading ────────────────────────────────────────────────────────────
    def load_all_default_tools(self, config: Optional[Mapping[str, Any]] = None,
                               *, verbose: bool = True) -> int:
        """Import and register every built-in JARVIS tool."""
        from . import (browser, communication, code, crawl, filesystem, obsidian,
                       screen, search, utility)

        modules: List[Tuple[str, Any]] = [
            ("browser", browser), ("screen", screen), ("search", search),
            ("crawl", crawl), ("code", code), ("filesystem", filesystem),
            ("utility", utility), ("communication", communication),
            ("obsidian", obsidian),
        ]
        total = 0
        for category, module in modules:
            getter = getattr(module, "register_tools", None)
            if getter is None:
                continue
            try:
                count = getter(self, config)
                total += int(count or 0)
            except Exception as exc:
                log.warning(f"could not load {category} tools: {exc}")
        if verbose:
            log.info(f"loaded {total} tools across {len(self._categories)} categories: "
                     f"{self.categories()}")
        return total

    def load_plugin(self, dotted_path: str) -> int:
        """Load tools from ``package.module`` (module-level ``TOOLS`` list or
        functions decorated with ``@tool``)."""
        module = importlib.import_module(dotted_path)
        count = 0
        for item in getattr(module, "TOOLS", []) or []:
            self.register(item)
            count += 1
        for value in vars(module).values():
            if isinstance(value, Tool):
                self.register(value)
                count += 1
        return count

    # ── stats ──────────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        top = sorted((t.stats() for t in self._tools.values()),
                     key=lambda row: row["calls"], reverse=True)[:10]
        return {"tools": len(self._tools), "categories": self.categories(),
                "calls": len(self.history),
                "failures": sum(1 for r in self.history if not r.ok),
                "top": top}

    def summary(self) -> str:
        lines = [f"{len(self._tools)} tools in {len(self._categories)} categories:"]
        for category, names in sorted(self._categories.items()):
            tools = [self._tools[n] for n in names]
            lines.append(f"  {category:<14} {len(tools):>2}  "
                         f"{', '.join(t.name for t in tools[:6])}"
                         f"{'…' if len(tools) > 6 else ''}")
        return "\n".join(lines)
