"""Content and filename search (grep-like, pure Python, no ripgrep needed)."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ...utils.logger import get_logger
from ...utils.sanitizer import truncate
from ..base import tool

log = get_logger("fs.search")

__all__ = ["grep", "find_files", "FILE_SEARCH_TOOLS"]

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".mypy_cache", ".pytest_cache", "data", "logs",
              ".ruff_cache", ".next", "coverage"}
_BINARY_HINTS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar",
                 ".whl", ".so", ".dll", ".exe", ".pyc", ".ico", ".mp3", ".mp4",
                 ".woff", ".woff2", ".ttf", ".sqlite", ".db"}


@dataclass
class GrepMatch:
    path: str
    line_number: int
    line: str

    def render(self, *, max_line: int = 160) -> str:
        return f"{self.path}:{self.line_number}: {truncate(self.line.strip(), max_line)}"


def grep(pattern: str, root: str | Path = ".", *, glob: str = "*",
         ignore_case: bool = True, max_results: int = 60,
         regex: bool = False, context: int = 0) -> List[GrepMatch]:
    """Search file contents. ``regex=False`` means literal substring search."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as exc:
        raise ValueError(f"bad pattern: {exc}") from exc

    base = Path(root).expanduser()
    matches: List[GrepMatch] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if glob and glob != "*" and not fnmatch.fnmatch(path.name, glob):
            continue
        if path.suffix.lower() in _BINARY_HINTS:
            continue
        try:
            if path.stat().st_size > 3_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines, 1):
            if matcher.search(line):
                matches.append(GrepMatch(str(path), index, line))
                if context:
                    for offset in range(1, context + 1):
                        if index - 1 + offset < len(lines):
                            matches.append(GrepMatch(str(path), index + offset,
                                                     lines[index - 1 + offset]))
                if len(matches) >= max_results:
                    return matches
    return matches


def find_files(pattern: str, root: str | Path = ".", *,
               max_results: int = 60, files_only: bool = False) -> List[str]:
    """Find paths whose name matches a glob pattern (case-insensitive)."""
    base = Path(root).expanduser()
    needle = pattern.lower()
    out: List[str] = []
    for path in base.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if files_only and not path.is_file():
            continue
        if fnmatch.fnmatch(path.name.lower(), needle) or needle in path.name.lower():
            out.append(str(path))
            if len(out) >= max_results:
                break
    return out


# ── tools ───────────────────────────────────────────────────────────────────
@tool(category="file", timeout=90)
def file_search(pattern: str, root: str = ".", glob: str = "*",
                ignore_case: bool = True, regex: bool = False,
                max_results: int = 40) -> str:
    """Search inside files for text (like grep) — the fastest way to locate code.

    Args:
        pattern: Text to find (or a regex when regex=True)
        root: Directory to search
        glob: Only search files matching this glob, e.g. "*.py"
        ignore_case: Case-insensitive matching
        regex: Treat pattern as a regular expression
        max_results: Stop after this many matches
    """
    try:
        matches = grep(pattern, root, glob=glob, ignore_case=ignore_case,
                       max_results=max_results, regex=regex)
    except ValueError as exc:
        return f"Search failed: {exc}"
    if not matches:
        return (f"No matches for '{pattern}' under {root}"
                + (f" in {glob} files" if glob != "*" else "") + ".")
    lines = [f"{len(matches)} match(es) for '{pattern}':"]
    lines.extend(match.render() for match in matches[:max_results])
    return "\n".join(lines)


@tool(category="file", timeout=60)
def find_files_by_name(name_pattern: str, root: str = ".",
                       files_only: bool = False) -> str:
    """Find files or folders by name (supports * wildcards).

    Args:
        name_pattern: Name or glob, e.g. "*.csv", "config", "test_*.py"
        root: Where to search
        files_only: Skip directories in the results
    """
    results = find_files(name_pattern, root, files_only=files_only)
    if not results:
        return f"No files matching '{name_pattern}' under {root}."
    return f"{len(results)} path(s):\n" + "\n".join(results[:60])


FILE_SEARCH_TOOLS = [file_search, find_files_by_name]
