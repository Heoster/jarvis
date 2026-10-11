"""OS input backends for screen control.

JARVIS must be able to move the *real* cursor and type on whatever machine it
runs on.  Rather than depending on one library, it probes a chain of backends:

===============  =====================================================
``pyautogui``    cross-platform Python library (preferred)
``xdotool``      X11 CLI — works when pyautogui is not installed
``ydotool``      Wayland CLI (via ``ydotoold``)
``powershell``   Windows: user32 SendInput through PowerShell
``osascript``    macOS: System Events / ``cliclick``
``null``         nothing available — every call returns a clear error
===============  =====================================================

Each backend implements: ``move``, ``click``, ``scroll``, ``type_text``,
``press_key``, ``position``, ``screen_size``, ``available``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ...utils.logger import get_logger
from ...utils.platform_probe import has_display, probe_platform

log = get_logger("screen.backends")

__all__ = ["InputBackend", "PyAutoGUIBackend", "XdotoolBackend", "YdotoolBackend",
           "PowerShellBackend", "OsascriptBackend", "NullBackend", "get_backend",
           "choose_backend", "list_backends"]


@dataclass
class ScreenInfo:
    width: int = 0
    height: int = 0
    scale: float = 1.0


class InputBackend:
    """Interface every backend implements."""

    name = "base"

    def available(self) -> Tuple[bool, str]:
        return False, "not implemented"

    # input primitives
    def move(self, x: int, y: int, *, duration: float = 0.0) -> bool:
        raise NotImplementedError

    def click(self, x: Optional[int] = None, y: Optional[int] = None, *,
              button: str = "left", clicks: int = 1) -> bool:
        raise NotImplementedError

    def scroll(self, amount: int, *, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        raise NotImplementedError

    def type_text(self, text: str, *, interval: float = 0.0) -> bool:
        raise NotImplementedError

    def press_key(self, key: str) -> bool:
        raise NotImplementedError

    def button_down(self, button: str = "left") -> bool:
        """Hold a mouse button (needed for drag-and-drop)."""
        return False

    def button_up(self, button: str = "left") -> bool:
        return False

    def position(self) -> Tuple[int, int]:
        raise NotImplementedError

    def screen_size(self) -> ScreenInfo:
        raise NotImplementedError


class PyAutoGUIBackend(InputBackend):
    """Cross-platform backend — the default when pyautogui is importable."""

    name = "pyautogui"

    def __init__(self, *, failsafe: bool = True, pause: float = 0.05):
        self._gui = None
        self._failsafe = failsafe
        self._pause = pause

    def _lib(self) -> Any:
        if self._gui is None:
            import pyautogui  # type: ignore

            pyautogui.FAILSAFE = self._failsafe
            pyautogui.PAUSE = self._pause
            self._gui = pyautogui
        return self._gui

    def available(self) -> Tuple[bool, str]:
        try:
            import pyautogui  # noqa: F401
        except Exception as exc:
            return False, f"pip install pyautogui ({exc})"
        if not has_display() and sys.platform not in ("win32", "darwin"):
            return False, "no DISPLAY (X11/Wayland) — run under Xvfb or a desktop"
        return True, "native OS input"

    def move(self, x: int, y: int, *, duration: float = 0.0) -> bool:
        self._lib().moveTo(int(x), int(y), duration=max(0.0, float(duration)))
        return True

    def click(self, x: Optional[int] = None, y: Optional[int] = None, *,
              button: str = "left", clicks: int = 1) -> bool:
        gui = self._lib()
        kwargs = {"button": button, "clicks": int(clicks)}
        if x is not None and y is not None:
            kwargs["x"], kwargs["y"] = int(x), int(y)
        gui.click(**kwargs)
        return True

    def scroll(self, amount: int, *, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        gui = self._lib()
        if x is not None and y is not None:
            gui.moveTo(int(x), int(y))
        gui.scroll(int(amount))
        return True

    def type_text(self, text: str, *, interval: float = 0.0) -> bool:
        self._lib().typewrite(text, interval=max(0.0, float(interval)))
        return True

    def press_key(self, key: str) -> bool:
        gui = self._lib()
        if "+" in key:
            gui.hotkey(*[part.strip().lower() for part in key.split("+")])
        else:
            gui.press(key.strip().lower())
        return True

    def button_down(self, button: str = "left") -> bool:
        self._lib().mouseDown(button=button)
        return True

    def button_up(self, button: str = "left") -> bool:
        self._lib().mouseUp(button=button)
        return True

    def position(self) -> Tuple[int, int]:
        point = self._lib().position()
        return int(point.x), int(point.y)

    def screen_size(self) -> ScreenInfo:
        size = self._lib().size()
        return ScreenInfo(width=int(size.width), height=int(size.height))


class _SubprocessBackend(InputBackend):
    """Shared helpers for CLI-driven backends."""

    def _run(self, argv: List[str], *, timeout: float = 6.0) -> Tuple[bool, str]:
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=timeout)
            return result.returncode == 0, (result.stdout or result.stderr).strip()
        except Exception as exc:
            return False, str(exc)


class XdotoolBackend(_SubprocessBackend):
    """X11 via xdotool — tiny, always available on Linux desktops."""

    name = "xdotool"

    def available(self) -> Tuple[bool, str]:
        if sys.platform.startswith("win") or sys.platform == "darwin":
            return False, "xdotool is Linux/X11 only"
        if not shutil.which("xdotool"):
            return False, "apt-get install xdotool"
        if not os.environ.get("DISPLAY"):
            return False, "no DISPLAY"
        ok, _ = self._run(["xdotool", "getdisplaygeometry"])
        return (True, "X11 CLI input") if ok else (False, "xdotool cannot reach the display")

    def move(self, x: int, y: int, *, duration: float = 0.0) -> bool:
        ok, _ = self._run(["xdotool", "mousemove", "--sync", str(int(x)), str(int(y))])
        return ok

    def click(self, x: Optional[int] = None, y: Optional[int] = None, *,
              button: str = "left", clicks: int = 1) -> bool:
        if x is not None and y is not None:
            self.move(x, y)
        button_num = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        repeat = ["--repeat", str(int(clicks)), "--delay", "60"] if clicks > 1 else []
        ok, _ = self._run(["xdotool", "click", *repeat, button_num])
        return ok

    def scroll(self, amount: int, *, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        if x is not None and y is not None:
            self.move(x, y)
        button = "4" if amount > 0 else "5"
        steps = max(1, abs(int(amount)) // 100)
        ok = True
        for _ in range(steps):
            success, _ = self._run(["xdotool", "click", button])
            ok = ok and success
        return ok

    def type_text(self, text: str, *, interval: float = 0.0) -> bool:
        delay = max(0, int(interval * 1000))
        ok, _ = self._run(["xdotool", "type", "--delay", str(delay), "--", text],
                          timeout=30.0)
        return ok

    def press_key(self, key: str) -> bool:
        combo = "+".join(part.strip().lower() for part in key.split("+"))
        ok, _ = self._run(["xdotool", "key", "--clearmodifiers", combo])
        return ok

    def button_down(self, button: str = "left") -> bool:
        button_num = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        ok, _ = self._run(["xdotool", "mousedown", button_num])
        return ok

    def button_up(self, button: str = "left") -> bool:
        button_num = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        ok, _ = self._run(["xdotool", "mouseup", button_num])
        return ok

    def position(self) -> Tuple[int, int]:
        ok, out = self._run(["xdotool", "getmouselocation", "--shell"])
        if not ok:
            return 0, 0
        values: Dict[str, int] = {}
        for line in out.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                try:
                    values[key.strip()] = int(value)
                except ValueError:
                    continue
        return values.get("X", 0), values.get("Y", 0)

    def screen_size(self) -> ScreenInfo:
        ok, out = self._run(["xdotool", "getdisplaygeometry"])
        if ok and " " in out:
            width, _, height = out.partition(" ")
            try:
                return ScreenInfo(width=int(width), height=int(height))
            except ValueError:
                pass
        return ScreenInfo()


class YdotoolBackend(_SubprocessBackend):
    """Wayland via ydotool (requires the ydotoold daemon)."""

    name = "ydotool"

    def available(self) -> Tuple[bool, str]:
        if not shutil.which("ydotool"):
            return False, "install ydotool + ydotoold (Wayland)"
        ok, _ = self._run(["ydotool", "mousemove", "-x", "0", "-y", "0"])
        if not ok:
            return False, "ydotoold daemon not running"
        return True, "Wayland CLI input"


class PowerShellBackend(_SubprocessBackend):
    """Windows input through user32 SendInput (no extra Python deps)."""

    name = "powershell"

    MOUSE_JS = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;using System.Runtime.InteropServices;
public class JvMouse {{
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint dx,uint dy,uint d,uint e);
  public const uint LEFTDOWN=0x02, LEFTUP=0x04, RIGHTDOWN=0x08, RIGHTUP=0x10;
  public static void Down(bool right) { mouse_event(right?RIGHTDOWN:LEFTDOWN,0,0,0,0); }
  public static void Up(bool right) { mouse_event(right?RIGHTUP:LEFTUP,0,0,0,0); }
  public static void Move(int x,int y) {{ SetCursorPos(x,y); }}
  public static void Click(bool right, int times) {{
    for (int i=0;i<times;i++) {{
      mouse_event(right?RIGHTDOWN:LEFTDOWN,0,0,0,0);
      mouse_event(right?RIGHTUP:LEFTUP,0,0,0,0);
      System.Threading.Thread.Sleep(40);
    }}
  }}
}}
"@
JvMouse::{action}
"""

    def available(self) -> Tuple[bool, str]:
        if not sys.platform.startswith("win"):
            return False, "Windows only"
        ok, _ = self._run(["powershell", "-NoProfile", "-Command", "echo ok"])
        return (True, "user32 SendInput via PowerShell") if ok else \
            (False, "powershell not found on PATH")

    def _ps(self, script: str, timeout: float = 15.0) -> Tuple[bool, str]:
        return self._run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          script], timeout=timeout)

    def move(self, x: int, y: int, *, duration: float = 0.0) -> bool:
        ok, _ = self._ps(self.MOUSE_JS.format(action=f"Move({int(x)},{int(y)});"))
        return ok

    def click(self, x: Optional[int] = None, y: Optional[int] = None, *,
              button: str = "left", clicks: int = 1) -> bool:
        prefix = ""
        if x is not None and y is not None:
            prefix = f"JvMouse::Move({int(x)},{int(y)});"
        script = self.MOUSE_JS.format(
            action=f"{prefix}JvMouse::Click(${'true' if button == 'right' else 'false'},"
                   f"{int(clicks)});")
        ok, _ = self._ps(script)
        return ok

    def scroll(self, amount: int, *, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        direction = "up" if amount > 0 else "down"
        steps = max(1, abs(int(amount)) // 100)
        script = ("Add-Type -AssemblyName System.Windows.Forms;"
                  f"for($i=0;$i -lt {steps};$i++){{"
                  f"[System.Windows.Forms.SendKeys]::SendWait('{{{direction}}}');"
                  "Start-Sleep -Milliseconds 30}")
        ok, _ = self._ps(script)
        return ok

    def type_text(self, text: str, *, interval: float = 0.0) -> bool:
        escaped = text.replace("'", "''").replace("+", "{+}").replace("^", "{^}")
        script = ("Add-Type -AssemblyName System.Windows.Forms;"
                  f"[System.Windows.Forms.SendKeys]::SendWait('{escaped}')")
        ok, _ = self._ps(script)
        return ok

    def press_key(self, key: str) -> bool:
        mapping = {"enter": "{ENTER}", "tab": "{TAB}", "escape": "{ESC}",
                   "esc": "{ESC}", "space": " ", "backspace": "{BACKSPACE}",
                   "delete": "{DELETE}", "up": "{UP}", "down": "{DOWN}",
                   "left": "{LEFT}", "right": "{RIGHT}", "pageup": "{PGUP}",
                   "pagedown": "{PGDN}", "home": "{HOME}", "end": "{END}",
                   "f5": "{F5}", "f11": "{F11}", "f12": "{F12}", "win": "{LWIN}"}
        parts = [part.strip().lower() for part in key.split("+")]
        modifiers = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+"}
        prefix = "".join(modifiers[p] for p in parts[:-1] if p in modifiers)
        last = parts[-1]
        token = mapping.get(last, last if len(last) == 1 else f"{{{last.upper()}}}")
        script = ("Add-Type -AssemblyName System.Windows.Forms;"
                  f"[System.Windows.Forms.SendKeys]::SendWait('{prefix}{token}')")
        ok, _ = self._ps(script)
        return ok

    def button_down(self, button: str = "left") -> bool:
        ok, _ = self._ps(self.MOUSE_JS.format(
            action=f"JvMouse::Down(${'true' if button == 'right' else 'false'});"))
        return ok

    def button_up(self, button: str = "left") -> bool:
        ok, _ = self._ps(self.MOUSE_JS.format(
            action=f"JvMouse::Up(${'true' if button == 'right' else 'false'});"))
        return ok

    def position(self) -> Tuple[int, int]:
        script = (self.MOUSE_JS + "[Console]::WriteLine("
                  "[System.Windows.Forms.Cursor]::Position.X.ToString() + ',' + "
                  "[System.Windows.Forms.Cursor]::Position.Y.ToString())")
        ok, out = self._ps(script)
        if ok and "," in out:
            try:
                x, y = out.strip().splitlines()[-1].split(",")
                return int(x), int(y)
            except Exception:
                pass
        return 0, 0

    def screen_size(self) -> ScreenInfo:
        script = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "[Console]::WriteLine([System.Windows.Forms.Screen]::PrimaryScreen."
                  "Bounds.Width.ToString() + ',' + [System.Windows.Forms.Screen]::"
                  "PrimaryScreen.Bounds.Height.ToString())")
        ok, out = self._ps(script)
        if ok and "," in out:
            try:
                width, height = out.strip().splitlines()[-1].split(",")
                return ScreenInfo(width=int(width), height=int(height))
            except Exception:
                pass
        return ScreenInfo()


class OsascriptBackend(_SubprocessBackend):
    """macOS: cliclick when installed, otherwise System Events for keys."""

    name = "osascript"

    def available(self) -> Tuple[bool, str]:
        if sys.platform != "darwin":
            return False, "macOS only"
        if shutil.which("cliclick"):
            return True, "cliclick + System Events"
        if shutil.which("osascript"):
            return True, "osascript (keyboard only; brew install cliclick for mouse)"
        return False, "osascript not found"

    def move(self, x: int, y: int, *, duration: float = 0.0) -> bool:
        if not shutil.which("cliclick"):
            return False
        if duration > 0:
            steps = max(1, int(duration * 60))
            ok, _ = self._run(["cliclick", f"m:{int(x)},{int(y)}", "w:"
                               f"{max(20, int(duration * 1000 / steps))}"])
            return ok
        ok, _ = self._run(["cliclick", f"m:{int(x)},{int(y)}"])
        return ok

    def click(self, x: Optional[int] = None, y: Optional[int] = None, *,
              button: str = "left", clicks: int = 1) -> bool:
        if not shutil.which("cliclick"):
            return False
        commands: List[str] = []
        if x is not None and y is not None:
            commands.append(f"m:{int(x)},{int(y)}")
        verb = {"left": "c", "right": "rc", "middle": "c"}.get(button, "c")
        commands.extend([verb] * int(clicks))
        ok, _ = self._run(["cliclick", *commands])
        return ok

    def type_text(self, text: str, *, interval: float = 0.0) -> bool:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        ok, _ = self._run(["osascript", "-e",
                           f'tell application "System Events" to keystroke "{escaped}"'],
                          timeout=30.0)
        return ok

    def press_key(self, key: str) -> bool:
        mapping = {"enter": "return", "escape": "escape", "tab": "tab",
                   "space": "space", "backspace": "delete", "up": "up arrow",
                   "down": "down arrow", "left": "left arrow", "right": "right arrow"}
        parts = [part.strip().lower() for part in key.split("+")]
        keyname = mapping.get(parts[-1], parts[-1])
        mods = [f"{m} down" for m in parts[:-1]]
        script = (f'tell application "System Events" to keystroke "" using '
                  f'{{{", ".join(mods)}}}') if mods else \
                 (f'tell application "System Events" to key code 0')
        # simpler: use key code for named keys, keystroke for modifiers
        if mods:
            script = (f'tell application "System Events" to key code '
                      f'{_MAC_KEYCODES.get(keyname, 0)} using '
                      f'{{{", ".join(mods)}}}')
        else:
            script = (f'tell application "System Events" to key code '
                      f'{_MAC_KEYCODES.get(keyname, 0)}')
        ok, _ = self._run(["osascript", "-e", script])
        return ok

    def scroll(self, amount: int, *, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        if not shutil.which("cliclick"):
            return False
        verb = "su" if amount > 0 else "sd"
        ok, _ = self._run(["cliclick", f"{verb}:{abs(int(amount)) // 100 or 1}"])
        return ok


_MAC_KEYCODES = {"return": 36, "enter": 36, "tab": 48, "space": 49, "escape": 53,
                 "backspace": 51, "delete": 117, "up arrow": 126, "down arrow": 125,
                 "left arrow": 123, "right arrow": 124, "home": 115, "end": 119,
                 "pageup": 116, "pagedown": 121, "f5": 96, "f11": 103, "f12": 111}


class NullBackend(InputBackend):
    """Nothing available — every method fails with an actionable message."""

    name = "null"

    def __init__(self, reason: str = "no input backend available"):
        self.reason = reason

    def available(self) -> Tuple[bool, str]:
        return False, self.reason

    def _fail(self) -> bool:
        log.warning(f"screen control unavailable: {self.reason}")
        return False

    def move(self, x: int, y: int, *, duration: float = 0.0) -> bool:
        return self._fail()

    def click(self, x: Optional[int] = None, y: Optional[int] = None, *,
              button: str = "left", clicks: int = 1) -> bool:
        return self._fail()

    def scroll(self, amount: int, *, x: Optional[int] = None,
               y: Optional[int] = None) -> bool:
        return self._fail()

    def type_text(self, text: str, *, interval: float = 0.0) -> bool:
        return self._fail()

    def press_key(self, key: str) -> bool:
        return self._fail()

    def position(self) -> Tuple[int, int]:
        return 0, 0

    def screen_size(self) -> ScreenInfo:
        return ScreenInfo()


_BACKENDS: List[type[InputBackend]] = [
    PyAutoGUIBackend, XdotoolBackend, YdotoolBackend, PowerShellBackend,
    OsascriptBackend,
]

_ACTIVE: Optional[InputBackend] = None


def list_backends() -> List[Tuple[str, bool, str]]:
    rows: List[Tuple[str, bool, str]] = []
    for backend_cls in _BACKENDS:
        backend = backend_cls()
        try:
            ok, detail = backend.available()
        except Exception as exc:
            ok, detail = False, str(exc)
        rows.append((backend.name, ok, detail))
    return rows


def choose_backend(preferred: str = "auto", *, failsafe: bool = True) -> InputBackend:
    """Pick the first working backend (or the named one)."""
    global _ACTIVE
    preferred = (preferred or "auto").lower()

    if preferred not in ("auto", ""):
        for backend_cls in _BACKENDS:
            if backend_cls.name == preferred:
                backend = backend_cls(failsafe=failsafe) \
                    if backend_cls is PyAutoGUIBackend else backend_cls()
                ok, detail = backend.available()
                if ok:
                    log.info(f"screen backend: {backend.name} ({detail})")
                    _ACTIVE = backend
                    return backend
                log.warning(f"requested backend '{preferred}' unavailable: {detail}")
        log.warning(f"falling back to auto-selection (wanted '{preferred}')")

    for backend_cls in _BACKENDS:
        backend = backend_cls(failsafe=failsafe) \
            if backend_cls is PyAutoGUIBackend else backend_cls()
        try:
            ok, detail = backend.available()
        except Exception:
            continue
        if ok:
            log.info(f"screen backend: {backend.name} ({detail})")
            _ACTIVE = backend
            return backend

    platform_info = probe_platform()
    reason = ("no mouse/keyboard backend found. Install one: pyautogui (pip), "
              "xdotool/ydotool (Linux), or run on a desktop session")
    if not platform_info.has_display and platform_info.os_family == "linux":
        reason = ("headless environment (no DISPLAY). Start a virtual display: "
                  "`Xvfb :99 -screen 0 1440x900x24 & export DISPLAY=:99`, then "
                  "`apt-get install xdotool` for input")
    log.warning(reason)
    _ACTIVE = NullBackend(reason)
    return _ACTIVE


def get_backend(config: Optional[Dict[str, Any]] = None, *,
                refresh: bool = False) -> InputBackend:
    global _ACTIVE
    if _ACTIVE is None or refresh:
        screen_cfg = (config or {}).get("screen") or {}
        _ACTIVE = choose_backend(str(screen_cfg.get("backend", "auto")),
                                 failsafe=bool(screen_cfg.get("failsafe", True)))
    return _ACTIVE


def active_backend_name() -> str:
    return _ACTIVE.name if _ACTIVE else "(not selected)"
