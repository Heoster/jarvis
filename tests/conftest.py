"""Shared fixtures: an isolated, offline JARVIS home so tests never touch real data.

The trick is ``JARVIS_CONFIG``: the config loader already honours it, so a
generated YAML file redirects *every* runtime path (memory store, sessions,
quota database, screenshots, trash) into a temp directory — even for tools that
call ``load_config()`` internally.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# A stray .env or exported key must never let a test hit the network.
for variable in ("GROQ_API_KEY", "OPENROUTER_API_KEY",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY", "HUGGINGFACE_API_KEY",
                 "HF_TOKEN", "TAVILY_API_KEY", "SERPER_API_KEY",
                 "FIRECRAWL_API_KEY", "JINA_API_KEY", "SEARXNG_BASE_URL",
                 "OBSIDIAN_VAULT"):
    os.environ[variable] = ""


# ── the suite is hermetic: no outbound network, no real browser ─────────────
#
# These two guards exist because the suite used to pass only on machines that
# happened to be offline and to have no browser installed. On a GitHub runner
# — which has internet *and* a preinstalled Chrome — six tests flipped:
# search_web succeeded via DuckDuckGo, read_url succeeded via the jina reader,
# install_prebuilt downloaded the real Dataview release instead of the fake
# vendored bundle, and browser_launch started a real Chrome on :9222.
#
# Both guards are opt-out with JARVIS_TEST_ALLOW_NETWORK=1 so a developer can
# still run the suite against the real thing when they mean to.
_ALLOW_NETWORK = os.environ.get("JARVIS_TEST_ALLOW_NETWORK", "").lower() in ("1", "true", "yes")


def _is_loopback(host: object) -> bool:
    """True for anything that stays inside this machine (servers included)."""
    if host in (None, "", "localhost"):
        return True                     # "" is what a server binds to
    text = str(host).split("%")[0].strip("[]")
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False                    # a real hostname


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    """Fail any DNS lookup that would leave the machine.

    ``getaddrinfo`` is the single choke point every HTTP client in the project
    goes through (httpx, urllib, selenium, the CDP websocket), so guarding it
    makes the whole suite offline-deterministic. Loopback stays open because
    some tests deliberately serve a local HTTP server and read it back.
    """
    if _ALLOW_NETWORK:
        return

    real_getaddrinfo = socket.getaddrinfo

    def guarded(host, port, family=0, type=0, proto=0, flags=0):
        if not _is_loopback(host):
            raise socket.gaierror(
                -2, f"JARVIS tests are offline: refusing to resolve {host!r} "
                    f"(port {port}). Set JARVIS_TEST_ALLOW_NETWORK=1 to allow it.")
        return real_getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Never spawn the host machine's Chrome/Firefox during a test.

    Runner images ship Chrome, so without this the "no browser is installed"
    tests launched a real one and then failed on their own assertion.
    """
    if _ALLOW_NETWORK:
        return

    from jarvis.tools.browser import terminal_launcher

    def no_binary(browser_type: str = "chromium", binary_path: str = ""):
        return None

    monkeypatch.setattr(terminal_launcher, "_resolve_binary", no_binary)

_TEST_CONFIG = """\
jarvis:
  operator: Heoster
llm:
  provider: stub
  fallback_chain: ["stub"]
  # A dead port on loopback: the ollama probe fails instantly instead of
  # reaching a daemon that happens to be installed on the machine (CI runners
  # ship one), so health checks are identical everywhere.
  base_url: "http://127.0.0.1:9"
search:
  provider_order: []
crawl:
  provider_order: []
memory:
  long_term:
    backend: sqlite
    embedding_provider: local
    persist_directory: "{home}/memory_store"
  knowledge_base:
    directory: "{home}/knowledge"
    auto_ingest: false
  skills:
    directory: "{home}/skills"
sessions:
  database: "{home}/sessions/jarvis.db"
limits:
  database: "{home}/data/quota.db"
screen:
  screenshot_dir: "{home}/data/screenshots"
browser:
  screenshot_dir: "{home}/data/screenshots"
obsidian:
  vault_path: ""
  srs:
    database: "{home}/data/obsidian_srs.json"
paths:
  trash_dir: "{home}/data/trash"
agent:
  turn_timeout: 60
"""


@pytest.fixture()
def home(tmp_path, monkeypatch) -> Path:
    """Isolate every runtime path for a single test."""
    for directory in ("data/screenshots", "sessions", "memory_store", "knowledge",
                      "skills", "logs", "data/trash"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_TEST_CONFIG.format(home=tmp_path))
    monkeypatch.setenv("JARVIS_CONFIG", str(config_path))
    monkeypatch.setenv("JARVIS_TRASH_DIR", str(tmp_path / "data" / "trash"))
    return tmp_path


@pytest.fixture()
def config(home):
    """A deterministic, offline configuration built from the isolated file."""
    from jarvis.config import load_config

    cfg = load_config(refresh=True)
    cfg["agent"] = dict(cfg.get("agent") or {})
    cfg["agent"]["turn_timeout"] = 60
    cfg["agent"]["max_iterations"] = 4
    _reset_singletons()
    return cfg


def _reset_singletons() -> None:
    """Fresh process-wide singletons so tests cannot leak state into each other."""
    from jarvis.core.api_manager import reset_api_manager
    from jarvis.memory.memory_manager import reset_memory_manager
    from jarvis.utils.quota import reset_quota_manager

    reset_api_manager()
    reset_memory_manager(drop_index=True)   # tests own their memory index
    reset_quota_manager()


@pytest.fixture()
def registry(config):
    from jarvis.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.load_all_default_tools(config)
    return reg
