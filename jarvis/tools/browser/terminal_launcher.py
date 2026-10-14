"""Launch and drive a real Chrome process from the terminal.

This is deliberately OS-level rather than framework-level: JARVIS spawns the
browser binary itself (``subprocess``) with a debugging port, exactly as you
would from a shell.  That gives three things at once:

1. a **real, visible window** (no headless-only automation);
2. a **CDP endpoint** (``127.0.0.1:9222``) that any engine can attach to —
   Playwright, Selenium, or raw CDP;
3. a process JARVIS can track, restart, and drive with the cursor/keyboard when
   the DOM route is unavailable (native dialogs, canvas apps, extensions).

It also provides :func:`run_in_terminal` for the screen agent, so "open a
terminal and run this" is a first-class capability instead of a hack.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from ...utils.platform_probe import find_chrome_binary, find_firefox_binary, probe_platform

log = get_logger("browser.launch")

__all__ = ["ChromeProcess", "launch_chrome", "wait_for_port", "port_free",
           "run_in_terminal", "pick_free_port", "port_in_use_by_chrome", "STEALTH_ARGS"]


# Flags that make a scripted Chrome behave like a normal one and stop the
# "your browser is being controlled" style detections.
STEALTH_ARGS: List[str] = [
    "--no-first-run",
    "--no-default-browser-check",
    # NOTE: "--disable-blink-features=AutomationControlled" is NOT a supported
    # Chrome flag (Chrome shows an "unsupported command-line flag" warning), so
    # it is intentionally not passed.
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-popup-blocking",
    "--disable-notifications",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--metrics-recording-only",
    "--password-store=basic",
    "--use-mock-keychain",
]

_HEADLESS_ARGS = ["--headless=new", "--disable-gpu", "--window-size=1440,900"]


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) != 0


def pick_free_port(start: int = 9222, attempts: int = 40) -> int:
    for port in range(start, start + attempts):
        if port_free(port):
            return port
    return start


def port_in_use_by_chrome(port: int, host: str = "127.0.0.1") -> bool:
    """True if a Chrome DevTools endpoint is already answering on ``port``."""
    if port_free(port, host):
        return False
    import httpx

    try:
        with httpx.Client(timeout=2.0) as client:
            return client.get(f"http://{host}:{port}/json/version").status_code == 200
    except Exception:
        return False


def wait_for_port(port: int, *, timeout: float = 15.0, host: str = "127.0.0.1") -> bool:
    """Block until something listens on ``port`` (or the timeout expires)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.15)
    return False


@dataclass
class ChromeProcess:
    """A Chrome instance JARVIS owns."""

    pid: int
    port: int
    binary: str
    user_data_dir: str = ""
    headless: bool = False
    url: str = ""
    started_at: float = field(default_factory=time.time)
    process: Optional[subprocess.Popen] = None
    log_path: str = ""

    @property
    def cdp_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def is_running(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def open_tab(self, url: str, *, timeout: float = 6.0) -> bool:
        """Ask Chrome for a new tab over the plain-HTTP CDP endpoint."""
        import httpx

        url = url if "://" in url else f"https://{url}"
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.put(f"{self.cdp_base}/json/new?{url}")
                if resp.status_code >= 400:
                    resp = client.get(f"{self.cdp_base}/json/new?{url}")
            return resp.status_code < 400
        except Exception as exc:
            log.debug(f"open_tab failed: {exc}")
            return False

    def tabs(self, *, timeout: float = 4.0) -> List[Dict[str, Any]]:
        import httpx

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(f"{self.cdp_base}/json/list")
            return [t for t in resp.json() if t.get("type") == "page"]
        except Exception:
            return []

    def kill(self, *, timeout: float = 5.0) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=timeout)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            return
        try:
            os.kill(self.pid, 15)
        except OSError:
            pass

    def status(self) -> Dict[str, Any]:
        return {"pid": self.pid, "port": self.port, "running": self.is_running(),
                "binary": self.binary, "headless": self.headless,
                "url": self.url, "uptime_s": round(time.time() - self.started_at, 1),
                "tabs": len(self.tabs()) if self.is_running() else 0,
                "cdp": self.cdp_base}


def _resolve_binary(browser_type: str = "chromium", binary_path: str = "") -> Optional[str]:
    if binary_path and Path(binary_path).exists():
        return binary_path
    env = os.environ.get("JARVIS_CHROME_PATH") or os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    if browser_type in ("firefox",):
        return find_firefox_binary()
    found = find_chrome_binary()
    if found:
        return found
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome",
                 "brave-browser", "microsoft-edge"):
        path = shutil.which(name)
        if path:
            return path
    return None


