"""Filesystem tools."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .directory_tree import DIRECTORY_TOOLS
from .file_ops import FILE_TOOLS
from .file_search import FILE_SEARCH_TOOLS

__all__ = ["register_tools"]

ALL_FILE_TOOLS = FILE_TOOLS + DIRECTORY_TOOLS + FILE_SEARCH_TOOLS


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    return registry.register_all(ALL_FILE_TOOLS, "file")
