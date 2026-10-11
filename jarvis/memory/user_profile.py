"""Operator profile store — the learned half of "who is Heoster".

The static facts live in :mod:`jarvis.persona` and are always injected into the
system prompt.  This store captures the *dynamic* half: preferences, projects,
routines and corrections the operator reveals during use ("call me Heoster",
"I use Chrome not Firefox", "always answer in Hindi for maths").

Two APIs matter:

* :meth:`observe` — cheap extraction of candidate facts from a user message.
* :meth:`preferences_block` — the tiny block injected into every prompt.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..persona import OPERATOR, OperatorProfile
from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from .long_term import LongTermMemory

log = get_logger("profile")

__all__ = ["UserProfileStore", "ProfileEntry"]


@dataclass
class ProfileEntry:
    key: str
    value: str
    kind: str = "preference"          # preference | fact | correction | routine
    confidence: float = 0.6
    source: str = "conversation"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    seen: int = 1


_PATTERNS: List[tuple[str, str, float]] = [
    (r"\b(?:i(?:'m| am) (?:a|an|the)?\s*)([^.,;!?]{3,80})", "self_description", 0.7),
    (r"\b(?:call me|my name is|i am)\s+([A-Z][\w\s'.-]{2,40})", "name_pref", 0.9),
    (r"\b(?:i (?:prefer|like|want|use|always use))\s+([^.,;!?]{3,90})", "preference", 0.8),
    (r"\b(?:i (?:don't|do not|never) (?:like|want|use))\s+([^.,;!?]{3,90})", "aversion", 0.8),
    (r"\b(?:remember(?: that)?|note that|keep in mind(?: that)?)\s+([^.!?]{4,140})", "explicit", 0.95),
    (r"\b(?:my|mera)\s+(project|app|website|repo|company|college|school|class|exam|goal)\s*(?:is|:)?\s*([^.!?]{2,90})",
     "personal", 0.8),
    (r"\b(?:i(?:'m| am) (?:working on|building|making))\s+([^.!?]{3,90})", "project", 0.85),
    (r"\b(?:answer|reply|respond|write)\s+(?:in|me in)\s+(hindi|english|hinglish)\b", "language", 0.9),
    (r"\b(?:use|prefer)\s+(chrome|firefox|edge|brave|playwright|selenium)\b", "tool_pref", 0.7),
]

_NEGATIVE_HINTS = ("?", "what", "how", "why", "kya", "kaise", "kaun")


class UserProfileStore:
    """Persistent, mergeable operator profile."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 memory: Optional[LongTermMemory] = None, *,
                 path: Optional[str | Path] = None):
        cfg = dict(config or {})
        if "memory" in cfg and isinstance(cfg["memory"], Mapping):
            cfg = dict(cfg["memory"])
        default = Path(__file__).resolve().parents[2] / "data" / "operator_profile.json"
        self.path = Path(path or default)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.memory = memory
        self.base: OperatorProfile = OPERATOR
        self.entries: Dict[str, ProfileEntry] = {}
        self.learning_enabled = bool((cfg.get("profile") or {}).get("learning", True))
        self.load()

    # ── persistence ────────────────────────────────────────────────────────
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning(f"cannot read operator profile: {exc}")
            return
        for key, raw in (data.get("entries") or {}).items():
            try:
                self.entries[key] = ProfileEntry(**raw)
            except TypeError:
                continue
        log.debug(f"operator profile loaded ({len(self.entries)} learned entries)")

    def save(self) -> None:
        payload = {
            "base_name": self.base.name,
            "base_handle": self.base.handle,
            "updated_at": time.time(),
            "entries": {k: vars(v) for k, v in self.entries.items()},
        }
        try:
            self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.debug(f"cannot persist operator profile: {exc}")

    # ── learning ───────────────────────────────────────────────────────────
    def observe(self, text: str, *, store_to_memory: bool = True) -> List[ProfileEntry]:
        """Extract candidate facts from a user message (cheap, no LLM)."""
        if not self.learning_enabled or not text:
            return []
        lowered = text.lower().strip()
        if any(hint in lowered for hint in _NEGATIVE_HINTS) and "remember" not in lowered:
            # questions are usually not statements about the operator
            if not re.search(r"\bi (?:prefer|like|use|am)\b", lowered):
                return []

        found: List[ProfileEntry] = []
        for pattern, kind, confidence in _PATTERNS:
            for match in re.finditer(pattern, text, flags=re.I):
                groups = [g for g in match.groups() if g]
                if not groups:
                    continue
                value = " ".join(groups).strip()
                value = re.sub(r"\s+", " ", value)
                if len(value) < 3 or len(value) > 160:
                    continue
                key = self._key_for(kind, value)
                entry = self.merge(key, value, kind=kind, confidence=confidence,
                                   source="conversation")
                found.append(entry)
                if store_to_memory and self.memory is not None and confidence >= 0.85:
                    self.memory.store_fact(
                        f"Operator {kind.replace('_', ' ')}: {value}",
                        importance=0.8, tags=["profile", kind], source="profile-observer")
        if found:
            self.save()
            log.info(f"profile updated: {', '.join(e.key for e in found[:4])}")
        return found

    def _key_for(self, kind: str, value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", value.lower())[:36].strip("_")
        return f"{kind}:{slug}" if slug else kind

    def merge(self, key: str, value: str, *, kind: str = "preference",
              confidence: float = 0.6, source: str = "conversation") -> ProfileEntry:
        entry = self.entries.get(key)
        if entry:
            entry.seen += 1
            entry.confidence = min(1.0, max(entry.confidence, confidence) + 0.03)
            entry.value = value
            entry.updated_at = time.time()
        else:
            entry = ProfileEntry(key=key, value=value, kind=kind,
                                 confidence=confidence, source=source)
            self.entries[key] = entry
        return entry

    def forget(self, key: str) -> bool:
        if self.entries.pop(key, None):
            self.save()
            return True
        return False

    def top(self, kind: Optional[str] = None, limit: int = 12) -> List[ProfileEntry]:
        entries = [e for e in self.entries.values() if kind is None or e.kind == kind]
        entries.sort(key=lambda e: (e.confidence, e.seen), reverse=True)
        return entries[:limit]

    # ── prompt blocks ──────────────────────────────────────────────────────
    def preferences_block(self, *, limit: int = 10, max_chars: int = 900) -> str:
        entries = self.top(limit=limit)
        if not entries:
            return ""
        lines = [f"- {e.value}  ({e.kind}, seen {e.seen}×)" for e in entries]
        text = "\n".join(lines)
        return truncate(text, max_chars)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for entry in self.entries.values():
            out[entry.kind] = out.get(entry.kind, 0) + 1
        return out

    def stats(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "learned_entries": len(self.entries),
            "by_kind": self.counts(),
            "base_profile": {"name": self.base.name, "handle": self.base.handle,
                             "location": self.base.location},
        }
