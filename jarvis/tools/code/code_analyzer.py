"""Code analysis helpers (AST, diffs, project snapshots).

Used by the code agent to understand a repository before editing: what languages
are present, how big the files are, where a symbol is defined, and what a patch
would actually change.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from ...utils.sanitizer import truncate
from ..base import tool

log = get_logger("code.analyzer")

__all__ = ["analyse_python_source", "find_symbol", "diff_texts", "LanguageStats",
           "scan_project"]

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist",
              "build", ".mypy_cache", ".pytest_cache", "data", "logs"}


@dataclass
class Symbol:
    name: str
    kind: str            # function | class | method | async function
    path: str
    lineno: int
    docstring: str = ""

    def line(self) -> str:
        doc = f" — {truncate(self.docstring.splitlines()[0], 70)}" if self.docstring else ""
        return f"{self.path}:{self.lineno} {self.kind} {self.name}{doc}"


def analyse_python_source(source: str, *, path: str = "<memory>") -> Dict[str, Any]:
    """Structure of a Python file: symbols, imports, complexity proxies."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {"ok": False, "error": f"syntax error line {exc.lineno}: {exc.msg}",
                "path": path}
    symbols: List[Symbol] = []
    imports: List[str] = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async function" if isinstance(child, ast.AsyncFunctionDef) else "function"
                if prefix:
                    kind = "method"
                symbols.append(Symbol(name=f"{prefix}{child.name}", kind=kind,
                                      path=path, lineno=child.lineno,
                                      docstring=ast.get_docstring(child) or ""))
                visit(child, prefix=f"{prefix}{child.name}." if prefix else "")
            elif isinstance(child, ast.ClassDef):
                symbols.append(Symbol(name=f"{prefix}{child.name}", kind="class",
                                      path=path, lineno=child.lineno,
                                      docstring=ast.get_docstring(child) or ""))
                visit(child, prefix=f"{child.name}.")
            elif isinstance(child, ast.Import):
                imports.extend(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                module = child.module or ""
                imports.extend(f"{module}.{alias.name}" for alias in child.names)
            else:
                visit(child, prefix)

    visit(tree)
    return {"ok": True, "path": path, "symbols": symbols, "imports": imports,
            "lines": len(source.splitlines()),
            "classes": sum(1 for s in symbols if s.kind == "class"),
            "functions": sum(1 for s in symbols if "function" in s.kind)}


def find_symbol(root: str | Path, symbol: str, *, max_hits: int = 30) -> List[Symbol]:
    """Locate a class/function name across a Python project."""
    hits: List[Symbol] = []
    for path in Path(root).rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if symbol not in source:
            continue
        report = analyse_python_source(source, path=str(path))
        for item in report.get("symbols", []):
            if symbol.lower() in item.name.lower():
                hits.append(item)
        if len(hits) >= max_hits:
            break
    return hits


def diff_texts(before: str, after: str, *, path: str = "file",
               context: int = 3) -> str:
    """Unified diff between two strings (what a patch would do)."""
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                fromfile=f"a/{path}", tofile=f"b/{path}",
                                lineterm="", n=context)
    return "\n".join(diff)


@dataclass
class LanguageStats:
    extension: str
    files: int = 0
    lines: int = 0
    bytes: int = 0


def scan_project(root: str | Path, *, max_files: int = 4000) -> Dict[str, Any]:
    """Quick project inventory (languages, sizes, biggest files)."""
    root_path = Path(root)
    stats: Dict[str, LanguageStats] = {}
    biggest: List[Tuple[int, str]] = []
    total_files = 0
    for path in root_path.rglob("*"):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        total_files += 1
        if total_files > max_files:
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        extension = path.suffix.lower() or "(none)"
        entry = stats.setdefault(extension, LanguageStats(extension=extension))
        entry.files += 1
        entry.bytes += size
        if size < 2_000_000:
            try:
                entry.lines += path.read_text(encoding="utf-8", errors="ignore").count("\n")
            except OSError:
                pass
        biggest.append((size, str(path.relative_to(root_path))))
    biggest.sort(reverse=True)
    return {"root": str(root_path), "files": min(total_files, max_files),
            "languages": sorted(
                ({"ext": s.extension, "files": s.files, "lines": s.lines}
                 for s in stats.values()),
                key=lambda row: row["lines"], reverse=True)[:12],
            "largest": [{"path": p, "kb": round(b / 1024, 1)} for b, p in biggest[:10]]}


# ── tools ───────────────────────────────────────────────────────────────────
@tool(category="code", timeout=60)
def code_outline(path: str) -> str:
    """List the classes/functions in a Python file (better than reading it raw).

    Args:
        path: Path to the .py file
    """
    target = Path(path).expanduser()
    if not target.exists():
        return f"File not found: {target}"
    report = analyse_python_source(target.read_text(encoding="utf-8", errors="replace"),
                                   path=str(target))
    if not report["ok"]:
        return f"Cannot parse {target}: {report['error']}"
    lines = [f"{target.name}: {report['lines']} lines, "
             f"{report['classes']} classes, {report['functions']} functions",
             f"imports: {', '.join(sorted(set(report['imports']))[:20]) or 'none'}", ""]
    lines.extend(symbol.line() for symbol in report["symbols"][:80])
    return "\n".join(lines)


@tool(category="code", timeout=90)
def code_find_symbol(symbol: str, root: str = ".") -> str:
    """Find where a class/function is defined across the project.

    Args:
        symbol: Name (or partial name) to search for
        root: Directory to search from
    """
    hits = find_symbol(root, symbol)
    if not hits:
        return f"No definition of '{symbol}' found under {root}."
    return f"{len(hits)} match(es):\n" + "\n".join(hit.line() for hit in hits[:30])


@tool(category="code", timeout=60)
def code_project_summary(root: str = ".") -> str:
    """Summarise a project: languages, sizes, largest files.

    Args:
        root: Project directory
    """
    summary = scan_project(root)
    lines = [f"Project: {summary['root']} ({summary['files']} files scanned)",
             "languages:"]
    for row in summary["languages"]:
        lines.append(f"  {row['ext']:<10} {row['files']:>4} files "
                     f"{row['lines']:>7} lines")
    lines.append("largest files:")
    for item in summary["largest"][:8]:
        lines.append(f"  {item['kb']:>8.1f} KB  {item['path']}")
    return "\n".join(lines)
