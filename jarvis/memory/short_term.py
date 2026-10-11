"""Short-term (working) memory — the sliding window of the live conversation.

Keeps the last N exchanges in order, tracks a rolling summary of everything that
scrolled out, and exposes token accounting so the context manager knows how much
room is left.  Nothing here touches disk: sessions persist through
:class:`~jarvis.memory.session_store.SessionStore` instead.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..utils.logger import get_logger
from ..utils.token_counter import TokenCounter
from ..core.messages import (
    AIMessage, HumanMessage, Message, SystemMessage, ensure_message,
    ensure_messages, normalise_role, text_of,
)

log = get_logger("memory.short")

__all__ = ["ShortTermMemory"]


class ShortTermMemory:
    """Sliding-window buffer with automatic summarisation of evicted turns."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 counter: Optional[TokenCounter] = None):
        cfg = dict(config or {})
        if "memory" in cfg and isinstance(cfg["memory"], Mapping):
            cfg = dict(cfg["memory"].get("short_term") or {})
        self.max_messages = int(cfg.get("max_messages", 24))
        self.summarize_after = int(cfg.get("summarize_after", 14))
        self.max_tokens = int(cfg.get("max_tokens", 6000))

        self.counter = counter or TokenCounter()
        self._messages: deque[Message] = deque(maxlen=self.max_messages)
        self._evicted: List[Message] = []
        self._summary: str = ""
        self._lock = threading.RLock()

    # ── writing ────────────────────────────────────────────────────────────
    def add(self, message: Any) -> Message:
        msg = ensure_message(message)
        with self._lock:
            if len(self._messages) == self.max_messages and self._messages:
                self._evicted.append(self._messages[0])
                if len(self._evicted) > 200:
                    self._evicted = self._evicted[-200:]
            self._messages.append(msg)
        return msg

    def add_many(self, messages: Iterable[Any]) -> None:
        for msg in messages:
            self.add(msg)

    def add_exchange(self, user_input: str, assistant_output: str, *,
                     tool_calls: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        self.add(HumanMessage(content=user_input))
        self.add(AIMessage(content=assistant_output or "", tool_calls=list(tool_calls or [])))

    def replace(self, messages: Iterable[Any]) -> None:
        with self._lock:
            self._messages = deque(ensure_messages(messages), maxlen=self.max_messages)

    # ── reading ────────────────────────────────────────────────────────────
    def get_recent(self, n: Optional[int] = None, *, include_summary: bool = True) -> List[Message]:
        with self._lock:
            messages = list(self._messages)
        if n:
            messages = messages[-n:]
        if include_summary and self._summary:
            return [SystemMessage(content=f"[Conversation so far] {self._summary}")] + messages
        return messages

    def last_user_message(self) -> str:
        with self._lock:
            for msg in reversed(self._messages):
                if normalise_role(msg.role) == "human":
                    return msg.content
        return ""

    def last_assistant_message(self) -> str:
        with self._lock:
            for msg in reversed(self._messages):
                if normalise_role(msg.role) == "ai" and msg.content:
                    return msg.content
        return ""

    def needs_summary(self) -> bool:
        with self._lock:
            return len(self._evicted) >= 4 or (
                len(self._messages) >= self.summarize_after and bool(self._evicted))

    def token_count(self) -> int:
        return self.counter.count_messages(self.get_recent())

    # ── summary handling ───────────────────────────────────────────────────
    def set_summary(self, summary: str) -> None:
        with self._lock:
            self._summary = summary or ""
            self._evicted.clear()

    @property
    def summary(self) -> str:
        return self._summary

    def pending_evicted(self) -> List[Message]:
        with self._lock:
            return list(self._evicted)

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._evicted.clear()
            self._summary = ""

    # ── reporting ──────────────────────────────────────────────────────────
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            count = len(self._messages)
        return {
            "message_count": count,
            "max_messages": self.max_messages,
            "has_summary": bool(self._summary),
            "summary_chars": len(self._summary),
            "tokens": self.token_count(),
            "evicted_pending": len(self._evicted),
        }

    def transcript(self, limit: Optional[int] = None) -> str:
        messages = self.get_recent()
        if limit:
            messages = messages[-limit:]
        return "\n".join(f"{m.role}: {text_of(m)}" for m in messages)
