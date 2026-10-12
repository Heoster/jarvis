"""Ollama runtime: where the Ollama server is, and starting it when it is not running.

Everything that talks to Ollama (the chat provider, the memory embedder, the
health probe) resolves its URL here, so there is one answer to "where is
Ollama?".

URL resolution, first match wins::

    OLLAMA_BASE_URL  env   (e.g. http://127.0.0.1:11434)
    OLLAMA_HOST      env   (Ollama's own variable: 127.0.0.1:11434, 0.0.0.0:11434, ...)
    llm.ollama.base_url    config.yaml
    llm.base_url           config.yaml (legacy location)
    http://127.0.0.1:11434 default

Auto-start, controlled by ``llm.ollama.autostart`` (default ``true``) or the
``JARVIS_OLLAMA_AUTOSTART`` env var (``0``/``1``, wins over config):

* if the server answers ``GET /api/tags`` it is used as-is;
* otherwise, *and only when the URL points at this machine*, ``ollama serve`` is
  started in the background with ``OLLAMA_HOST`` set to the configured
  host:port, so it binds exactly where JARVIS will look;
* we wait up to ``llm.ollama.autostart_timeout`` seconds for it to answer;
* a remote URL is never started (we cannot), and a missing ``ollama`` binary is
  reported with the install link instead of being retried in a loop.

The server is left running after JARVIS exits, the same as the Ollama app does.
Its output goes to ``logs/ollama-serve.log``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from ..utils.logger import get_logger

log = get_logger("ollama")

__all__ = [
    "DEFAULT_URL", "normalise_url", "resolve_base_url", "autostart_enabled",
    "autostart_timeout", "is_local", "is_served", "ensure_running",
    "start_in_background",
]

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_PORT = 11434
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
SPAWN_COOLDOWN_SECONDS = 60.0

_LOCK = threading.Lock()
_PROCESS: Optional[subprocess.Popen] = None
_LAST_FAILED_AT = 0.0
_LAST_FAILURE = ""


# ── URL handling ────────────────────────────────────────────────────────────
def normalise_url(raw: str) -> str:
    """``0.0.0.0:11434`` → ``http://127.0.0.1:11434``; adds scheme and default port.

    ``0.0.0.0`` is a bind address, not something a client can connect to, so it
    is mapped to loopback.  A path prefix (reverse proxy) is kept.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = parts.hostname or "127.0.0.1"
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = parts.port
    if port is None and host in LOCAL_HOSTS:
        port = DEFAULT_PORT
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme or "http", netloc, parts.path.rstrip("/"),
                       "", "")).rstrip("/")


def resolve_base_url(config: Optional[Mapping[str, Any]] = None,
                     env: Optional[Mapping[str, str]] = None) -> str:
    """The Ollama base URL JARVIS should use (see the module docstring)."""
    environ = os.environ if env is None else env
    llm = (config or {}).get("llm") if isinstance(config, Mapping) else None
    llm = llm if isinstance(llm, Mapping) else {}
    node = llm.get("ollama")
    node = node if isinstance(node, Mapping) else {}
    candidates = (
        environ.get("OLLAMA_BASE_URL"),
        environ.get("OLLAMA_HOST"),
        node.get("base_url"),
        llm.get("base_url"),
    )
    for raw in candidates:
        if raw and str(raw).strip():
            return normalise_url(str(raw))
    return DEFAULT_URL


