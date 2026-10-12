"""JARVIS configuration loader.

Layers, in increasing priority:
    1. Built-in defaults (``config.yaml`` shipped with the repo)
    2. The user's own ``config.yaml`` / ``config.local.yaml`` if present
    3. Environment variables, using ``JARVIS__SECTION__KEY`` (double underscore)
    4. A ``.env`` file in the project root (simple KEY=VALUE parsing)

The loader is deliberately dependency-light: PyYAML if available, otherwise a
tiny built-in parser that handles the flat subset of YAML used by JARVIS.  This
keeps the core importable on a bare Python install (important for the
CPU-first / zero-cost goal).
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

__all__ = [
    "PROJECT_ROOT", "load_config", "get_config", "reload_config",
    "get_env", "dotenv_values", "CONFIG_ENV_PREFIX",
    "parse_yaml_text", "dump_yaml",
]

CONFIG_ENV_PREFIX = "JARVIS__"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
_LOCAL_CONFIG_NAMES = ("config.local.yaml", "config.local.yml")

_CACHE: Optional[Dict[str, Any]] = None
_CACHE_SOURCE: Optional[str] = None

# ── Defaults ────────────────────────────────────────────────────────────────
# Kept in sync with config.yaml; used when the file is missing entirely so
# JARVIS still boots (e.g. installed as a package without the repo tree).
DEFAULTS: Dict[str, Any] = {
    "jarvis": {"version": "1.0.0", "name": "JARVIS", "operator": "Heoster",
               "company": "CODEEX-AI", "cost_policy": "free"},
    "llm": {
        "provider": "auto", "model": "hermes3:3b",
        "base_url": "http://localhost:11434", "temperature": 0.3,
        "max_tokens": 2048, "context_window": 16384, "request_timeout": 120,
        "fallback_chain": ["ollama", "groq", "gemini", "openrouter", "hf"],
        "provider_models": {},
        "api_manager": {"health_cache_seconds": 90, "probe_on_start": True,
                        "auto_heal": True, "max_retries_per_provider": 2,
                        "backoff_base_seconds": 2.0, "backoff_max_seconds": 120.0,
                        "verify_after_fallback": True},
    },
    "brain": {"system_prompt": "universal", "system_prompt_budget": 12000},
    "context": {
        "max_budget": 14000, "system_prompt_budget": 2000,
        "tool_definitions_budget": 2500, "memory_budget": 3500,
        "conversation_budget": 3500, "output_budget": 1500,
        "auto_compact": True, "compact_threshold": 0.85,
        "max_tool_result_chars": 4000, "selective_tools": True,
    },
    "memory": {
        "manager": {"importance_threshold": 0.35, "dedupe_similarity": 0.93,
                    "consolidation_interval": 25, "max_long_term_entries": 20000,
                    "decay_half_life_days": 45, "recall_k": 6, "hybrid_search": True,
                    "session_digest": True},
        "short_term": {"type": "sliding_window", "max_messages": 24,
                       "summarize_after": 14},
        "long_term": {"backend": "auto", "persist_directory": "./memory_store",
                      "collection_name": "jarvis_memory",
                      "embedding_provider": "auto",
                      "embedding_model": "nomic-embed-text", "embedding_dim": 384},
        "knowledge_base": {"enabled": True, "directory": "./knowledge",
                           "chunk_size": 900, "chunk_overlap": 120,
                           "auto_ingest": True},
        "skills": {"directory": "./skills", "auto_load": True},
        "profile": {"autoload": True},
    },
    "search": {
        "provider_order": ["tavily", "serper", "searxng", "ddg", "jina"],
        "parallel": {"enabled": True, "engines": ["tavily", "serper"],
                     "max_results_per_engine": 5},
        "tavily": {"api_key": "", "search_depth": "basic", "free_limit": 1000,
                   "period": "month"},
        "serper": {"api_key": "", "free_limit": 2500, "period": "total"},
        "searxng": {"base_url": "http://localhost:8080", "free_limit": 0},
        "duckduckgo": {"enabled": True, "base_url": "https://html.duckduckgo.com/html/",
                       "free_limit": 0},
        "jina": {"base_url": "https://s.jina.ai", "free_limit": 0},
    },
    "crawl": {
        "provider_order": ["jina_reader", "local", "firecrawl"],
        "firecrawl": {"api_key": "", "timeout": 30, "free_limit": 500,
                      "period": "total"},
        "jina_reader": {"base_url": "https://r.jina.ai", "free_limit": 0},
        "local_fallback": "trafilatura", "max_chars": 12000,
    },
    "browser": {
        "engine": "auto",
        "engine_chain": ["playwright", "selenium", "cdp"],
        "browser_type": "chromium", "headless": False,
        "launch_from_terminal": True,
        "window": {"width": 1440, "height": 900, "left": 0, "top": 0},
        "timeout": 30000, "user_data_dir": "", "remote_debugging_port": 9222,
        "binary_path": "", "user_agent": "", "stealth": True,
        "screenshot_dir": "./data/screenshots",
        "dom": {"max_elements": 120, "max_text_chars": 6000, "include_frames": True,
                "annotate_index": True},
        "hybrid": {"enabled": True, "coordinate_source": "dom",
                   "click_settle_ms": 350, "move_duration_ms": 260, "jitter": True},
    },
    "screen": {
        "enabled": True, "vision_model": False, "screenshot_format": "png",
        "screenshot_dir": "./data/screenshots", "ocr_engine": "tesseract",
        "ocr_enabled": True, "mouse_speed": 0.5, "move_duration_ms": 260,
        "failsafe": True, "backend": "auto", "display": "",
        "allow_click_outside_window": True,
    },
    "code": {
        "python": {"enabled": True, "sandbox": True, "timeout": 30},
        "bash": {"enabled": True, "timeout": 15,
                 "blocked_commands": ["rm -rf /", "mkfs", "dd if=", "shutdown"]},
    },
    "sessions": {"auto_save": True, "save_interval": 60,
                 "database": "./sessions/jarvis.db", "max_sessions": 100},
    "limits": {"enabled": True, "hard_stop_on_exhaustion": False,
               "warn_at_percent": 80, "database": "./data/quota.db",
               "global_requests_per_minute": 60},
    "interface": {"type": "cli", "theme": "dark", "show_tool_calls": True,
                  "show_thinking": True, "show_token_usage": True,
                  "show_fallback_notices": True,
                  "api": {"host": "0.0.0.0", "port": 8000,
                          "openai_compatible": True, "api_key": ""}},
}


# ── .env handling ───────────────────────────────────────────────────────────
def dotenv_values(path: Optional[Path] = None) -> Dict[str, str]:
    """Minimal ``.env`` reader (no python-dotenv required)."""
    path = path or PROJECT_ROOT / ".env"
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            values[key.strip()] = value
    except OSError:
        pass
    return values


def get_env(key: str, default: str = "") -> str:
    """os.environ first, then .env, then default."""
    if key in os.environ:
        return os.environ[key]
    return dotenv_values().get(key, default)


# ── Tiny YAML fallback ──────────────────────────────────────────────────────
# ── YAML helpers (PyYAML when present, the mini parser otherwise) ───────────
# vault.py needs these for Obsidian frontmatter, and the project must keep
# working on a bare Python install, so the fallback is not optional.
try:                                        # pragma: no cover - import shim
    import yaml as _pyyaml  # type: ignore
    _HAS_YAML = True
except Exception:                           # pragma: no cover
    _pyyaml = None  # type: ignore
    _HAS_YAML = False


def parse_yaml_text(text: str) -> Any:
    """Parse YAML text, falling back to the built-in mini parser."""
    if _HAS_YAML:
        try:
            return _pyyaml.safe_load(text)          # type: ignore[union-attr]
        except Exception as exc:
            from .utils.logger import get_logger

            get_logger("config").debug(f"PyYAML could not parse ({exc}); "
                                       "using the mini parser")
    try:
        return _mini_yaml("\n".join(line for line in text.splitlines()
                                    if not line.lstrip().startswith("#")))
    except Exception:
        return {}


def _mini_yaml_dump(data: Any, indent: int = 0) -> str:
    """Render the subset of YAML Obsidian frontmatter uses."""
    pad = "  " * indent
    lines: list[str] = []

    def scalar(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value)
        if text == "" or text.strip() != text or any(ch in text for ch in ":#[]{}&*!|>%@`\"'\n"):
            return '"' + text.replace('"', '\\"') + '"'
        return text

    if isinstance(data, Mapping):
        for key, value in data.items():
            if isinstance(value, Mapping):
                lines.append(f"{pad}{key}:")
                lines.append(_mini_yaml_dump(value, indent + 1))
            elif isinstance(value, (list, tuple)):
                if not value:
                    lines.append(f"{pad}{key}: []")
                elif all(not isinstance(item, (Mapping, list, tuple)) for item in value):
                    lines.append(f"{pad}{key}: [{', '.join(scalar(item) for item in value)}]")
                else:
                    lines.append(f"{pad}{key}:")
                    for item in value:
                        if isinstance(item, (Mapping, list, tuple)):
                            lines.append(f"{pad}- ")
                            lines.append(_mini_yaml_dump(item, indent + 1))
                        else:
                            lines.append(f"{pad}- {scalar(item)}")
            else:
                lines.append(f"{pad}{key}: {scalar(value)}")
        return "\n".join(line for line in lines if line != "")
    if isinstance(data, (list, tuple)):
        for item in data:
            lines.append(f"{pad}- {scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{scalar(data)}"


def dump_yaml(data: Mapping[str, Any]) -> str:
    """Serialise a mapping for YAML output (frontmatter, ``config show``)."""
    if _HAS_YAML:
        try:
            return _pyyaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True,
                                     default_flow_style=False).strip()  # type: ignore
        except Exception as exc:                # pragma: no cover - PyYAML internals
            from .utils.logger import get_logger

            get_logger("config").debug(f"PyYAML could not dump ({exc}); "
                                       "using the mini writer")
    return _mini_yaml_dump(dict(data)).strip()


def _mini_yaml(text: str) -> Dict[str, Any]:
    """Parse the flat/nested mapping subset of YAML that JARVIS configs use.

    Supports: nested maps by indentation, ``key: value``, inline ``[a, b]``
    lists, ``- item`` block lists, quoted strings, ints/floats/bools/null, and
    ``#`` comments.  It is intentionally small — it exists so a missing PyYAML
    never stops JARVIS from booting.
    """
    root: Dict[str, Any] = {}
    stack: list[tuple[int, Dict[str, Any]]] = [(-1, root)]
    pending_key: Optional[tuple[int, Dict[str, Any], str]] = None

    def coerce(value: str) -> Any:
        value = value.strip()
        if value == "" :
            return None
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            return value[1:-1]
        low = value.lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        if low in ("null", "none", "~"):
            return None
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                return []
            return [coerce(part) for part in _split_inline(inner)]
        if value.startswith("{") and value.endswith("}"):
            inner = value[1:-1].strip()
            out: Dict[str, Any] = {}
            for part in _split_inline(inner):
                k, _, v = part.partition(":")
                out[k.strip().strip("\"'")] = coerce(v)
            return out
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        return value

    def _split_inline(inner: str) -> Iterable[str]:
        parts, buf, quote = [], "", ""
        for ch in inner:
            if quote:
                buf += ch
                if ch == quote:
                    quote = ""
                continue
            if ch in "\"'":
                quote = ch
                buf += ch
                continue
            if ch == ",":
                parts.append(buf)
                buf = ""
                continue
            buf += ch
        if buf.strip():
            parts.append(buf)
        return [p.strip() for p in parts if p.strip()]

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = raw_line.split(" #")[0] if " #" in raw_line else raw_line
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()

        if body.startswith("- "):
            item = coerce(body[2:])
            if pending_key is not None:
                _, target, key = pending_key
                target.setdefault(key, [])
                if isinstance(target[key], list):
                    target[key].append(item)
            continue

        if ":" not in body:
            continue
        key, _, value = body.partition(":")
        key = key.strip().strip("\"'")

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if value.strip() == "":
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
            pending_key = (indent, parent, key)
        else:
            parent[key] = coerce(value)
            pending_key = None
    return root


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        try:
            return _mini_yaml(text)
        except Exception:
            return {}


# ── Merging ─────────────────────────────────────────────────────────────────
def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if key in out and isinstance(out[key], Mapping) and isinstance(value, Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _coerce_env_value(raw: str) -> Any:
    low = raw.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", ""):
        return ""
    if raw.startswith("[") :
        try:
            return json.loads(raw)
        except Exception:
            return [p.strip().strip("\"'") for p in raw[1:-1].split(",") if p.strip()]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _apply_env_overrides(cfg: Dict[str, Any], env: Mapping[str, str]) -> Dict[str, Any]:
    """``JARVIS__LLM__MODEL=x`` → ``cfg['llm']['model'] = 'x'``."""
    for key, raw in env.items():
        if not key.startswith(CONFIG_ENV_PREFIX):
            continue
        parts = [p.lower() for p in key[len(CONFIG_ENV_PREFIX):].split("__") if p]
        if not parts:
            continue
        node = cfg
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = _coerce_env_value(raw)
    return cfg


# ── Secret injection ────────────────────────────────────────────────────────
# Any of these env vars, when present, fills the matching config slot. This is
# how JARVIS keeps working with zero keys configured (every provider is simply
# skipped and the free/unauthenticated path is used).
_SECRET_MAP: Dict[str, tuple[str, ...]] = {
    "llm.groq.api_key": ("GROQ_API_KEY",),
    "llm.openrouter.api_key": ("OPENROUTER_API_KEY",),
    "llm.gemini.api_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "llm.hf.api_key": ("HUGGINGFACE_API_KEY", "HF_TOKEN"),
    "search.tavily.api_key": ("TAVILY_API_KEY",),
    "search.serper.api_key": ("SERPER_API_KEY",),
    "crawl.firecrawl.api_key": ("FIRECRAWL_API_KEY",),
}


def _set_dotted(cfg: Dict[str, Any], dotted: str, value: Any) -> None:
    """Write a value at a nested dotted path, creating sections as needed."""
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _apply_secrets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Fill every provider slot from the environment / .env.

    Values land in **both** shapes on purpose:

    * the natural nested slot — ``cfg["llm"]["groq"]["api_key"]`` — which is what
      providers and ``get(cfg, "llm.groq.api_key")`` read, and
    * the flat dotted key — ``cfg["llm.groq"]["api_key"]`` — kept for backwards
      compatibility with older code and hand-written configs.
    """
    env = dotenv_values()
    for dotted, names in _SECRET_MAP.items():
        section, key = dotted.rsplit(".", 1)
        flat = cfg.get(section)
        if not isinstance(flat, dict):
            flat = {}
            cfg[section] = flat
        existing = get(cfg, dotted) or flat.get(key)
        if existing:
            continue
        for name in names:
            value = os.environ.get(name) or env.get(name) or ""
            if value:
                _set_dotted(cfg, dotted, value)
                flat[key] = value
                break
    # extra keys: GROQ_API_KEY_2, TAVILY_API_KEY_2 ... collected into key_pool
    for section in ("llm", "search", "crawl"):
        for provider, pcfg in (cfg.get(section) or {}).items():
            if not isinstance(pcfg, dict):
                continue
            pool = list(pcfg.get("key_pool") or [])
            if pcfg.get("api_key"):
                pool.append(pcfg["api_key"])
            for name, value in list(os.environ.items()) + list(env.items()):
                if re.fullmatch(rf"{provider.upper()}_API_KEY\d+", name) and value:
                    pool.append(value)
            if pool:
                pcfg["key_pool"] = list(dict.fromkeys(pool))
    return cfg


