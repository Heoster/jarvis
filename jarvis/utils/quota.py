"""Free-tier quota accounting.

Every metered free API JARVIS touches is registered here with its published
allowance.  Usage lives in SQLite so the numbers survive restarts, and windows
reset themselves (``day`` / ``month`` / ``total``).

The contract the rest of the system relies on::

    qm = get_quota_manager(config)
    if qm.allow("tavily", cost=1):        # consumes 1 unit if available
        ...
    else:
        # quota exhausted → the API manager falls through to the next provider

Published free-tier numbers (verified 2025/2026, see ``PROVIDER_QUOTAS``):
Tavily 1 000 search credits/month · Serper 2 500 one-time credits ·
Firecrawl 500 credits one-time · Groq/OpenRouter/Gemini generous
per-day token or request caps · Ollama, SearXNG, DuckDuckGo HTML, Jina Reader,
trafilatura: unlimited (self-hosted or keyless).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .logger import get_logger
from .sanitizer import redact_secrets

log = get_logger("quota")

__all__ = [
    "QuotaManager", "QuotaExceeded", "QuotaState", "PROVIDER_QUOTAS",
    "get_quota_manager", "reset_quota_manager",
]


@dataclass(frozen=True)
class QuotaSpec:
    """Published allowance for one provider."""

    limit: int                 # 0 == unlimited
    period: str                # second | minute | hour | day | month | total
    unit: str = "request"
    category: str = "llm"      # llm | search | crawl | embed
    note: str = ""

    @property
    def unlimited(self) -> bool:
        return self.limit <= 0


PROVIDER_QUOTAS: Dict[str, QuotaSpec] = {
    # ── LLM providers ───────────────────────────────────────────────────────
    "ollama":      QuotaSpec(0, "total", "token", "llm", "Local — unlimited"),
    "groq":        QuotaSpec(14_400, "day", "request", "llm", "Free tier ~14.4k req/day"),
    "openrouter":  QuotaSpec(50, "day", "request", "llm", "Free models: 50 req/day"),
    "gemini":      QuotaSpec(1_500, "day", "request", "llm", "Free tier ~1.5k req/day"),
    "hf":          QuotaSpec(1_000, "day", "request", "llm", "Serverless inference cap"),
    "stub":        QuotaSpec(0, "total", "request", "llm", "Offline fallback"),
    # ── Search providers ────────────────────────────────────────────────────
    "tavily":      QuotaSpec(1_000, "month", "credit", "search", "1 000 credits/month"),
    "serper":      QuotaSpec(2_500, "total", "credit", "search", "2 500 credits one-time"),
    "searxng":     QuotaSpec(0, "total", "query", "search", "Self-hosted — unlimited"),
    "ddg":         QuotaSpec(0, "total", "query", "search", "Keyless HTML endpoint"),
    "jina":        QuotaSpec(0, "total", "query", "search", "Keyless search endpoint"),
    # ── Crawl / reader providers ────────────────────────────────────────────
    "jina_reader": QuotaSpec(0, "total", "page", "crawl", "Keyless reader"),
    "local":       QuotaSpec(0, "total", "page", "crawl", "trafilatura/builtin parser"),
    "firecrawl":   QuotaSpec(500, "total", "credit", "crawl", "500 credits one-time"),
    # ── Embeddings ──────────────────────────────────────────────────────────
    "ollama_embed": QuotaSpec(0, "total", "token", "embed", "Local embeddings"),
    "local_embed":  QuotaSpec(0, "total", "token", "embed", "Hash/TF-IDF embeddings"),
}


class QuotaExceeded(RuntimeError):
    """Raised when a provider's free allowance is used up."""

    def __init__(self, provider: str, state: "QuotaState"):
        self.provider = provider
        self.state = state
        super().__init__(
            f"free-tier quota exhausted for '{provider}': "
            f"{state.used}/{state.limit} {state.unit}(s) per {state.period}"
        )


@dataclass
class QuotaState:
    provider: str
    used: int = 0
    limit: int = 0
    period: str = "total"
    unit: str = "request"
    resets_at: Optional[float] = None
    category: str = "llm"

    @property
    def unlimited(self) -> bool:
        return self.limit <= 0

    @property
    def remaining(self) -> int:
        return -1 if self.unlimited else max(0, self.limit - self.used)

    @property
    def percent_used(self) -> float:
        if self.unlimited:
            return 0.0
        return round(100.0 * self.used / max(1, self.limit), 2)

    @property
    def exhausted(self) -> bool:
        return not self.unlimited and self.used >= self.limit

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "used": self.used, "limit": self.limit,
            "remaining": self.remaining, "unit": self.unit, "period": self.period,
            "percent_used": self.percent_used, "exhausted": self.exhausted,
            "resets_at": (datetime.fromtimestamp(self.resets_at, timezone.utc).isoformat()
                          if self.resets_at else None),
            "category": self.category,
        }


