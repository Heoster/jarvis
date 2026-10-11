"""Shared fixtures: an isolated, offline JARVIS home so tests never touch real data.

The trick is ``JARVIS_CONFIG``: the config loader already honours it, so a
generated YAML file redirects *every* runtime path (memory store, sessions,
quota database, screenshots, trash) into a temp directory — even for tools that
call ``load_config()`` internally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# A stray .env or exported key must never let a test hit the network.
for variable in ("GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
                 "GEMINI_API_KEY", "GOOGLE_API_KEY", "HUGGINGFACE_API_KEY",
                 "HF_TOKEN", "TAVILY_API_KEY", "SERPER_API_KEY",
                 "FIRECRAWL_API_KEY", "JINA_API_KEY", "SEARXNG_BASE_URL",
                 "OBSIDIAN_VAULT"):
    os.environ[variable] = ""

_TEST_CONFIG = """\
jarvis:
  operator: Heoster
llm:
  provider: stub
  fallback_chain: ["stub"]
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