def launch_chrome(url: str = "", *, browser_type: str = "chromium",
                  headless: bool = False, port: Optional[int] = None,
                  binary_path: str = "", user_data_dir: str = "",
                  window_size: Tuple[int, int] = (1440, 900),
                  window_position: Tuple[int, int] = (0, 0),
                  stealth: bool = True, extra_args: Optional[Sequence[str]] = None,
                  extra_env: Optional[Mapping[str, str]] = None,
                  capture_log: bool = True) -> ChromeProcess:
    """Spawn Chrome from the terminal and wait until CDP answers.

    Raises ``RuntimeError`` with an actionable message when no browser binary is
    available (the caller — the harness — then moves to the next engine).
    """
    binary = _resolve_binary(browser_type, binary_path)
    if not binary:
        raise RuntimeError(
            "no Chromium/Chrome/Firefox binary found. Install one, or run "
            "`playwright install chromium`, or set JARVIS_CHROME_PATH.")

    port = port or pick_free_port(9222)

    # Reuse a Chrome that already answers on this CDP port instead of spawning
    # another one (a second launch would just hand off to the existing window
    # and leave us with duplicate processes/tabs).
    if port_in_use_by_chrome(port):
        log.info(f"chrome already running on port {port}; reusing it")
        instance = ChromeProcess(pid=0, port=port, binary=binary,
                                 user_data_dir=user_data_dir, headless=headless,
                                 url=url, process=None, log_path="")
        if url:
            instance.open_tab(url)
        return instance

    if user_data_dir:
        profile_dir = str(Path(user_data_dir).expanduser())
    else:
        # a per-run temp profile keeps the operator's real profile untouched
        profile_dir = tempfile.mkdtemp(prefix="jarvis-chrome-")

    args: List[str] = [binary]
    if not headless and probe_platform().has_display:
        args += [f"--window-size={window_size[0]},{window_size[1]}",
                 f"--window-position={window_position[0]},{window_position[1]}"]
    if headless or not probe_platform().has_display:
        args += _HEADLESS_ARGS
    if stealth:
        args += STEALTH_ARGS
    args += [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--remote-allow-origins=*",
    ]
    args += list(extra_args or [])
    if url:
        args.append(url if "://" in url else f"https://{url}")
    else:
        args.append("about:blank")

    env = dict(os.environ)
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()})

    log.info(f"launching {Path(binary).name} (port {port}, "
             f"{'headless' if headless or not probe_platform().has_display else 'visible'})")

    stdout_target: Any = subprocess.DEVNULL
    log_path = ""
    if capture_log:
        log_path = str(Path(tempfile.gettempdir()) / f"jarvis-chrome-{port}.log")
        stdout_target = open(log_path, "ab", buffering=0)

    creation = {}
    if sys.platform.startswith("win"):
        creation["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_GROUP
    else:
        creation["start_new_session"] = True

    try:
        process = subprocess.Popen(args, stdout=stdout_target,
                                   stderr=subprocess.STDOUT, env=env,
                                   close_fds=True, **creation)
    except Exception as exc:
        raise RuntimeError(f"failed to start '{binary}': {exc}") from exc

    instance = ChromeProcess(pid=process.pid, port=port, binary=binary,
                             user_data_dir=profile_dir, headless=headless
                             or not probe_platform().has_display,
                             url=url, process=process, log_path=log_path)

    if not wait_for_port(port, timeout=20.0):
        tail = ""
        if log_path and Path(log_path).exists():
            tail = Path(log_path).read_text(errors="replace")[-400:]
        instance.kill()
        raise RuntimeError(f"Chrome started (pid {process.pid}) but CDP port {port} "
                           f"never opened. Log tail: {tail}")

    log.info(f"chrome ready: pid={process.pid} port={port}")
    return instance


def run_in_terminal(command: str, *, wait: bool = False, timeout: float = 30.0,
                    cwd: Optional[str] = None, visible: bool = False) -> Dict[str, Any]:
    """Run a shell command — optionally in a *real* visible terminal window.

    ``visible=True`` opens the platform's terminal emulator so the operator can
    watch (used by the screen agent when a task needs an interactive shell).
    """
    if not visible:
        try:
            completed = subprocess.run(command, shell=True, cwd=cwd,
                                       capture_output=True, text=True,
                                       timeout=timeout)
            return {"ok": completed.returncode == 0, "output": completed.stdout[-6000:],
                    "error": completed.stderr[-2000:], "code": completed.returncode,
                    "visible": False}
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "", "error": f"timed out after {timeout}s",
                    "code": -1, "visible": False}
        except Exception as exc:
            return {"ok": False, "output": "", "error": str(exc), "code": -1,
                    "visible": False}

    launched = _spawn_terminal(command, cwd=cwd)
    if not launched:
        return {"ok": False, "output": "", "visible": True,
                "error": "no terminal emulator found; fell back to no-op. "
                         "Set JARVIS_TERMINAL to your emulator."}
    if not wait:
        return {"ok": True, "output": f"launched in terminal: {command}",
                "visible": True, "code": 0}
    time.sleep(min(timeout, 1.0))
    return {"ok": True, "output": f"launched in terminal (waited {timeout}s)",
            "visible": True, "code": 0}


def _spawn_terminal(command: str, *, cwd: Optional[str] = None) -> bool:
    cwd = cwd or os.path.expanduser("~")
    candidates: List[List[str]] = []
    if sys.platform == "darwin":
        candidates.append(["osascript", "-e",
                           f'tell app "Terminal" to do script "{command}"'])
    elif sys.platform.startswith("win"):
        candidates.append(["cmd", "/c", "start", "cmd", "/k", command])
    else:
        emulator = os.environ.get("JARVIS_TERMINAL")
        if emulator:
            candidates.append([emulator, "-e", f"bash -lc '{command}'"])
        for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                           ("konsole", "-e"), ("xfce4-terminal", "-e"),
                           ("alacritty", "-e"), ("kitty", "-e"), ("xterm", "-e")):
            if shutil.which(term):
                candidates.append([term, flag, "bash", "-lc",
                                   f"{command}; exec bash"])
                break
    for argv in candidates:
        try:
            subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False
