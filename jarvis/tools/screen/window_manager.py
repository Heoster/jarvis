"""Window management: list, focus, move, resize, minimise.

Windows are found through the best available mechanism:

* **wmctrl / xdotool** on X11 (CLI, always present on desktops)
* **AT-SPI / xdotool search** for focusing by title
* **PowerShell + user32** on Windows
* **AppleScript** on macOS

Focusing a window is the prerequisite for keyboard control, which is why this
module exists rather than being folded into the input tools.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from ...utils.logger import get_logger
from ..base import failure, tool

log = get_logger("screen.windows")

__all__ = ["WindowInfo", "list_windows", "focus_window", "move_window",
           "minimize_window", "close_window", "active_window"]


@dataclass
class WindowInfo:
    id: str
    title: str
    app: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    desktop: str = ""

    def line(self) -> str:
        geometry = f"{self.width}×{self.height}+{self.x}+{self.y}"
        return f"[{self.id}] {self.title or '(untitled)'} — {geometry}" \
               + (f" ({self.app})" if self.app else "")


def _run(argv: Sequence[str], *, timeout: float = 6.0) -> str:
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True,
                                timeout=timeout)
        return result.stdout or ""
    except Exception:
        return ""


def list_windows() -> List[WindowInfo]:
    """Enumerate visible top-level windows."""
    if sys.platform.startswith("win"):
        return _windows_list_powershell()
    if sys.platform == "darwin":
        return _list_macos()
    if shutil.which("wmctrl"):
        return _parse_wmctrl(_run(["wmctrl", "-lpG"]))
    if shutil.which("xdotool"):
        return _list_xdotool()
    return []


def _parse_wmctrl(output: str) -> List[WindowInfo]:
    windows: List[WindowInfo] = []
    for line in output.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            windows.append(WindowInfo(
                id=parts[0], desktop=parts[1], app=parts[2],
                x=int(parts[3]), y=int(parts[4]),
                width=int(parts[5]), height=int(parts[6]), title=parts[7]))
        except ValueError:
            continue
    return windows


def _list_xdotool() -> List[WindowInfo]:
    ids = _run(["xdotool", "search", "--onlyvisible", "--name", "."]).split()
    windows: List[WindowInfo] = []
    for window_id in ids[:80]:
        title = _run(["xdotool", "getwindowname", window_id]).strip()
        if not title:
            continue
        geometry = _run(["xdotool", "getwindowgeometry", "--shell", window_id])
        values: Dict[str, int] = {}
        for line in geometry.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                try:
                    values[key.strip()] = int(value)
                except ValueError:
                    continue
        windows.append(WindowInfo(id=window_id, title=title,
                                  x=values.get("X", 0), y=values.get("Y", 0),
                                  width=values.get("WIDTH", 0),
                                  height=values.get("HEIGHT", 0)))
    return windows


def _windows_list_powershell() -> List[WindowInfo]:
    script = (
        "Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | "
        "ForEach-Object { \"{0}|{1}\" -f $_.Id, $_.MainWindowTitle }"
    )
    out = _run(["powershell", "-NoProfile", "-Command", script])
    windows: List[WindowInfo] = []
    for line in out.splitlines():
        if "|" in line:
            pid, _, title = line.partition("|")
            windows.append(WindowInfo(id=pid.strip(), title=title.strip()))
    return windows


def _list_macos() -> List[WindowInfo]:
    script = ('tell application "System Events" to get name of every process '
              'whose background only is false')
    out = _run(["osascript", "-e", script])
    names = [name.strip() for name in out.split(",") if name.strip()]
    return [WindowInfo(id=str(index), title=name, app=name)
            for index, name in enumerate(names)]


def active_window() -> Optional[WindowInfo]:
    if sys.platform.startswith("linux") and shutil.which("xdotool"):
        window_id = _run(["xdotool", "getactivewindow"]).strip()
        if window_id:
            for window in list_windows():
                if window.id == window_id:
                    return window
            return WindowInfo(id=window_id,
                              title=_run(["xdotool", "getwindowname", window_id]).strip())
    windows = list_windows()
    return windows[0] if windows else None


def focus_window(title_or_id: str) -> Dict[str, Any]:
    """Bring a window to the front by id or partial title."""
    needle = str(title_or_id).strip().lower()
    windows = list_windows()
    target = None
    for window in windows:
        if window.id == title_or_id:
            target = window
            break
    if target is None:
        for window in windows:
            if needle and needle in window.title.lower():
                target = window
                break
    if target is None:
        return {"ok": False, "error": f"no window matching '{title_or_id}'",
                "windows": [w.line() for w in windows[:15]]}

    if sys.platform.startswith("win"):
        script = (f"$sig='[DllImport(\"user32.dll\")] public static extern bool "
                  f"SetForegroundWindow(int h);';"
                  f"Add-Type -MemberDefinition $sig -Name W -Namespace N;"
                  f"[N.W]::SetForegroundWindow({target.id})")
        _run(["powershell", "-NoProfile", "-Command", script])
    elif sys.platform == "darwin":
        _run(["osascript", "-e",
              f'tell application "{target.app or target.title}" to activate'])
    elif shutil.which("wmctrl"):
        _run(["wmctrl", "-ia", target.id])
    elif shutil.which("xdotool"):
        _run(["xdotool", "windowactivate", "--sync", target.id])
    return {"ok": True, "window": target.line()}


def move_window(title_or_id: str, x: int, y: int, *, width: int = 0,
                height: int = 0) -> Dict[str, Any]:
    windows = list_windows()
    target = next((w for w in windows if w.id == title_or_id), None)
    if target is None:
        target = next((w for w in windows
                       if str(title_or_id).lower() in w.title.lower()), None)
    if target is None:
        return {"ok": False, "error": f"no window matching '{title_or_id}'"}
    if shutil.which("wmctrl"):
        if width and height:
            _run(["wmctrl", "-ir", target.id, "-e", f"0,{x},{y},{width},{height}"])
        else:
            _run(["wmctrl", "-ir", target.id, "-e", f"0,{x},{y},-1,-1"])
        return {"ok": True, "window": target.line()}
    if shutil.which("xdotool"):
        _run(["xdotool", "windowmove", target.id, str(x), str(y)])
        if width and height:
            _run(["xdotool", "windowsize", target.id, str(width), str(height)])
        return {"ok": True, "window": target.line()}
    return {"ok": False, "error": "no window manager tool (install wmctrl or xdotool)"}


def minimize_window(title_or_id: str) -> Dict[str, Any]:
    if sys.platform.startswith("win"):
        return {"ok": False, "error": "use keyboard 'win+down' via keyboard_press"}
    if shutil.which("xdotool"):
        windows = list_windows()
        target = next((w for w in windows
                       if str(title_or_id).lower() in w.title.lower()
                       or w.id == title_or_id), None)
        if target:
            _run(["xdotool", "windowminimize", target.id])
            return {"ok": True, "window": target.line()}
    return {"ok": False, "error": f"cannot minimise '{title_or_id}'"}


def close_window(title_or_id: str) -> Dict[str, Any]:
    windows = list_windows()
    target = next((w for w in windows if w.id == title_or_id or
                   str(title_or_id).lower() in w.title.lower()), None)
    if target is None:
        return {"ok": False, "error": f"no window matching '{title_or_id}'"}
    if sys.platform.startswith("linux") and shutil.which("xdotool"):
        _run(["xdotool", "windowkill", target.id])
        return {"ok": True, "closed": target.line()}
    if sys.platform == "darwin":
        _run(["osascript", "-e",
              f'tell application "{target.app or target.title}" to quit'])
        return {"ok": True, "closed": target.line()}
    return {"ok": False, "error": "closing windows here needs xdotool (Linux) "
                                  "or the app itself"}


# ── tools ───────────────────────────────────────────────────────────────────
@tool(category="screen", timeout=60)
def screen_list_windows(limit: int = 30) -> str:
    """List the open windows (title, id, geometry) so you can focus one.

    Args:
        limit: How many windows to show
    """
    windows = list_windows()
    if not windows:
        return failure("No window list available. On Linux install wmctrl or xdotool "
                       "(apt-get install wmctrl xdotool).")
    return "\n".join(window.line() for window in windows[:limit])


@tool(category="screen", timeout=60)
def screen_focus_window(title: str) -> str:
    """Bring a window to the front so keyboard input reaches it.

    Args:
        title: Window title or part of it (e.g. "Chrome", "Notepad", "Code")
    """
    result = focus_window(title)
    if result.get("ok"):
        return f"✅ Focused: {result['window']}"
    hint = "\n".join(result.get("windows") or [])
    return failure(f"Could not focus '{title}'. {result.get('error', '')}\n"
                   f"Open windows:\n{hint}")


@tool(category="screen", timeout=60)
def screen_window_action(title: str, action: str, x: int = -1, y: int = -1,
                         width: int = 0, height: int = 0) -> str:
    """Move, resize, minimise or close a window.

    Args:
        title: Window title (or part of it)
        action: "focus", "move", "minimize" or "close"
        x: Target X for "move"
        y: Target Y for "move"
        width: Optional width when moving
        height: Optional height when moving
    """
    action = (action or "focus").lower()
    if action == "focus":
        return screen_window_action_focus(title)
    if action == "move":
        result = move_window(title, max(0, x), max(0, y), width=max(0, width),
                             height=max(0, height))
    elif action == "minimize":
        result = minimize_window(title)
    elif action == "close":
        result = close_window(title)
    else:
        return failure(f"Unknown action '{action}'. Use focus/move/minimize/close.")
    if not result.get("ok"):
        return failure(f"Failed to {action} '{title}': {result.get('error')}")
    return f"✅ {action}: {result.get('window') or result.get('closed')}"


def screen_window_action_focus(title: str) -> Any:
    """Focus helper that keeps `screen_window_action` callable on its own."""
    focused = screen_focus_window.invoke({"title": title})
    return (focused.ok, focused.content) if focused.ok else (False, focused.error)


@tool(category="screen", timeout=30)
def screen_active_window() -> str:
    """Report which window currently has focus (and therefore receives typing)."""
    window = active_window()
    if window is None:
        return failure("Could not determine the active window (no window manager "
                       "tools available, or nothing is focused).")
    return f"Active window: {window.line()}"


WINDOW_TOOLS = [screen_list_windows, screen_focus_window, screen_window_action,
                screen_active_window]
