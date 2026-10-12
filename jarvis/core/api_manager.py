"""API Manager — controls, verifies and auto-falls-back across free providers.

This is the module that makes "it never dies" true.  Responsibilities:

1. **Control**   — one place that decides which provider serves a request,
   honouring the operator's free-tier quota (via :mod:`jarvis.utils.quota`)
   and per-provider rate limits.
2. **Verify**    — providers are probed before use, and a provider that just
   failed is re-probed in the background ("auto-heal") so the system returns to
   the fastest option instead of drifting permanently to the fallback.
3. **Fallback**  — an ordered chain (``llm.fallback_chain``) is walked on any
   error: missing key → timeout → HTTP 5xx → 429 → exhausted quota → bad model.
   Every step is recorded so ``jarvis api status`` can explain what happened.

Design notes
------------
* Everything is synchronous internally but :meth:`APIManager.acomplete` wraps it
  in a thread so the async orchestrator does not block.
* Health is cached for ``llm.api_manager.health_cache_seconds``.
* Failures apply exponential backoff per provider, capped by config.
* The chain is ``ollama → groq → gemini → openrouter → hf``.  When every member
  fails the manager raises :class:`AllProvidersFailed` and the caller shows the
  error — it never fabricates an answer.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from ..utils.quota import QuotaManager, get_quota_manager
from ..utils.rate_limiter import LimitRegistry
from ..utils.sanitizer import redact_secrets
from .messages import AIMessage, ensure_messages
from .providers import (
    PROVIDER_SPECS, BaseProvider, LLMError, LLMResponse, ProviderUnavailable,
    ProviderSpec, build_provider, is_model_unavailable,
)

log = get_logger("api")

__all__ = [
    "ProviderHealth", "ProviderState", "APIManager", "AllProvidersFailed",
    "FallbackEvent", "get_api_manager", "reset_api_manager",
]

# Provider → (requests per minute, requests per day) published free-tier limits.
# These mirror PROVIDER_QUOTAS in utils/quota.py but expressed as windows the
# rate limiter can enforce in real time.
_PROVIDER_RATE_HINTS: Dict[str, Tuple[int, int]] = {
    "groq": (30, 14_400),
    "openrouter": (20, 50),
    "gemini": (15, 1_500),
    "hf": (10, 1_000),
    "tavily": (10, 1_000),
    "serper": (10, 2_500),
    "firecrawl": (10, 500),
    "ollama": (600, 0),        # local: effectively unlimited
    "stub": (600, 0),
}


class AllProvidersFailed(RuntimeError):
    """Every provider in the chain was tried and none answered.

    ``attempts`` lists ``(provider, reason)`` in the order they were tried, so the
    operator can see exactly why each one was skipped or failed.
    """

    def __init__(self, message: str, attempts: Sequence[Tuple[str, str]] = ()):
        super().__init__(message)
        self.attempts: List[Tuple[str, str]] = list(attempts)


#: production order.  ``stub`` is deliberately absent: a turn that no real model
#: can answer is reported as an error, not answered with a canned template.
DEFAULT_CHAIN: Tuple[str, ...] = ("ollama", "groq", "gemini", "openrouter", "hf")


@dataclass
class FallbackEvent:
    ts: float
    from_provider: str
    to_provider: str
    reason: str
    detail: str = ""

    def line(self) -> str:
        when = datetime.fromtimestamp(self.ts, timezone.utc).strftime("%H:%M:%S")
        extra = f" — {self.detail}" if self.detail else ""
        return f"{when} {self.from_provider} → {self.to_provider}: {self.reason}{extra}"

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "when": datetime.fromtimestamp(
            self.ts, timezone.utc).isoformat(), "from": self.from_provider,
            "to": self.to_provider, "reason": self.reason, "detail": self.detail}


class ProviderHealth:
    """Mutable runtime state for one provider."""

    def __init__(self, spec: ProviderSpec):
        self.spec = spec
        self.status: str = "unknown"      # unknown|healthy|degraded|down|unconfigured|exhausted
        self.detail: str = ""
        self.latency_ms: int = 0
        self.checked_at: float = 0.0
        self.consecutive_failures: int = 0
        self.total_calls: int = 0
        self.total_failures: int = 0
        self.last_error: str = ""
        # Set when the provider reports its model is gone (404 / model_not_supported).
        # Retrying cannot fix that, so the provider is skipped for the rest of the session.
        self.disabled: bool = False
        self.backoff_until: float = 0.0
        self.provider: Optional[BaseProvider] = None

    @property
    def available(self) -> bool:
        return self.status in ("healthy", "degraded", "unknown")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.spec.name,
            "status": self.status,
            "detail": redact_secrets(self.detail),
            "latency_ms": self.latency_ms,
            "checked_ago_s": round(time.time() - self.checked_at, 1) if self.checked_at else None,
            "calls": self.total_calls,
            "failures": self.total_failures,
            "consecutive_failures": self.consecutive_failures,
            "backoff_s": max(0.0, round(self.backoff_until - time.time(), 1)),
            "free_limits": self.spec.free_limits,
            "requires_key": self.spec.requires_key,
            "last_error": redact_secrets(self.last_error)[:200],
        }


ProviderState = ProviderHealth  # backwards-friendly alias


class APIManager:
    """Owns every provider, its health, its quota and its fallback order."""

    def __init__(self, config: Mapping[str, Any], *,
                 quota: Optional[QuotaManager] = None,
                 providers: Optional[Mapping[str, BaseProvider]] = None):
        self.config = dict(config or {})
        self.llm_cfg: Mapping[str, Any] = self.config.get("llm") or {}
        self.mgr_cfg: Mapping[str, Any] = self.llm_cfg.get("api_manager") or {}

        self.health_cache_seconds = float(self.mgr_cfg.get("health_cache_seconds", 90))
        self.max_retries = int(self.mgr_cfg.get("max_retries_per_provider", 2))
        self.backoff_base = float(self.mgr_cfg.get("backoff_base_seconds", 2.0))
        self.backoff_max = float(self.mgr_cfg.get("backoff_max_seconds", 120.0))
        self.auto_heal = bool(self.mgr_cfg.get("auto_heal", True))
        self.verify_after_fallback = bool(self.mgr_cfg.get("verify_after_fallback", True))

        self.quota = quota or get_quota_manager(self.config)
        self.limiters = LimitRegistry()
        self._lock = threading.RLock()
        self.events: List[FallbackEvent] = []

        # build provider instances (injected ones win, for tests)
        self.states: Dict[str, ProviderHealth] = {}
        for name, spec in PROVIDER_SPECS.items():
            state = ProviderHealth(spec)
            if providers and name in providers:
                state.provider = providers[name]
                state.status = "unknown"
            else:
                try:
                    state.provider = build_provider(name, self.config)
                except Exception as exc:  # pragma: no cover - defensive
                    state.status = "unconfigured"
                    state.detail = f"cannot construct: {exc}"
            self.states[name] = state

        for state in self.states.values():
            if state.provider is not None and not state.provider.is_configured():
                state.status = "unconfigured"
                state.detail = state.provider.availability_problem()

        self.chain: List[str] = self._normalise_chain()
        self._heal_thread: Optional[threading.Thread] = None
        self._stop_heal = threading.Event()

    # ── chain handling ─────────────────────────────────────────────────────
    def _normalise_chain(self) -> List[str]:
        """Forced provider first (if any), then ``llm.fallback_chain`` in order.

        Unknown names are dropped with a warning; duplicates keep their first
        position.  ``stub`` only appears if the operator asked for it explicitly.
        """
        chain: List[str] = []
        forced = str(self.llm_cfg.get("provider", "auto")).lower()
        if forced and forced not in ("auto", ""):
            chain.append(forced)
        configured = self.llm_cfg.get("fallback_chain") or list(DEFAULT_CHAIN)
        for name in configured:
            name = str(name).lower()
            if name not in chain:
                chain.append(name)
        known = [n for n in chain if n in self.states]
        for unknown in (n for n in chain if n not in self.states):
            log.warning(f"ignoring unknown provider '{unknown}' in the fallback chain")
        return known

    def _ordered_candidates(self, *, skip: Iterable[str] = ()) -> List[str]:
        skip = set(skip)
        return [n for n in self.chain if n not in skip]

    # ── health ─────────────────────────────────────────────────────────────
    def check_health(self, name: str, *, force: bool = False,
                     timeout: float = 8.0) -> ProviderHealth:
        """Probe one provider (cached).  Never raises."""
        state = self.states.get(name)
        if state is None or state.provider is None:
            return state or ProviderHealth(PROVIDER_SPECS.get(name,
                                                              ProviderSpec(name=name)))
        now = time.time()
        if not force and state.checked_at and (now - state.checked_at) < self.health_cache_seconds:
            return state
        if not state.provider.is_configured():
            state.status = "unconfigured"
            state.detail = state.provider.availability_problem()
            state.checked_at = now
            return state

        ok, latency, detail = state.provider.probe()
        state.checked_at = now
        state.latency_ms = latency
        state.detail = detail
        if ok:
            state.status = "healthy" if latency < 5_000 else "degraded"
            state.consecutive_failures = 0
            state.backoff_until = 0.0
        else:
            state.status = "down"
            state.last_error = detail
            state.consecutive_failures += 1
            state.backoff_until = now + self._backoff(state.consecutive_failures)
        log.debug(f"health {name}: {state.status} ({latency} ms) {detail[:120]}")
        return state

    def _backoff(self, failures: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2 ** max(0, failures - 1)))

    def verify_all(self, *, force: bool = False) -> Dict[str, Dict[str, Any]]:
        """Probe every provider; used at startup and by ``jarvis doctor``."""
        for name in self.states:
            self.check_health(name, force=force)
        return self.status()

    def verified_provider(self) -> Optional[str]:
        """First provider in the chain that is healthy *right now*."""
        for name in self._ordered_candidates():
            state = self.check_health(name)
            if state.status in ("healthy", "degraded") and self._within_limits(name):
                return name
        return None

    def _within_limits(self, name: str) -> bool:
        rpm, rpd = _PROVIDER_RATE_HINTS.get(name, (60, 0))
        if rpd:
            day = self.limiters.get(f"{name}:day", rpd, 86_400.0)
            if day.used >= rpd and day.wait_time() > 60:
                return False
        limiter = self.limiters.get(f"{name}:min", rpm, 60.0)
        if limiter.used >= rpm:
            return False
        return self.quota.allow(name, cost=0)

    # ── events ─────────────────────────────────────────────────────────────
    def _record_fallback(self, from_provider: str, to_provider: str,
                         reason: str, detail: str = "") -> None:
        event = FallbackEvent(time.time(), from_provider, to_provider, reason,
                              redact_secrets(detail)[:300])
        with self._lock:
            self.events.append(event)
            if len(self.events) > 200:
                del self.events[:-200]
        log.warning(f"fallback {event.line()}")

    def recent_fallbacks(self, limit: int = 15) -> List[Dict[str, Any]]:
        return [e.as_dict() for e in self.events[-limit:]]

    # ── auto-heal ──────────────────────────────────────────────────────────
    def start_auto_heal(self, interval: float = 120.0) -> None:
        """Background thread that re-probes down providers and restores order."""
        if not self.auto_heal or (self._heal_thread and self._heal_thread.is_alive()):
            return
        self._stop_heal.clear()

        def loop() -> None:
            while not self._stop_heal.wait(interval):
                for name, state in list(self.states.items()):
                    if state.status in ("down", "exhausted") and \
                            time.time() >= state.backoff_until:
                        before = state.status
                        self.check_health(name, force=True)
                        if state.status in ("healthy", "degraded") and before != state.status:
                            log.info(f"provider recovered: {name} "
                                     f"({state.latency_ms} ms)")
                if self._stop_heal.is_set():
                    break

        self._heal_thread = threading.Thread(target=loop, name="jarvis-api-heal",
                                             daemon=True)
        self._heal_thread.start()

    def stop_auto_heal(self) -> None:
        self._stop_heal.set()

    def _verify_later(self, name: str, delay: float = 20.0) -> None:
        """Re-probe a provider we just fell back from (verification)."""
        if not self.verify_after_fallback:
            return

        def worker() -> None:
            time.sleep(delay)
            state = self.states.get(name)
            if state is None:
                return
            self.check_health(name, force=True)
            log.info(f"post-fallback verification {name}: {state.status} "
                     f"({state.detail[:120]})")

        threading.Thread(target=worker, name=f"jarvis-verify-{name}",
                         daemon=True).start()

    # ── completion with fallback ───────────────────────────────────────────
    def _attempt(self, name: str, provider: BaseProvider, msgs: Sequence[Any],
                 context: Dict[str, Any], budget_tokens: int
                 ) -> Tuple[Optional[LLMResponse], Optional[Exception]]:
        """Call one provider, retrying only rate limits.  Never raises."""
        state = self.states[name]
        attempt = 0
        while True:
            attempt += 1
            start = time.perf_counter()
            try:
                response = provider.complete(msgs, **context)  # type: ignore[arg-type]
            except (LLMError, ProviderUnavailable) as exc:
                self._mark_failure(state, str(exc))
                if not isinstance(exc, ProviderUnavailable) and is_model_unavailable(exc):
                    state.disabled = True
                    state.status = "disabled"
                    state.detail = f"model unavailable: {redact_secrets(str(exc))[:120]}"
                    log.warning(f"{name} disabled for this session: {state.detail}")
                    return None, exc
                retryable = getattr(exc, "retryable", True)
                if retryable and attempt <= self.max_retries and "429" in str(exc):
                    wait = min(8.0, self.backoff_base * attempt)
                    log.warning(f"{name} rate limited, retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                state.status = "down"
                log.warning(f"{name} failed ({attempt} attempt(s)): "
                            f"{redact_secrets(str(exc))[:180]}")
                return None, exc
            except Exception as exc:  # unexpected adapter bug — still just a failure
                self._mark_failure(state, f"{type(exc).__name__}: {exc}")
                state.status = "down"
                log.error(f"{name} crashed: {exc}")
                return None, exc

            if budget_tokens:
                self.quota.refund(name, 1)  # replace the coarse unit
                self.quota.allow(name, cost=max(1, budget_tokens // 1000))
            state.total_calls += 1
            state.consecutive_failures = 0
            state.status = "healthy"
            state.last_error = ""
            log.debug(f"completion via {name} in "
                      f"{int((time.perf_counter() - start) * 1000)} ms")
            return response, None

    def _mark_failure(self, state: ProviderHealth, detail: str) -> None:
        state.total_failures += 1
        state.consecutive_failures += 1
        state.last_error = detail
        state.backoff_until = time.time() + self._backoff(state.consecutive_failures)

    def _gate(self, name: str, provider: BaseProvider) -> str:
        """Why ``name`` must not be called right now, or ``""`` if it may be."""
        state = self.states[name]
        if not provider.is_configured():
            state.status = "unconfigured"
            state.detail = provider.availability_problem()
            return state.detail
        if state.disabled:
            return f"disabled this session: {state.detail}"
        if time.time() < state.backoff_until:
            wait = int(state.backoff_until - time.time())
            return f"cooling down {wait}s after: {state.last_error[:80]}"
        health = self.check_health(name)
        if health.status == "down":
            return f"unhealthy: {health.detail[:100]}"
        if not self.quota.allow(name, cost=1):
            state.status = "exhausted"
            state.detail = "quota exhausted (free limit)"
            return "free quota exhausted"
        rpm, _rpd = _PROVIDER_RATE_HINTS.get(name, (60, 0))
        limiter = self.limiters.get(f"{name}:min", rpm, 60.0)
        if not limiter.acquire(timeout=0.0):
            wait = limiter.wait_time()
            if wait > 5:
                state.detail = f"local rate limit hit, retry in {wait:.0f}s"
                return state.detail
            limiter.acquire(timeout=min(wait + 1, 10))
        return ""

    def complete(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None,
                 json_mode: bool = False,
                 preferred: Optional[str] = None,
                 budget_tokens: int = 0) -> LLMResponse:
        """Try providers in chain order until one answers.

        Order: ollama (only if it is actually served) → groq → gemini →
        openrouter → hf.  If every one of them fails, :class:`AllProvidersFailed`
        is raised with the per-provider reasons; callers show that as an error.

        ``budget_tokens`` (when > 0) charges the provider's quota by estimated
        token cost so long generations count against the right allowance.
        """
        msgs = ensure_messages(messages)
        context = {"tools": tools, "temperature": temperature,
                   "max_tokens": max_tokens, "json_mode": json_mode}
        candidates = self._ordered_candidates()
        if preferred and preferred in self.states:
            candidates = [preferred] + [c for c in candidates if c != preferred]

        trace: List[Tuple[str, str]] = []   # every provider, in order, with why

        for index, name in enumerate(candidates):
            provider = self.states[name].provider
            if provider is None:
                trace.append((name, "not constructed"))
                continue
            reason = self._gate(name, provider)
            if reason:
                trace.append((name, reason))
                continue

            response, error = self._attempt(name, provider, msgs, context, budget_tokens)
            if response is not None:
                if trace:  # someone earlier in the chain was skipped or failed
                    response.fallback_from = trace[0][0]
                    response.degraded = True
                return response

            detail = redact_secrets(str(error)) if error else "unavailable"
            trace.append((name, detail))
            upcoming = candidates[index + 1] if index + 1 < len(candidates) else "error"
            self._record_fallback(name, upcoming, detail)
            self._verify_later(name)

        summary = "; ".join(f"{n}: {r[:120]}" for n, r in trace) or "chain is empty"
        raise AllProvidersFailed(
            f"every LLM provider failed — {summary}", attempts=trace)

    async def acomplete(self, messages: Sequence[Any], **kwargs: Any) -> LLMResponse:
        """Async wrapper — runs the blocking chain in a worker thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.complete(messages, **kwargs))

    # ── reporting ──────────────────────────────────────────────────────────
    def status(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for name, state in self.states.items():
            row = state.as_dict()
            quota_state = self.quota.state(name)
            row["quota"] = quota_state.as_dict()
            out[name] = row
        return out

    def active_provider(self) -> str:
        for name in self.chain:
            state = self.states[name]
            if state.status in ("healthy",) and self.quota.allow(name, cost=0):
                return name
        for name in self.chain:
            if self.states[name].status in ("unknown", "degraded"):
                return name
        return "none"

    def summary_table(self) -> List[Tuple[str, str, str, str]]:
        """``(provider, status, latency, note)`` rows for the CLI."""
        rows: List[Tuple[str, str, str, str]] = []
        for name in self.chain:
            state = self.states[name]
            quota_state = self.quota.state(name)
            latency = f"{state.latency_ms} ms" if state.latency_ms else "-"
            if quota_state.unlimited:
                quota_text = "unlimited"
            else:
                quota_text = (f"{quota_state.used}/{quota_state.limit} "
                              f"{quota_state.unit}/{quota_state.period}")
            note = state.detail or quota_text
            rows.append((name, state.status, latency, note[:70]))
        return rows

    def report(self, *, verify: bool = False) -> str:
        """Human-readable provider report, including the fallback history."""
        if verify:
            self.verify_all(force=True)
        lines = [f"LLM providers (mode: {self.llm_cfg.get('provider', 'auto')}, "
                 f"free-tier only):", ""]
        lines.append(f"  {'provider':<12} {'status':<13} {'latency':<9} note")
        lines.append("  " + "-" * 68)
        for name, status, latency, note in self.summary_table():
            lines.append(f"  {name:<12} {status:<13} {latency:<9} {note}")
        lines.append("")
        lines.append(f"  fallback chain : {' → '.join(self.chain)}")
        lines.append(f"  would answer   : {self.active_provider()}")

        fallbacks = self.recent_fallbacks(limit=5)
        if fallbacks:
            lines.append("")
            lines.append("  recent automatic fallbacks (verified by the next success):")
            for event in fallbacks:
                when = time.strftime("%H:%M:%S", time.localtime(event["at"]))
                lines.append(f"    {when}  {event['from']} → {event['to']}"
                             f"  ({event.get('reason', '')[:60]})")
        if verify:
            lines.append("")
            lines.append("  live probe results are reflected in the status column above.")
        return "\n".join(lines)

    def close(self) -> None:
        self.stop_auto_heal()


def api_health_report(config: Optional[Mapping[str, Any]] = None,
                      *, verify: bool = False) -> str:
    """Module-level helper used by ``python -m jarvis doctor``."""
    return get_api_manager(config).report(verify=verify)


# ── singleton ───────────────────────────────────────────────────────────────
_MANAGER: Optional[APIManager] = None
_MANAGER_LOCK = threading.Lock()


def get_api_manager(config: Optional[Mapping[str, Any]] = None,
                    *, refresh: bool = False) -> APIManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None or refresh:
            if _MANAGER is not None:
                _MANAGER.close()
            if config is None:
                from ..config import load_config

                config = load_config()
            _MANAGER = APIManager(config)
        return _MANAGER


def reset_api_manager() -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            _MANAGER.close()
        _MANAGER = None
