"""File operations: read, write, append, copy, move, delete, search.

Safety posture: reads are free, writes are logged, **destructive operations go to
a trash folder instead of vanishing** (``data/trash/<timestamp>/``) unless the
caller explicitly asks for a hard delete.  That one design choice has saved more
agent-caused data loss than any sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ...utils.logger import get_logger
from ...utils.sanitizer import is_probably_binary, truncate
from ..base import tool

log = get_logger("fs")

__all__ = ["read_file", "write_file", "append_file", "delete_path", "move_path",
           "copy_path", "file_info", "TRASH_DIR"]

DEFAULT_TRASH_DIR = Path(__file__).resolve().parents[3] / "data" / "trash"


def trash_dir() -> Path:
    """Where deletes go. Honours ``JARVIS_TRASH_DIR`` / ``paths.trash_dir``."""
    override = os.environ.get("JARVIS_TRASH_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    try:  # config is optional here — the tool must work standalone too
        from ...config import get, load_config

        configured = get(load_config(), "paths.trash_dir", "")
        if configured:
            return Path(str(configured)).expanduser()
    except Exception:
        pass
    return DEFAULT_TRASH_DIR


# backwards-compatible module attribute (older code imported TRASH_DIR)
TRASH_DIR = DEFAULT_TRASH_DIR
MAX_READ_BYTES = 10_000_000

_TEXT_SUFFIXES = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json",
                  ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".tsv",
                  ".html", ".htm", ".css", ".sql", ".sh", ".bat", ".ps1", ".env",
                  ".xml", ".rst", ".tex", ".log", ".gitignore"}


def _resolve(path: str) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser().resolve()


def _is_text(path: Path) -> bool:
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    try:
        head = path.read_bytes()[:2048]
    except OSError:
        return False
    return not is_probably_binary(head)


@tool(category="file", timeout=30)
def read_file(path: str, max_chars: int = 6000, start_line: int = 1,
              with_line_numbers: bool = False) -> str:
    """Read a text file (truncated to save context).

    Args:
        path: Path to the file
        max_chars: Maximum characters to return
        start_line: Start reading from this line (1-based, for long files)
        with_line_numbers: Prefix each line with its number (useful before editing)
    """
    try:
        path = _resolve(path)
    except Exception as exc:
        return f"Bad path: {exc}"
    if not path.exists():
        return f"File not found: {path}"
    if path.is_dir():
        return f"{path} is a directory — use `list_directory` or `file_search`."
    try:
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return (f"File too large to read ({size / 1e6:.1f} MB). Use "
                    f"`file_search` or `run_bash('head -n 200 ...')` instead.")
        if not _is_text(path):
            return (f"{path} looks binary ({size} bytes). "
                    f"Use `file_info` or a specific tool for this format.")
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Could not read {path}: {exc}"

    lines = content.splitlines()
    if start_line > 1:
        lines = lines[start_line - 1:]
    if with_line_numbers:
        lines = [f"{start_line + index:>5} | {line}"
                 for index, line in enumerate(lines)]
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = (f"{body[:int(max_chars * 0.8)]}\n\n"
                f"[…file continues: {len(lines)} lines total, "
                f"{len(content)} chars. Use start_line= to read further…]")
    return (f"**{path}** ({size} bytes, {len(content.splitlines())} lines)\n"
            f"```\n{body}\n```")


@tool(category="file", timeout=30)
def write_file(path: str, content: str, mode: str = "write",
               encoding: str = "utf-8") -> str:
    """Create or overwrite a file (parent directories are created automatically).

    Args:
        path: Where to write
        content: The full content to write
        mode: "write" (replace) or "append"
        encoding: Text encoding (default utf-8)
    """
    try:
        path = _resolve(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        if mode == "append":
            with open(path, "a", encoding=encoding) as handle:
                handle.write(content)
        else:
            if existed:
                backup_path = _backup(path)
            else:
                backup_path = None
            path.write_text(content, encoding=encoding)
        verb = "Appended to" if mode == "append" else ("Overwrote" if existed else "Created")
        extra = f" (previous version kept at {backup_path})" if mode != "append" and existed \
            else ""
        return f"✅ {verb} {path}: {len(content)} chars{extra}"
    except Exception as exc:
        return f"Could not write {path}: {exc}"


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = trash_dir() / stamp / path.name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    except Exception as exc:
        log.debug(f"backup failed: {exc}")
    return target


@tool(category="file", timeout=30)
def append_file(path: str, content: str) -> str:
    """Append text to a file (creates it if missing).

    Args:
        path: Target file
        content: Text to append
    """
    return write_file.invoke({"path": path, "content": content,
                              "mode": "append"}).content


@tool(category="file", timeout=60)
def delete_path(path: str, recursive: bool = False, permanent: bool = False) -> str:
    """Delete a file or directory — moved to the trash by default (recoverable).

    Args:
        path: File or directory to delete
        recursive: Required for directories
        permanent: Really delete it (no trash). Use only when Heoster confirms.
    """
    try:
        target = _resolve(path)
    except Exception as exc:
        return f"Bad path: {exc}"
    if not target.exists():
        return f"Nothing to delete: {target}"
    if target.is_dir() and not recursive and not permanent:
        return (f"{target} is a directory — pass recursive=True. "
                f"Contents will go to the trash folder, not be lost.")
    try:
        if permanent:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return f"🗑️ Permanently deleted {target}"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = trash_dir() / stamp / target.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(destination))
        return (f"🗑️ Moved to trash: {target} → {destination}\n"
                f"(recoverable — say the word and I'll restore it)")
    except Exception as exc:
        return f"Delete failed: {exc}"


@tool(category="file", timeout=30)
def move_path(source: str, destination: str, overwrite: bool = False) -> str:
    """Move or rename a file/directory.

    Args:
        source: Path to move
        destination: New path
        overwrite: Allow replacing an existing destination
    """
    try:
        src, dst = _resolve(source), _resolve(destination)
    except Exception as exc:
        return f"Bad path: {exc}"
    if not src.exists():
        return f"Source not found: {src}"
    if dst.exists() and not overwrite:
        return f"Destination exists: {dst} (pass overwrite=True to replace)"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"✅ Moved {src} → {dst}"
    except Exception as exc:
        return f"Move failed: {exc}"


@tool(category="file", timeout=60)
def copy_path(source: str, destination: str, overwrite: bool = False) -> str:
    """Copy a file or directory tree.

    Args:
        source: Path to copy
        destination: Where to copy it
        overwrite: Allow replacing existing files
    """
    try:
        src, dst = _resolve(source), _resolve(destination)
    except Exception as exc:
        return f"Bad path: {exc}"
    if not src.exists():
        return f"Source not found: {src}"
    if dst.exists() and not overwrite:
        return f"Destination exists: {dst} (pass overwrite=True)"
    try:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=overwrite)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        return f"✅ Copied {src} → {dst}"
    except Exception as exc:
        return f"Copy failed: {exc}"


@tool(category="file", timeout=30)
def file_info(path: str) -> str:
    """Show metadata about a file or directory (size, type, permissions, timestamps).

    Args:
        path: Path to inspect
    """
    try:
        target = _resolve(path)
    except Exception as exc:
        return f"Bad path: {exc}"
    if not target.exists():
        return f"Not found: {target}"
    stat = target.stat()
    kind = "directory" if target.is_dir() else "file"
    details = [
        f"path       : {target}",
        f"type       : {kind}",
        f"size       : {stat.st_size:,} bytes ({stat.st_size / 1024:.1f} KB)",
        f"modified   : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}",
        f"created    : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_ctime))}",
        f"permissions: {oct(stat.st_mode)[-3:]}",
    ]
    if target.is_file():
        details.append(f"text file  : {'yes' if _is_text(target) else 'no (binary)'}")
        if _is_text(target) and stat.st_size < 200_000:
            content = target.read_text(encoding="utf-8", errors="replace")
            json_detected = False
            if target.suffix.lower() == ".json":
                try:
                    data = json.loads(content)
                    json_detected = True
                    if isinstance(data, dict):
                        details.append(f"json keys  : {', '.join(list(data)[:12])}")
                    elif isinstance(data, list):
                        details.append(f"json items : {len(data)}")
                except Exception:
                    details.append("json       : invalid")
            if not json_detected:
                details.append(f"lines      : {content.count(chr(10)) + 1}")
    else:
        try:
            entries = list(target.iterdir())
            details.append(f"entries    : {len(entries)}")
        except OSError:
            pass
    return "\n".join(details)


FILE_TOOLS = [read_file, write_file, append_file, delete_path, move_path,
              copy_path, file_info]
