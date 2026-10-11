"""Rate limiting primitives (sync + async) used by the API manager.

Two layers matter in JARVIS:

* a **global** ceiling so a looping agent cannot hammer anything;
* a **per-provider** ceiling matching each free tier's RPM/RPD.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

__all__ = ["TokenBucket", "RateLimiter", "AsyncRateLimiter"]


@dataclass
class _BucketState:
    tokens: float
    updated: float


class TokenBucket:
    """Classic token bucket: ``capacity`` burst, ``refill_per_second`` average."""

    def __init__(self, capacity: float = 10, refill_per_second: float = 1.0):
        self.capacity = max(1.0, float(capacity))
        self.refill = max(0.0, float(refill_per_second))
        self._state = _BucketState(tokens=self.capacity, updated=time.monotonic())
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._state.updated
        if elapsed > 0:
            self._state.tokens = min(self.capacity,
                                     self._state.tokens + elapsed * self.refill)
            self._state.updated = now

    def consume(self, amount: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._state.tokens >= amount:
                self._state.tokens -= amount
                return True
            return False

    def wait_time(self, amount: float = 1.0) -> float:
        with self._lock:
            self._refill()
            if self._state.tokens >= amount:
                return 0.0
            if self.refill <= 0:
                return float("inf")
            return (amount - self._state.tokens) / self.refill

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._state.tokens


class RateLimiter:
    """Sliding-window limiter: at most ``max_calls`` per ``period`` seconds."""

    def __init__(self, max_calls: int = 60, period: float = 60.0):
        self.max_calls = max(1, int(max_calls))
        self.period = max(0.001, float(period))
        self._events: Deque[float] = deque()
        # RLock: snapshot() holds the lock and calls wait_time(), which locks too
        self._lock = threading.RLock()

    def _trim(self, now: Optional[float] = None) -> None:
        now = now or time.monotonic()
        cutoff = now - self.period
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def try_acquire(self) -> bool:
        with self._lock:
            self._trim()
            if len(self._events) >= self.max_calls:
                return False
            self._events.append(time.monotonic())
            return True

    def wait_time(self) -> float:
        with self._lock:
            self._trim()
            if len(self._events) < self.max_calls:
                return 0.0
            return max(0.0, self.period - (time.monotonic() - self._events[0]))

    def acquire(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.try_acquire():
                return True
            time.sleep(min(0.25, max(0.01, self.wait_time())))
        return self.try_acquire()

    @property
    def used(self) -> int:
        with self._lock:
            self._trim()
            return len(self._events)

    def snapshot(self) -> Dict[str, float]:
        with self._lock:
            self._trim()
            return {
                "used": len(self._events),
                "limit": self.max_calls,
                "period_seconds": self.period,
                "retry_after": self.wait_time() if len(self._events) >= self.max_calls else 0.0,
            }


class AsyncRateLimiter:
    """Async sibling of :class:`RateLimiter` (same semantics, no busy waiting)."""

    def __init__(self, max_calls: int = 60, period: float = 60.0):
        self.max_calls = max(1, int(max_calls))
        self.period = max(0.001, float(period))
        self._events: Deque[float] = deque()
        self._lock = asyncio.Lock()

    def _trim(self, now: float) -> None:
        cutoff = now - self.period
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    async def acquire(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            async with self._lock:
                now = time.monotonic()
                self._trim(now)
                if len(self._events) < self.max_calls:
                    self._events.append(now)
                    return True
                wait = max(0.01, self.period - (now - self._events[0]))
            if time.monotonic() + wait > deadline:
                return False
            await asyncio.sleep(min(wait, 1.0))

    async def wait_time(self) -> float:
        async with self._lock:
            now = time.monotonic()
            self._trim(now)
            if len(self._events) < self.max_calls:
                return 0.0
            return max(0.0, self.period - (now - self._events[0]))


class LimitRegistry:
    """Lazily-created named limiters (one per provider+window)."""

    def __init__(self) -> None:
        self._limiters: Dict[str, RateLimiter] = {}
        self._async: Dict[str, AsyncRateLimiter] = {}
        self._lock = threading.Lock()

    def get(self, name: str, max_calls: int, period: float) -> RateLimiter:
        with self._lock:
            limiter = self._limiters.get(name)
            if limiter is None:
                limiter = RateLimiter(max_calls, period)
                self._limiters[name] = limiter
            else:
                limiter.max_calls = max(1, int(max_calls))
                limiter.period = max(0.001, float(period))
            return limiter

    def get_async(self, name: str, max_calls: int, period: float) -> AsyncRateLimiter:
        with self._lock:
            limiter = self._async.get(name)
            if limiter is None or limiter.max_calls != max_calls:
                limiter = AsyncRateLimiter(max_calls, period)
                self._async[name] = limiter
            return limiter
