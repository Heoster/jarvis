"""Structured logging for JARVIS.

Console output is humans-first (Rich if present), file output is JSON lines so
the API manager's fallback history can be audited later.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOG_DIR = Path(os.environ.get("JARVIS_LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))

_CONFIGURED = False
_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,
}

_COLORS = {
    "DEBUG": "\033[38;5;245m",
    "INFO": "\033[38;5;39m",
    "WARNING": "\033[38;5;214m",
    "ERROR": "\033[38;5;196m",
    "CRITICAL": "\033[48;5;196m\033[38;5;231m",
}
_RESET = "\033[0m"


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        name = record.name.replace("jarvis.", "")
        level = record.levelname
        color = _COLORS.get(level, "")
        use_color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None
        if use_color:
            head = f"{color}{stamp} {level:<7}{_RESET} \033[38;5;245m{name}{_RESET}"
        else:
            head = f"{stamp} {level:<7} {name}"
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{head} {message}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("provider", "tool", "event", "latency_ms", "session", "detail"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "info", *, log_file: Optional[str] = None,
                  quiet: bool = False, json_file: bool = True) -> None:
    """Configure the root ``jarvis`` logger once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger("jarvis")
    root.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    if not quiet:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(_ConsoleFormatter())
        root.addHandler(console)

    if json_file:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            target = Path(log_file) if log_file else LOG_DIR / "jarvis.jsonl"
            handler = logging.handlers.RotatingFileHandler(
                target, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
            handler.setFormatter(_JsonFormatter())
            root.addHandler(handler)
        except OSError:
            pass

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("chromadb").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """``get_logger("brain")`` → ``jarvis.brain`` with sane defaults."""
    if not name.startswith("jarvis"):
        name = f"jarvis.{name}"
    if not _CONFIGURED:
        setup_logging(os.environ.get("JARVIS_LOG_LEVEL", "info"),
                      quiet=os.environ.get("JARVIS_QUIET") == "1")
    return logging.getLogger(name)