# ── Public API ──────────────────────────────────────────────────────────────
def _candidate_paths(explicit: Optional[str | Path] = None) -> list[Path]:
    """Config files in *increasing* precedence order (last one wins).

    An explicit path (or ``JARVIS_CONFIG``) always has the final say, which is
    what makes ``JARVIS_CONFIG=/tmp/test.yaml python -m pytest`` isolate the
    whole runtime.
    """
    paths: list[Path] = [_DEFAULT_CONFIG_PATH]
    paths.extend(PROJECT_ROOT / name for name in _LOCAL_CONFIG_NAMES)
    cwd_config = Path.cwd() / "config.yaml"
    if cwd_config != _DEFAULT_CONFIG_PATH:
        paths.append(cwd_config)
    env_path = os.environ.get("JARVIS_CONFIG")
    if env_path:
        paths.append(Path(env_path).expanduser())
    if explicit:
        paths.append(Path(explicit).expanduser())
    return paths


def load_config(path: Optional[str | Path] = None, *, refresh: bool = False) -> Dict[str, Any]:
    """Load the merged JARVIS configuration (cached)."""
    global _CACHE, _CACHE_SOURCE
    if _CACHE is not None and not refresh and path is None:
        return copy.deepcopy(_CACHE)

    cfg = copy.deepcopy(DEFAULTS)
    used: list[str] = []
    for candidate in _candidate_paths(path):
        if candidate.exists():
            data = _read_yaml(candidate)
            if data:
                cfg = _deep_merge(cfg, data)
                used.append(str(candidate))

    _apply_secrets(cfg)
    _apply_env_overrides(cfg, os.environ)

    # Derived conveniences ---------------------------------------------------
    cfg.setdefault("paths", {})
    cfg["paths"]["project_root"] = str(PROJECT_ROOT)
    cfg["paths"]["config_files"] = used
    _normalise_paths(cfg)

    _CACHE = cfg
    _CACHE_SOURCE = used[-1] if used else "(defaults)"
    return copy.deepcopy(cfg)


def _normalise_paths(cfg: Dict[str, Any]) -> None:
    """Make every ``./…`` path absolute relative to the project root."""
    def fix(dotted: str) -> None:
        section, key = dotted.rsplit(".", 1)
        node = cfg.get(section)
        if isinstance(node, dict) and isinstance(node.get(key), str):
            node[key] = str(_abs(node[key]))

    for dotted in (
        "memory.long_term.persist_directory",
        "memory.knowledge_base.directory",
        "memory.skills.directory",
        "sessions.database",
        "limits.database",
        "browser.screenshot_dir",
        "screen.screenshot_dir",
    ):
        fix(dotted)


def _abs(value: str) -> Path:
    if not value:
        return PROJECT_ROOT
    p = Path(value).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def reload_config(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return load_config(path, refresh=True)


def get_config(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return load_config(path)


def config_source() -> str:
    load_config()
    return _CACHE_SOURCE or "(defaults)"


def get(cfg: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    """``get(cfg, 'llm.model')`` — safe dotted lookup."""
    node: Any = cfg
    for part in dotted.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return default
    return node
