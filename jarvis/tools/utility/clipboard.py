"""Clipboard access across platforms (no heavyweight dependency)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional, Tuple

from ...utils.logger import get_logger
from ..base import failure, tool

log = get_logger("utility.clipboard")

__all__ = ["get_clipboard", "set_clipboard", "CLIPBOARD_TOOLS"]


def _backends() -> Tuple[Optional[list[str]], Optional[list[str]]]:
    """Return (read_command, write_command) for this platform."""
    if sys.platform == "darwin":
        return ["pbpaste"], ["pbcopy"]
    if sys.platform.startswith("win"):
        return (["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                ["powershell", "-NoProfile", "-Command",
                 "$input | Set-Clipboard"])
    for reader in (["xclip", "-selection", "clipboard", "-o"],
                   ["xsel", "--clipboard", "--output"],
                   ["wl-paste", "--no-newline"]):
        if shutil.which(reader[0]):
            break
    else:
        reader = None
    for writer in (["xclip", "-selection", "clipboard", "-i"],
                   ["xsel", "--clipboard", "--input"],
                   ["wl-copy"]):
        if shutil.which(writer[0]):
            break
    else:
        writer = None
    return reader, writer


def _read() -> str:
    reader, _ = _backends()
    if reader is None:
        try:
            import tkinter  # type: ignore

            root = tkinter.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            return text
        except Exception as exc:
            raise RuntimeError(
                "no clipboard tool available — install xclip/xsel (Linux), or "
                "run on a desktop session") from exc
    result = subprocess.run(reader, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "clipboard read failed")
    return result.stdout


def _write(text: str) -> None:
    _, writer = _backends()
    if writer is None:
        try:
            import tkinter  # type: ignore

            root = tkinter.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.destroy()
            return
        except Exception as exc:
            raise RuntimeError("no clipboard tool available") from exc
    result = subprocess.run(writer, input=text, capture_output=True, text=True,
                            timeout=10)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "clipboard write failed")


@tool(category="utility", timeout=20)
def clipboard_get(max_chars: int = 4000) -> str:
    """Read the current clipboard contents.

    Args:
        max_chars: Truncate the result to this length
    """
    try:
        text = _read()
    except Exception as exc:
        return failure(f"Cannot read the clipboard: {exc}")
    if not text.strip():
        return "The clipboard is empty."
    body = text if len(text) <= max_chars else f"{text[:max_chars]}\n[…truncated…]"
    return f"**Clipboard ({len(text)} chars):**\n\n{body}"


@tool(category="utility", timeout=20)
def clipboard_set(text: str) -> str:
    """Copy text to the clipboard (so the operator can paste it anywhere).

    Args:
        text: The text to copy
    """
    try:
        _write(text)
    except Exception as exc:
        return f"Cannot write to the clipboard: {exc}"
    return f"✅ Copied {len(text)} characters to the clipboard"


CLIPBOARD_TOOLS = [clipboard_get, clipboard_set]
