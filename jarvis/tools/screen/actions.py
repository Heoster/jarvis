"""High-level desktop actions: launch apps, click what you see, follow workflows.

``screen_click_text`` is the interesting one — it is the complete
"see → locate → act" loop with no vision model anywhere:

1. screenshot the desktop,
2. OCR it,
3. find the requested word,
4. move the real cursor along a human-like path,
5. click the exact pixel.

That is how JARVIS drives arbitrary GUI software on a CPU-only machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...utils.logger import get_logger
from ..base import failure, tool
from .backends import get_backend
from .mouse_keyboard import click_screen_point, get_cursor_position, type_text
from .ocr_reader import find_text_in_image
from .screen_capture import capture_desktop

log = get_logger("screen.actions")

__all__ = ["launch_app", "screen_click_text", "desktop_report"]

# Common application names → per-OS launch commands.
_APP_ALIASES: Dict[str, Dict[str, List[str]]] = {
    "chrome": {"linux": ["google-chrome", "chromium", "chromium-browser"],
               "darwin": ["Google Chrome"], "windows": ["chrome.exe"]},
    "browser": {"linux": ["google-chrome", "chromium", "firefox"],
                "darwin": ["Google Chrome", "Safari"], "windows": ["chrome.exe"]},
    "firefox": {"linux": ["firefox"], "darwin": ["Firefox"], "windows": ["firefox.exe"]},
    "terminal": {"linux": ["x-terminal-emulator", "gnome-terminal", "konsole", "xterm"],
                 "darwin": ["Terminal"], "windows": ["cmd.exe", "wt.exe"]},
    "files": {"linux": ["nautilus", "dolphin", "thunar", "pcmanfm"],
              "darwin": ["Finder"], "windows": ["explorer.exe"]},
    "explorer": {"linux": ["nautilus"], "darwin": ["Finder"], "windows": ["explorer.exe"]},
    "notepad": {"linux": ["gedit", "kate", "mousepad", "nano"],
                "darwin": ["TextEdit"], "windows": ["notepad.exe"]},
    "editor": {"linux": ["code", "gedit", "kate"], "darwin": ["TextEdit"],
               "windows": ["notepad.exe"]},
    "vscode": {"linux": ["code", "codium"], "darwin": ["Visual Studio Code"],
               "windows": ["code.cmd", "code.exe"]},
    "calculator": {"linux": ["gnome-calculator", "kcalc", "galculator"],
                   "darwin": ["Calculator"], "windows": ["calc.exe"]},
    "settings": {"linux": ["gnome-control-center", "systemsettings5"],
                 "darwin": ["System Preferences"], "windows": ["ms-settings:"]},
}


def _os_family() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _spawn(argv: List[str]) -> bool:
    try:
        kwargs: Dict[str, Any] = {"stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL}
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = 0x00000008
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, **kwargs)
        return True
    except Exception as exc:
        log.debug(f"spawn failed for {argv}: {exc}")
        return False


def launch_app(name: str, *, arguments: str = "", wait: float = 1.2) -> Dict[str, Any]:
    """Start a desktop application by name/path."""
    family = _os_family()
    cleaned = (name or "").strip()
    if not cleaned:
        return {"ok": False, "error": "no application name given"}

    candidates: List[str] = []
    alias = _APP_ALIASES.get(cleaned.lower())
    if alias:
        candidates = list(alias.get(family, []))

    if family == "linux":
        for candidate in candidates + [cleaned]:
            path = shutil.which(candidate)
            if path:
                argv = [path] + (arguments.split() if arguments else [])
                if _spawn(argv):
                    time.sleep(wait)
                    return {"ok": True, "launched": candidate, "argv": argv}
        if "/" in cleaned or Path(cleaned).exists():
            if _spawn([cleaned] + (arguments.split() if arguments else [])):
                time.sleep(wait)
                return {"ok": True, "launched": cleaned}
        if shutil.which("gtk-launch"):
            if _spawn(["gtk-launch", cleaned]):
                time.sleep(wait)
                return {"ok": True, "launched": f"gtk-launch {cleaned}"}
        return {"ok": False, "error": f"'{cleaned}' not found on PATH",
                "hint": "Try a full path, or use browser_launch for a browser."}

    if family == "darwin":
        target = candidates[0] if candidates else cleaned
        if _spawn(["open", "-a", target]):
            time.sleep(wait)
            return {"ok": True, "launched": target}
        return {"ok": False, "error": f"cannot open '{target}'"}

    # windows
    target = candidates[0] if candidates else cleaned
    if target.endswith(":"):                       # ms-settings:, etc.
        if _spawn(["cmd", "/c", "start", "", target]):
            time.sleep(wait)
            return {"ok": True, "launched": target}
    if _spawn(["cmd", "/c", "start", "", target] +
              (arguments.split() if arguments else [])):
        time.sleep(wait)
        return {"ok": True, "launched": target}
    return {"ok": False, "error": f"cannot launch '{target}'"}


@tool(category="screen", timeout=60)
def launch_application(name: str, visible: bool = True) -> str:
    """Open a desktop application (Chrome, Notepad, terminal, calculator, files…).

    Args:
        name: Application name or full path, e.g. "chrome", "notepad", "terminal"
        visible: Whether the window should be visible (kept for API symmetry)
    """
    result = launch_app(name)
    if not result.get("ok"):
        return failure(f"Could not launch '{name}': {result.get('error')}. "
                       f"{result.get('hint', '')}")
    return f"✅ Launched {result['launched']} — use screen_focus_window to bring it to front"


@tool(category="screen", timeout=150)
def screen_click_text(text: str, monitor: int = 1, double: bool = False,
                      min_confidence: float = 45.0) -> str:
    """Find text on screen with OCR and click it with the real cursor.

    This is the no-vision-model way to operate any GUI: it reads the screen as
    text, locates the words, then moves the mouse there like a person would.
    Use it for buttons in native apps, PDF viewers, installers, games.

    Args:
        text: The visible text to click (e.g. "Save", "Download", "Continue")
        monitor: Monitor to search (1 = primary)
        double: Double-click instead of single click
        min_confidence: OCR confidence threshold (0-100)
    """
    try:
        path = capture_desktop(monitor=monitor)
    except Exception as exc:
        return failure(f"Cannot capture the screen: {exc}")
    matches = find_text_in_image(path, text, min_confidence=min_confidence)
    if not matches:
        return failure(f"Could not find '{text}' on screen. Run `read_screen_text` "
                       "to see what is currently visible.")
    word = matches[0]
    cx, cy = word.center
    result = click_screen_point(cx, cy, clicks=2 if double else 1)
    if not result.get("ok"):
        return failure(f"Located '{word.text}' at ({cx}, {cy}) but the click failed — "
                       f"backend '{result['backend']}' could not send input.")
    others = f" ({len(matches) - 1} other match(es) ignored)" if len(matches) > 1 else ""
    return (f"✅ Clicked '{word.text}' at ({cx}, {cy}) via {result['backend']}"
            f"{others}")


@tool(category="screen", timeout=120)
def screen_fill_field(label: str, value: str, clear_first: bool = True) -> str:
    """Click a labelled field on screen (OCR), then type a value into it.

    Args:
        label: Visible label/placeholder text next to the field
        value: Text to type
        clear_first: Select existing content first (ctrl+a) before typing
    """
    clicked = screen_click_text.invoke({"text": label})
    if not clicked.ok:
        return failure(clicked.error or clicked.content)   # precise reason
    found = clicked.content
    if clear_first:
        backend = get_backend()
        backend.press_key("ctrl+a")
        time.sleep(0.08)
        backend.press_key("delete")
        time.sleep(0.08)
    typed = type_text(value, interval=0.015)
    if isinstance(typed, tuple) and typed[0] is False:
        return typed
    return f"{found}\n{typed}"


@tool(category="screen", timeout=30)
def run_in_terminal_window(command: str, wait_seconds: float = 0.0) -> str:
    """Run a shell command in a visible terminal window (for the operator to watch).

    Args:
        command: Shell command to execute
        wait_seconds: Seconds to wait after launching before returning
    """
    from ..browser.terminal_launcher import run_in_terminal

    result = run_in_terminal(command, visible=True, wait=wait_seconds > 0,
                             timeout=max(1.0, wait_seconds))
    return result.get("output") or result.get("error", "")


def desktop_report() -> str:
    """Capability report for the CLI."""
    from .screen_capture import screen_tools_report

    lines = [screen_tools_report()]
    try:
        x, y = get_cursor_position()
        lines.append(f"cursor       : ({x}, {y})")
    except Exception:
        pass
    return "\n".join(lines)


ACTION_TOOLS = [launch_application, screen_click_text, screen_fill_field,
                run_in_terminal_window]
