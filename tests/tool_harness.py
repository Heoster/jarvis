"""The shortest path to testing a tool: no model, no provider, no network.

A tool test should read like the call the model would make, and nothing else::

    from tool_harness import run_tool

    def test_calculator_answers():
        result = run_tool("calculate", expression="2 + 2 * 3")
        assert result.ok
        assert "8" in result.content

That is the whole point of this module: the model is *not* involved. The tool is
resolved through the real registry (the same object the brain binds to), so the
test exercises the real schema, the real coercion and the real error handling —
but nothing ever calls a provider.

Three helpers, all offline:

- :func:`tool_registry` — a registry loaded with the project's own tools;
- :func:`run_tool` — call one by name with keyword arguments, get a
  :class:`~jarvis.tools.base.ToolResult` back (async tools included);
- :func:`assert_clean` — assert the call did not *crash*: either it worked, or
  it explained itself in a message a model could act on.

For probes that must run against every tool, see
:mod:`jarvis.tools.selftest` (``python -m jarvis tools --smoke``) — this module
is for the focused, per-tool test you are about to write.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Mapping, Optional

from jarvis.tools.base import ToolResult
from jarvis.tools.registry import ToolRegistry

__all__ = ["tool_registry", "run_tool", "assert_clean", "CRASH_RE"]

#: An exception that escaped the tool — ``ToolResult.error`` keeps the type name.
CRASH_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Signal)"
                      r"(?:\s*\(.*\))?\s*:")

_DEFAULT_REGISTRY: Optional[ToolRegistry] = None


def tool_registry(config: Optional[Mapping[str, Any]] = None) -> ToolRegistry:
    """A registry with every built-in tool, loaded lazily and cached."""
    global _DEFAULT_REGISTRY
    if config is not None or _DEFAULT_REGISTRY is None:
        from jarvis.config import load_config

        registry = ToolRegistry()
        registry.load_all_default_tools(config or load_config(), verbose=False)
        if config is None:
            _DEFAULT_REGISTRY = registry
        return registry
    return _DEFAULT_REGISTRY


def run_tool(name: str, *args: Any, timeout: float = 60.0, **kwargs: Any) -> ToolResult:
    """Call ``name`` with ``kwargs`` (and positional ``args``) and return the result.

    Async tools are awaited here, so the caller stays synchronous:

        result = run_tool("media_history", day="yesterday")
    """
    payload: Any = args if args else kwargs
    return asyncio.run(tool_registry().execute(name, payload, timeout=timeout))


def assert_clean(result: ToolResult, *, expect_ok: bool = False) -> ToolResult:
    """Assert the tool did not crash — and, with ``expect_ok``, that it worked.

    "Clean" means: either it succeeded, or it failed with a readable reason
    (missing dependency, no vault configured, file not found). A raw
    ``ValueError: …`` in ``result.error`` means nobody handled the input, which
    is always a bug worth failing a test over.
    """
    assert result is not None, "the tool returned nothing"
    if result.ok:
        return result
    error = str(result.error or "")
    assert not CRASH_RE.match(error.strip()), (
        f"the tool crashed instead of explaining itself: {error}\n"
        "wrap the failure in failure(...) so the model can act on it")
    assert not expect_ok, f"expected the tool to succeed, it said: {error}"
    return result