def _flag(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def autostart_enabled(config: Optional[Mapping[str, Any]] = None,
                      env: Optional[Mapping[str, str]] = None) -> bool:
    environ = os.environ if env is None else env
    if environ.get("JARVIS_OLLAMA_AUTOSTART") not in (None, ""):
        return _flag(environ.get("JARVIS_OLLAMA_AUTOSTART"), True)
    node = _ollama_node(config)
    return _flag(node.get("autostart"), True)


def autostart_timeout(config: Optional[Mapping[str, Any]] = None) -> float:
    try:
        return float(_ollama_node(config).get("autostart_timeout", 20) or 20)
    except (TypeError, ValueError):
        return 20.0


def _ollama_node(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    llm = (config or {}).get("llm") if isinstance(config, Mapping) else None
    if not isinstance(llm, Mapping):
        return {}
    node = llm.get("ollama")
    return node if isinstance(node, Mapping) else {}


def is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOCAL_HOSTS


# ── liveness ────────────────────────────────────────────────────────────────
def is_served(url: str, timeout: float = 1.5) -> bool:
    """True when an Ollama server answers at ``url``."""
    import httpx

    try:
        with httpx.Client(timeout=timeout) as client:
            return client.get(f"{url}/api/tags").status_code == 200
    except httpx.HTTPError:
        return False


def _log_path() -> Path:
    base = Path(os.environ.get("JARVIS_LOG_DIR") or "logs")
    base.mkdir(parents=True, exist_ok=True)
    return base / "ollama-serve.log"


def _log_tail(path: Path, lines: int = 6) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " | ".join(line.strip() for line in text[-lines:] if line.strip())


def _spawn(binary: str, url: str) -> subprocess.Popen:
    from urllib.parse import urlsplit as _split

    parts = _split(url)
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"{parts.hostname}:{parts.port or DEFAULT_PORT}"
    kwargs: dict = {}
    if os.name == "nt":  # no console window on Windows
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(_log_path(), "ab") as handle:  # the child inherits its own descriptor
        return subprocess.Popen([binary, "serve"], stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT, env=env, **kwargs)


def ensure_running(url: str, *, autostart: bool = True,
                   timeout: float = 20.0) -> Tuple[bool, str]:
    """Make sure an Ollama server answers at ``url``, starting it if allowed.

    Returns ``(ok, detail)``.  Thread-safe: concurrent callers wait for the one
    start attempt instead of spawning several servers.
    """
    global _PROCESS, _LAST_FAILED_AT, _LAST_FAILURE

    if is_served(url):
        return True, "running"
    if not autostart:
        return False, f"not running at {url} (autostart is off)"
    if not is_local(url):
        return False, f"not running at {url} (remote host: start Ollama there)"

    with _LOCK:
        if is_served(url):  # another thread started it while we waited
            return True, "running"
        binary = shutil.which("ollama")
        if not binary:
            return False, ("not running and the `ollama` program is not installed "
                           "(get it from https://ollama.com)")
        still_starting = _PROCESS is not None and _PROCESS.poll() is None
        if not still_starting:
            if time.time() - _LAST_FAILED_AT < SPAWN_COOLDOWN_SECONDS:
                return False, f"ollama serve failed recently: {_LAST_FAILURE}"
            try:
                _PROCESS = _spawn(binary, url)
            except OSError as exc:
                _LAST_FAILED_AT, _LAST_FAILURE = time.time(), str(exc)
                return False, f"could not start `ollama serve`: {exc}"
            log.info(f"started `ollama serve` (pid {_PROCESS.pid}) for {url}")

        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_served(url, timeout=1.0):
                return True, f"started `ollama serve` (pid {_PROCESS.pid})"
            if _PROCESS.poll() is not None:
                break
            time.sleep(0.5)

        tail = _log_tail(_log_path())
        reason = (f"exited with code {_PROCESS.returncode}" if _PROCESS.poll() is not None
                  else f"no answer after {timeout:.0f}s")
        _LAST_FAILED_AT = time.time()
        _LAST_FAILURE = f"{reason}" + (f" — {tail}" if tail else "")
        log.warning(f"`ollama serve` did not come up: {_LAST_FAILURE}")
        return False, f"could not start `ollama serve` at {url}: {_LAST_FAILURE}"


def start_in_background(config: Optional[Mapping[str, Any]] = None) -> Optional[threading.Thread]:
    """Start Ollama without blocking JARVIS startup (used by the orchestrator)."""
    url = resolve_base_url(config)
    if not autostart_enabled(config) or not is_local(url) or is_served(url, 0.5):
        return None

    def worker() -> None:
        ok, detail = ensure_running(url, autostart=True,
                                    timeout=autostart_timeout(config))
        (log.info if ok else log.warning)(f"ollama: {detail}")

    thread = threading.Thread(target=worker, name="jarvis-ollama-start", daemon=True)
    thread.start()
    return thread
