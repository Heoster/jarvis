"""Utility tools: calculator, time, JSON, clipboard, images, discovery, memory."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .calculator import CALCULATOR_TOOLS
from .clipboard import CLIPBOARD_TOOLS
from .datetime_tool import DATETIME_TOOLS
from .discovery import DISCOVERY_TOOLS
from .image_tool import IMAGE_TOOLS
from .json_tool import JSON_TOOLS
from .memory_tools import MEMORY_TOOLS

__all__ = ["register_tools", "ALL_UTILITY_TOOLS", "ALL_MEMORY_TOOLS"]

ALL_UTILITY_TOOLS = (CALCULATOR_TOOLS + DATETIME_TOOLS + JSON_TOOLS
                     + CLIPBOARD_TOOLS + IMAGE_TOOLS + DISCOVERY_TOOLS)
ALL_MEMORY_TOOLS = MEMORY_TOOLS


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    count = registry.register_all(ALL_UTILITY_TOOLS, "utility")
    count += registry.register_all(ALL_MEMORY_TOOLS, "memory")
    return count
