"""Browser control tools (focus area #2)."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .actions import BROWSER_TOOLS
from .media import MEDIA_TOOLS
from .screenshot import SCREENSHOT_TOOLS

__all__ = ["register_tools", "BROWSER_TOOLS", "MEDIA_TOOLS", "SCREENSHOT_TOOLS"]


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    """Register every browser tool into ``registry``."""
    count = registry.register_all(BROWSER_TOOLS, "browser")
    count += registry.register_all(MEDIA_TOOLS, "browser")
    count += registry.register_all(SCREENSHOT_TOOLS, "browser")
    return count
