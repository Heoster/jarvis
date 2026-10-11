"""Obsidian tools — vault notes, spaced-repetition flashcards, student planning.

Registered as the ``obsidian`` category.  Everything is plain markdown on disk,
so it works with Obsidian closed, with any editor, and fully offline.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .tools import OBSIDIAN_TOOLS
from .vault import ObsidianError, resolve_vault, vault_index

__all__ = ["register_tools", "OBSIDIAN_TOOLS", "ObsidianError", "resolve_vault",
           "vault_index"]


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    return registry.register_all(OBSIDIAN_TOOLS, "obsidian")
