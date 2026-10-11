"""Environment probing: what OS are we on, is Chrome installed, is there a screen?

JARVIS is designed to *degrade instead of crash*.  Everything that depends on a
GUI, a browser binary, or an external tool asks this module first, so a headless
server produces a clear "why" instead of a traceback.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

__all__ = [
    "PlatformInfo", "probe_platform", "which", "command_exists",
    "find_chrome_binary", "find_firefox_binary", "has_display",
    "running_in_container", "python_packages", "optional_deps_report",
]

_CHROME_CANDIDATES = {
    "linux": [
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
        "chrome", "brave-browser", "microsoft-edge",
        "/usr/bin/google-chrome", "/usr/bin/chromium",
        "/opt/google/chrome/chrome", "/snap/bin/chromium",
    ],
    "darwin": [
        "google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ],
    "windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "chrome.exe", "msedge.exe",
    ],
}

_FIREFOX_CANDIDATES = {
    "linux": ["firefox", "/usr/bin/firefox", "/snap/bin/firefox"],
    "darwin": ["/Applications/Firefox.app/Contents/MacOS/firefox", "firefox"],
    "windows": [r"C:\Program Files\Mozilla Firefox\firefox.exe", "firefox.exe"],
}

_SCREEN_TOOLS = ("xdotool", "ydotool", "wmctrl", "xwininfo", "scrot", "tesseract",
                 "grim", "slurp", "osascript")


@lru_cache(maxsize=1)
def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def command_exists(cmd: str) -> bool:
    return which(cmd) is not None


def _run(cmd: List[str], timeout: float = 3.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or out.stderr or "").strip()
    except Exception:
        return ""


@dataclass
class PlatformInfo:
    os_name: str
    os_family: str                       # linux | darwin | windows
    os_version: str
    arch: str
    python_version: str
    kernel: str = ""
    desktop: str = ""
    session_type: str = ""               # x11 | wayland | none
    display: str = ""
    is_container: bool = False
    is_wsl: bool = False
    cpu_count: int = 1
    total_memory_gb: float = 0.0
    chrome_path: str = ""
    chrome_version: str = ""
    firefox_path: str = ""
    screen_tools: Dict[str, str] = field(default_factory=dict)
    has_display: bool = False
    can_screenshot: bool = False
    can_control_mouse: bool = False
    can_control_browser: bool = False
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return dict(self.__dict__)

    def summary(self) -> str:
        lines = [
            f"OS          : {self.os_name} {self.os_version} ({self.arch})",
            f"Session     : {self.desktop or 'unknown'} / "
            f"{self.session_type or 'headless'}  display={self.display or '-'}",
            f"Container   : {'yes' if self.is_container else 'no'}"
            f"{' (WSL)' if self.is_wsl else ''}",
            f"CPU / RAM   : {self.cpu_count} cores / {self.total_memory_gb:.1f} GB",
            f"Chrome      : {self.chrome_path or 'not found'}"
            + (f" ({self.chrome_version})" if self.chrome_version else ""),
            f"Firefox     : {self.firefox_path or 'not found'}",
            f"Screen ctrl : screenshot={'yes' if self.can_screenshot else 'no'}, "
            f"mouse/keyboard={'yes' if self.can_control_mouse else 'no'}",
            f"Browser     : {'ready' if self.can_control_browser else 'needs Chrome'}",
        ]
        for name, path in sorted(self.screen_tools.items()):
            lines.append(f"  tool {name:<10}: {path}")
        if self.notes:
            lines.append("Notes       : " + " | ".join(self.notes))
        return "\n".join(lines)


def running_in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(errors="replace")
        if any(tag in cgroup for tag in ("docker", "kubepods", "containerd", "lxc")):
            return True
    except OSError:
        pass
    return bool(os.environ.get("container"))


def has_display() -> bool:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def find_chrome_binary() -> Optional[str]:
    """Locate a Chromium-family browser executable."""
    env = os.environ.get("JARVIS_CHROME_PATH") or os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    family = _os_family()
    for candidate in _CHROME_CANDIDATES.get(family, []):
        if candidate.startswith("/") or ":" in candidate:
            if Path(candidate).exists():
                return candidate
            continue
        found = which(candidate)
        if found:
            return found
    return None


def find_firefox_binary() -> Optional[str]:
    env = os.environ.get("JARVIS_FIREFOX_PATH")
    if env and Path(env).exists():
        return env
    for candidate in _FIREFOX_CANDIDATES.get(_os_family(), []):
        if candidate.startswith("/"):
            if Path(candidate).exists():
                return candidate
            continue
        found = which(candidate)
        if found:
            return found
    return None


def _os_family() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _memory_gb() -> float:
    try:
        import psutil  # type: ignore

        return round(psutil.virtual_memory().total / 1e9, 2)
    except Exception:
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                kb = int(line.split()[1])
                return round(kb / 1e6, 2)
    except Exception:
        pass
    return 0.0


def _chrome_version(path: str) -> str:
    for flag in ("--version", "-version"):
        try:
            out = subprocess.run([path, flag], capture_output=True, text=True, timeout=5)
            text = (out.stdout or out.stderr).strip()
            if text:
                return text.splitlines()[0][:80]
        except Exception:
            continue
    return ""


@lru_cache(maxsize=1)
def probe_platform(*, refresh: bool = False) -> PlatformInfo:
    """One-shot environment probe (cached for the process lifetime)."""
    family = _os_family()
    display = os.environ.get("DISPLAY", "")
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    session_type = "x11" if display else ("wayland" if wayland else "none")

    chrome = find_chrome_binary() or ""
    firefox = find_firefox_binary() or ""
    tools = {name: (which(name) or "") for name in _SCREEN_TOOLS}

    can_screenshot = False
    if family == "darwin":
        can_screenshot = True
    elif family == "windows":
        can_screenshot = True
    elif display:
        can_screenshot = bool(tools.get("scrot") or tools.get("grim")
                              or _has_python_package("mss") or _has_python_package("PIL"))
    elif wayland:
        can_screenshot = bool(tools.get("grim") or _has_python_package("mss"))

    can_control_mouse = False
    if family in ("darwin", "windows"):
        can_control_mouse = _has_python_package("pyautogui")
    else:
        can_control_mouse = bool(tools.get("xdotool") or tools.get("ydotool")
                                 or (_has_python_package("pyautogui") and bool(display)))

    info = PlatformInfo(
        os_name=f"{platform.system()} {platform.release()}",
        os_family=family,
        os_version=platform.version()[:80],
        arch=platform.machine(),
        python_version=platform.python_version(),
        kernel=platform.release(),
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", "")
        or os.environ.get("DESKTOP_SESSION", ""),
        session_type=session_type,
        display=display or wayland,
        is_container=running_in_container(),
        is_wsl="microsoft" in platform.release().lower(),
        cpu_count=os.cpu_count() or 1,
        total_memory_gb=_memory_gb(),
        chrome_path=chrome,
        chrome_version=_chrome_version(chrome) if chrome else "",
        firefox_path=firefox,
        screen_tools=tools,
        has_display=has_display(),
        can_screenshot=can_screenshot,
        can_control_mouse=can_control_mouse,
        can_control_browser=bool(chrome or firefox) or _has_python_package("playwright"),
    )

    notes = info.notes
    if info.is_container and not info.has_display:
        notes.append("headless container: browser runs in --headless mode, "
                     "screen control needs Xvfb (apt-get install xvfb)")
    if family == "linux" and not display and not wayland:
        notes.append("no DISPLAY/WAYLAND_DISPLAY: screen tools will report "
                     "'no display' instead of failing silently")
    if not chrome and not firefox:
        notes.append("no Chromium/Firefox found: install one or run "
                     "`playwright install chromium`")
    if info.total_memory_gb and info.total_memory_gb < 6:
        notes.append(f"only {info.total_memory_gb:.1f} GB RAM: stick to <=7B "
                     f"quantised models, keep context <= 8K")
    return info


def _has_python_package(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def python_packages() -> Dict[str, bool]:
    """Which optional extras are importable right now."""
    names = [
        "playwright", "selenium", "pyautogui", "PIL", "mss", "pytesseract",
        "chromadb", "trafilatura", "httpx", "yaml", "rich", "click", "psutil",
        "websockets", "numpy", "langchain_core", "ollama", "tiktoken",
        "tavily", "firecrawl", "bs4", "lxml",
    ]
    return {name: _has_python_package(name) for name in names}


def optional_deps_report() -> str:
    """Human-friendly capability matrix used by ``jarvis doctor``."""
    pkgs = python_packages()
    groups = {
        "browser": [("playwright", "Playwright engine (recommended)"),
                    ("selenium", "Selenium engine"),
                    ("websockets", "full CDP command set")],
        "screen": [("pyautogui", "Python-level mouse/keyboard"),
                   ("mss", "fast screenshots"),
                   ("PIL", "image handling"),
                   ("pytesseract", "OCR (reads screen text, no vision model)")],
        "memory": [("chromadb", "optional vector backend"),
                   ("numpy", "faster local vector math"),
                   ("tiktoken", "exact token counts")],
        "web": [("trafilatura", "best-in-class HTML→text"),
                ("bs4", "DOM parsing helper"),
                ("yaml", "config + DOM parsing")],
    }
    lines: List[str] = []
    for group, items in groups.items():
        lines.append(f"[{group}]")
        for module, what in items:
            mark = "✓" if pkgs.get(module) else "·"
            lines.append(f"  {mark} {module:<14} {what}")
    return "\n".join(lines)