_WINDOW_SECONDS = {
    "second": 1,
    "minute": 60,
    "hour": 3_600,
    "day": 86_400,
    "month": 2_592_000,      # 30 days, matching how these free tiers publish it
    "total": 0,
}


def _window_key(period: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if period == "second":
        return now.strftime("%Y%m%d%H%M%S")
    if period == "minute":
        return now.strftime("%Y%m%d%H%M")
    if period == "hour":
        return now.strftime("%Y%m%d%H")
    if period == "day":
        return now.strftime("%Y%m%d")
    if period == "month":
        return now.strftime("%Y%m")
    return "all"


def _next_reset(period: str, now: Optional[datetime] = None) -> Optional[float]:
    now = now or datetime.now(timezone.utc)
    if period == "total":
        return None
    if period == "month":
        first_next = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        return first_next.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if period == "day":
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.timestamp()
    if period == "hour":
        nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return nxt.timestamp()
    if period == "minute":
        nxt = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
        return nxt.timestamp()
    return time.time() + 1


class QuotaManager:
    """SQLite-backed free-tier budget with atomic consume/refund."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 db_path: Optional[str | Path] = None):
        config = config or {}
        limits_cfg = config.get("limits", {}) if isinstance(config, Mapping) else {}
        self.enabled = bool(limits_cfg.get("enabled", True))
        self.hard_stop = bool(limits_cfg.get("hard_stop_on_exhaustion", False))
        self.warn_at = float(limits_cfg.get("warn_at_percent", 80))

        default_db = Path(__file__).resolve().parents[2] / "data" / "quota.db"
        db = db_path or limits_cfg.get("database") or self._discover(config) or default_db
        self.db_path = Path(db).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        # Provider allowances come from PROVIDER_QUOTAS, optionally overridden
        # by the free_limit/period keys in config.yaml.
        self.specs: Dict[str, QuotaSpec] = dict(PROVIDER_QUOTAS)
        self._apply_config_limits(config)

    # ── setup ──────────────────────────────────────────────────────────────
    @staticmethod
    def _discover(config: Mapping[str, Any]) -> Optional[str]:
        for section in ("search", "crawl", "llm"):
            node = config.get(section)
            if isinstance(node, Mapping):
                for value in node.values():
                    if isinstance(value, Mapping) and value.get("database"):
                        return str(value["database"])
        return None

    def _apply_config_limits(self, config: Mapping[str, Any]) -> None:
        for section in ("search", "crawl", "llm"):
            node = config.get(section)
            if not isinstance(node, Mapping):
                continue
            for provider, pcfg in node.items():
                if not isinstance(pcfg, Mapping):
                    continue
                limit = pcfg.get("free_limit")
                period = pcfg.get("period")
                if limit is None and period is None:
                    continue
                base = self.specs.get(provider)
                self.specs[provider] = QuotaSpec(
                    limit=int(limit if limit is not None else (base.limit if base else 0)),
                    period=str(period or (base.period if base else "total")),
                    unit=base.unit if base else "request",
                    category=base.category if base else section,
                    note=base.note if base else "from config",
                )

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_usage (
                    provider TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    period TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    limit_value INTEGER NOT NULL DEFAULT 0,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY (provider, window_key)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    provider TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_quota_events_provider "
                "ON quota_events(provider, ts)"
            )

    # ── core operations ────────────────────────────────────────────────────
    def spec(self, provider: str, *, category: str = "llm") -> QuotaSpec:
        return self.specs.get(provider, QuotaSpec(0, "total", "request", category))

    def state(self, provider: str) -> QuotaState:
        spec = self.spec(provider)
        window = _window_key(spec.period)
        with self._lock:
            cur = self._conn.execute(
                "SELECT used FROM quota_usage WHERE provider=? AND window_key=?",
                (provider, window),
            )
            row = cur.fetchone()
        used = int(row[0]) if row else 0
        return QuotaState(provider=provider, used=used, limit=spec.limit,
                          period=spec.period, unit=spec.unit,
                          resets_at=_next_reset(spec.period), category=spec.category)

    def check(self, provider: str, cost: int = 1) -> QuotaState:
        """Non-consuming availability check."""
        if not self.enabled:
            return QuotaState(provider=provider, limit=0, period="total")
        state = self.state(provider)
        if state.unlimited:
            return state
        if state.used + cost > state.limit:
            return state
        if state.percent_used >= self.warn_at:
            log.warning(f"quota {provider}: {state.used}/{state.limit} "
                        f"{state.unit}s used ({state.percent_used}%)")
        return state

    def allow(self, provider: str, cost: int = 1, *, raise_on_exhausted: bool = False) -> bool:
        """Consume ``cost`` units if the provider still has budget."""
        if not self.enabled:
            return True
        spec = self.spec(provider)
        if spec.unlimited:
            return True

        window = _window_key(spec.period)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "SELECT used FROM quota_usage WHERE provider=? AND window_key=?",
                (provider, window),
            )
            row = cur.fetchone()
            used = int(row[0]) if row else 0
            if used + cost > spec.limit:
                self._record_event(provider, 0, "blocked",
                                   f"{used}+{cost} > {spec.limit}/{spec.period}")
                log.warning(f"quota exhausted: {provider} "
                            f"({used}/{spec.limit} per {spec.period}) — will fall back")
                if raise_on_exhausted:
                    raise QuotaExceeded(provider, self.state(provider))
                return False
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO quota_usage (provider, window_key, period, used,
                                             limit_value, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, window_key) DO UPDATE SET
                        used = used + excluded.used,
                        last_seen = excluded.last_seen
                    """,
                    (provider, window, spec.period, cost, spec.limit, now, now),
                )
            self._record_event(provider, cost, "consume", window)
            new_used = used + cost
        if spec.limit and (100.0 * new_used / spec.limit) >= self.warn_at:
            log.warning(f"quota {provider}: {new_used}/{spec.limit} "
                        f"{spec.unit}s used this {spec.period}")
        return True

    def refund(self, provider: str, cost: int = 1, *, reason: str = "manual") -> None:
        """Give units back (e.g. a request failed before reaching the provider)."""
        if not self.enabled or cost <= 0:
            return
        spec = self.spec(provider)
        if spec.unlimited:
            return
        window = _window_key(spec.period)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE quota_usage SET used = MAX(0, used - ?) "
                "WHERE provider=? AND window_key=?",
                (cost, provider, window),
            )
            self._record_event(provider, -cost, f"refund:{reason}", window)

    def _record_event(self, provider: str, units: int, action: str,
                      detail: str = "") -> None:
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO quota_events (ts, provider, units, action, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (time.time(), provider, units, action, redact_secrets(detail)),
                )
        except sqlite3.Error:
            pass

    # ── reporting ──────────────────────────────────────────────────────────
    def snapshot(self) -> List[Dict[str, Any]]:
        rows = []
        for provider in sorted(self.specs):
            try:
                rows.append(self.state(provider).as_dict())
            except Exception:
                continue
        return rows

    def summary(self) -> str:
        lines = []
        for row in self.snapshot():
            if row["limit"] == 0:
                continue
            bar_len = 18
            filled = int(bar_len * min(1.0, row["percent_used"] / 100.0))
            bar = "█" * filled + "░" * (bar_len - filled)
            flag = " ⚠" if row["percent_used"] >= self.warn_at else ""
            lines.append(
                f"{row['provider']:<12} [{bar}] {row['percent_used']:>6.2f}%  "
                f"{row['used']}/{row['limit']} {row['unit']}s/{row['period']}{flag}"
            )
        return "\n".join(lines) or "no metered providers configured — all free/unlimited"

    def next_available(self, providers: List[str], cost: int = 1) -> Optional[str]:
        """First provider in ``providers`` with budget left (fallback helper)."""
        for provider in providers:
            if self.allow(provider, cost=0):
                return provider
        return None

    def reset(self, provider: Optional[str] = None) -> None:
        with self._lock, self._conn:
            if provider:
                self._conn.execute("DELETE FROM quota_usage WHERE provider=?", (provider,))
                self._conn.execute("DELETE FROM quota_events WHERE provider=?", (provider,))
            else:
                self._conn.execute("DELETE FROM quota_usage")
                self._conn.execute("DELETE FROM quota_events")
        log.info(f"quota reset: {provider or 'all providers'}")

    def recent_events(self, limit: int = 25) -> List[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT ts, provider, units, action, detail FROM quota_events "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {"ts": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
             "provider": provider, "units": units, "action": action, "detail": detail}
            for ts, provider, units, action, detail in cur.fetchall()
        ]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_MANAGER: Optional[QuotaManager] = None


def get_quota_manager(config: Optional[Mapping[str, Any]] = None, *,
                      refresh: bool = False) -> QuotaManager:
    """Process-wide quota manager (shared by search, crawl and the LLM chain)."""
    global _MANAGER
    if _MANAGER is None or refresh:
        if _MANAGER is not None:
            _MANAGER.close()
        _MANAGER = QuotaManager(config or {})
    return _MANAGER


def reset_quota_manager() -> None:
    global _MANAGER
    if _MANAGER is not None:
        _MANAGER.close()
    _MANAGER = None
