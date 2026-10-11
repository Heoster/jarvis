"""Directory listing and tree rendering (token-budgeted)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ...utils.logger import get_logger
from ..base import tool

log = get_logger("fs.tree")

__all__ = ["list_directory", "tree_view", "DIRECTORY_TOOLS"]

_DEFAULT_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
                 ".next", ".turbo", "coverage", ".cache"}


def _human(size: int) -> str:
    for unit, factor in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if size >= factor:
            return f"{size / factor:.1f}{unit}"
    return f"{size}B"


def tree_view(root: str | Path = ".", *, max_depth: int = 2, max_entries: int = 200,
              show_hidden: bool = False, skip: Optional[Set[str]] = None,
              extensions: Optional[List[str]] = None) -> str:
    """Render a directory tree as text within a strict entry budget."""
    base = Path(root).expanduser().resolve()
    if not base.exists():
        return f"Directory not found: {base}"
    skip_dirs = set(skip or _DEFAULT_SKIP)
    lines = [f"📁 {base}"]
    counter = {"shown": 0, "hidden": 0}

    def walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth or counter["shown"] >= max_entries:
            return
        try:
            entries = sorted(path.iterdir(),
                             key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return
        entries = [e for e in entries
                   if (show_hidden or not e.name.startswith("."))
                   and e.name not in skip_dirs]
        if extensions:
            wanted = {ext if ext.startswith(".") else f".{ext}" for ext in extensions}
            entries = [e for e in entries if e.is_dir() or e.suffix.lower() in wanted]
        for index, entry in enumerate(entries):
            if counter["shown"] >= max_entries:
                counter["hidden"] += 1
                continue
            last = index == len(entries) - 1
            connector = "└── " if last else "├── "
            if entry.is_dir():
                try:
                    child_count = len(list(entry.iterdir()))
                except OSError:
                    child_count = 0
                lines.append(f"{prefix}{connector}📁 {entry.name}/ ({child_count})")
                walk(entry, prefix + ("    " if last else "│   "), depth + 1)
            else:
                try:
                    size = _human(entry.stat().st_size)
                except OSError:
                    size = "?"
                lines.append(f"{prefix}{connector}📄 {entry.name} ({size})")
            counter["shown"] += 1

    walk(base, "", 0)
    if counter["hidden"]:
        lines.append(f"… {counter['hidden']} more entries not shown "
                     f"(raise max_entries to see them)")
    return "\n".join(lines)


@tool(category="file", timeout=45)
def list_directory(directory: str = ".", max_depth: int = 2,
                   show_hidden: bool = False) -> str:
    """List a directory as a tree — use this to understand a project's layout.

    Args:
        directory: Directory to list
        max_depth: Recursion depth (1 = only the top level)
        show_hidden: Include dotfiles
    """
    return tree_view(directory, max_depth=max_depth, show_hidden=show_hidden)


@tool(category="file", timeout=30)
def directory_size(directory: str = ".") -> str:
    """Report total size and file count for a directory (top-level breakdown).

    Args:
        directory: Directory to measure
    """
    base = Path(directory).expanduser().resolve()
    if not base.is_dir():
        return f"Not a directory: {base}"
    rows: List[Dict[str, Any]] = []
    total = 0
    for child in sorted(base.iterdir()):
        if child.name.startswith("."):
            continue
        try:
            if child.is_dir():
                size = sum(f.stat().st_size for f in child.rglob("*")
                           if f.is_file())
                count = sum(1 for _ in child.rglob("*") if _.is_file())
            else:
                size, count = child.stat().st_size, 1
        except OSError:
            continue
        total += size
        rows.append({"name": child.name + ("/" if child.is_dir() else ""),
                     "size": size, "files": count})
    rows.sort(key=lambda row: row["size"], reverse=True)
    lines = [f"{base}: {_human(total)} total"]
    lines.extend(f"  {_human(row['size']):>9}  {row['files']:>5} files  {row['name']}"
                 for row in rows[:25])
    return "\n".join(lines)


DIRECTORY_TOOLS = [list_directory, directory_size]
