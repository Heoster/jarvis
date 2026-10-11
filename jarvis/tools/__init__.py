"""JARVIS tools: registry + every capability package."""

from __future__ import annotations

from .base import Tool, ToolResult, tool
from .registry import ToolRegistry, ToolCallRecord

__all__ = ["Tool", "ToolResult", "tool", "ToolRegistry", "ToolCallRecord",
           "build_registry"]


def build_registry(config=None, *, verbose: bool = False) -> ToolRegistry:
    """Create a registry with every built-in tool loaded."""
    if config is None:
        from ..config import load_config

        config = load_config()
    registry = ToolRegistry()
    registry.load_all_default_tools(config, verbose=verbose)
    return registry
