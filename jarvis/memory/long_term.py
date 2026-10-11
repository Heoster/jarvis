"""Long-term memory — tiered, semantic, persistent.

Four tiers, mirroring how human memory is usually modelled (and how agent
frameworks get the most out of a small model):

``working``    the live conversation (delegated to ShortTermMemory)
``episodic``   "what happened" — turns, tool outcomes, session digests
``semantic``   "what is true" — distilled facts the agent should keep forever
``procedural`` "how to do it" — reusable recipes / skills

Every record carries **importance**, access counters and timestamps so the
memory manager can decay, promote (episodic → semantic) and prune.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from .embedder import get_embedder
from .vector_store import VectorRecord, VectorStore, get_vector_store

log = get_logger("memory.long")

__all__ = ["MemoryTier", "MemoryRecord", "LongTermMemory"]


class MemoryTier(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"

    @classmethod
    def coerce(cls, value: Any) -> "MemoryTier":
        if isinstance(value, cls):
            return value
        text = str(value or "episodic").lower()
        for member in cls:
            if member.value == text:
                return member
        return cls.EPISODIC


@dataclass
class MemoryRecord:
    """A single durable memory."""

    content: str
    tier: MemoryTier = MemoryTier.EPISODIC
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: str = ""
    created_at: float = field(default_factory=time.time)
    vector: Optional[List[float]] = None
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)

    def preview(self, max_chars: int = 220) -> str:
        """One-line, whitespace-collapsed view used in prompts and CLI output."""
        text = " ".join((self.content or "").split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "…"

    def age_days(self) -> float:
        return max(0.0, (time.time() - float(self.created_at or time.time())) / 86400)

    def to_vector_record(self) -> VectorRecord:
        return VectorRecord(
            id=self.id or _make_id(self.content, self.tier),
            content=self.content,
            metadata={**self.metadata, "tier": self.tier.value,
                      "source": self.source, "tags": self.tags},
            vector=self.vector,
            tier=self.tier.value,
            importance=self.importance,
            created_at=self.created_at,
            last_accessed=self.last_accessed,
            access_count=self.access_count,
            tags=list(self.tags),
            source=self.source,
        )

    @classmethod
    def from_vector_record(cls, record: VectorRecord) -> "MemoryRecord":
        return cls(
            content=record.content,
            tier=MemoryTier.coerce(record.tier),
            importance=float(record.importance or 0.5),
            tags=list(record.tags or []),
            source=record.source or str(record.metadata.get("source", "")),
            metadata={k: v for k, v in (record.metadata or {}).items()
                      if k not in ("tier", "source", "tags")},
            id=record.id,
            created_at=float(record.created_at or time.time()),
            vector=record.vector,
            access_count=int(record.access_count or 0),
            last_accessed=float(record.last_accessed or time.time()),
        )

    def age_days(self) -> float:
        return max(0.0, (time.time() - self.created_at) / 86_400)


def _make_id(content: str, tier: MemoryTier) -> str:
    digest = hashlib.blake2b(f"{tier.value}:{content}".encode("utf-8"),
                             digest_size=12).hexdigest()
    return f"{tier.value[:3]}_{digest}"


class LongTermMemory:
    """CRUD + retrieval over the vector store, expressed in memory tiers."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 store: Optional[VectorStore] = None):
        cfg = dict(config or {})
        if "memory" in cfg and isinstance(cfg["memory"], Mapping):
            cfg = dict(cfg["memory"])
        self.cfg = cfg
        # NB: named `vector_store` (not `store`) so it never shadows the
        # `store()` method that every caller uses to write memories.
        self.vector_store = store or get_vector_store(config)
        self.embedder = self.vector_store.embedder
        self._lock = threading.RLock()
        self.writes = 0
        self.duplicates_skipped = 0

    # ── writing ────────────────────────────────────────────────────────────
    def store(self, content: str, *, tier: MemoryTier | str = MemoryTier.EPISODIC,
              importance: float = 0.5, tags: Optional[Sequence[str]] = None,
              source: str = "", metadata: Optional[Mapping[str, Any]] = None,
              dedupe_threshold: float = 0.0) -> Optional[MemoryRecord]:
        """Persist a memory.  Returns ``None`` when it was a duplicate."""
        text = (content or "").strip()
        if not text:
            return None
        tier_enum = MemoryTier.coerce(tier)
        if dedupe_threshold > 0:
            existing = self.vector_store.find_similar(text, threshold=dedupe_threshold, limit=1)
            if existing:
                record, score = existing[0]
                # reinforce instead of duplicating
                new_importance = min(1.0, max(record.importance, importance) + 0.05)
                self.vector_store.set_importance(record.id, new_importance)
                self.vector_store.touch([record.id], boost=0.01)
                self.duplicates_skipped += 1
                log.debug(f"deduped memory ({score:.3f}): {truncate(text, 60)}")
                return None

        record = MemoryRecord(content=text, tier=tier_enum,
                              importance=max(0.0, min(1.0, importance)),
                              tags=list(tags or []), source=source,
                              metadata=dict(metadata or {}))
        stored = self.vector_store.add(record.to_vector_record())
        with self._lock:
            self.writes += 1
        record.id = stored.id
        return record

    def store_many(self, records: Iterable[MemoryRecord]) -> int:
        count = 0
        for record in records:
            if self.store(record.content, tier=record.tier,
                          importance=record.importance, tags=record.tags,
                          source=record.source, metadata=record.metadata):
                count += 1
        return count

    def store_exchange(self, user_input: str, agent_response: str, *,
                       importance: float = 0.5, tags: Optional[Sequence[str]] = None,
                       metadata: Optional[Mapping[str, Any]] = None,
                       dedupe_threshold: float = 0.0) -> Optional[MemoryRecord]:
        content = (f"Operator asked: {truncate(user_input.strip(), 400)}\n"
                   f"JARVIS answered: {truncate((agent_response or '').strip(), 700)}")
        return self.store(content, tier=MemoryTier.EPISODIC, importance=importance,
                          tags=list(tags or ["conversation"]), source="session",
                          metadata=dict(metadata or {}), dedupe_threshold=dedupe_threshold)

    def store_fact(self, fact: str, *, importance: float = 0.85,
                   tags: Optional[Sequence[str]] = None, source: str = "inference",
                   metadata: Optional[Mapping[str, Any]] = None) -> Optional[MemoryRecord]:
        return self.store(fact, tier=MemoryTier.SEMANTIC, importance=importance,
                          tags=list(tags or ["fact"]), source=source,
                          metadata=dict(metadata or {}), dedupe_threshold=0.9)

    def store_procedure(self, name: str, steps: Sequence[str], *,
                        when_to_use: str = "", importance: float = 0.9,
                        tags: Optional[Sequence[str]] = None) -> Optional[MemoryRecord]:
        body = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
        content = f"Procedure '{name}'" + (f" — use when {when_to_use}" if when_to_use else "")
        content += f"\n{body}"
        return self.store(content, tier=MemoryTier.PROCEDURAL, importance=importance,
                          tags=list(tags or ["procedure", name.lower()]),
                          source="skill-library", dedupe_threshold=0.92)

    def store_knowledge(self, content: str, *, source: str = "knowledge",
                        topic: str = "", chunk_index: int = 0) -> Optional[MemoryRecord]:
        return self.store(content, tier=MemoryTier.SEMANTIC, importance=0.6,
                          tags=["knowledge", topic] if topic else ["knowledge"],
                          source=source,
                          metadata={"kind": "knowledge", "topic": topic,
                                    "chunk": chunk_index},
                          dedupe_threshold=0.97)

    # ── reading ────────────────────────────────────────────────────────────
    def retrieve(self, query: str, k: int = 5, *, tier: Optional[MemoryTier | str] = None,
                 min_score: float = 0.0,
                 metadata_filter: Optional[Mapping[str, Any]] = None
                 ) -> List[Tuple[MemoryRecord, float]]:
        """Hybrid retrieve: vector similarity + BM25, blended.

        Fusion is a weighted sum (0.65 semantic / 0.35 lexical) rather than RRF
        because we need *ranks we can threshold* — the manager uses the score to
        decide what is worth injecting into a 3 500-token memory budget.
        """
        tier_value = MemoryTier.coerce(tier).value if tier else None
        semantic = self.vector_store.search_vector(query, k=max(k * 3, k), tier=tier_value,
                                            metadata_filter=metadata_filter,
                                            min_score=min_score)
        lexical = self.vector_store.search_keywords(query, k=max(k * 3, k), tier=tier_value,
                                             metadata_filter=metadata_filter)
        lexical_max = max((s for _r, s in lexical), default=1.0) or 1.0

        blended: Dict[str, Tuple[MemoryRecord, float, VectorRecord]] = {}
        for record, score in semantic:
            key = record.id
            blended[key] = (MemoryRecord.from_vector_record(record),
                            0.65 * max(0.0, score), record)
        for record, score in lexical:
            norm = score / lexical_max
            key = record.id
            if key in blended:
                memory, current, vec = blended[key]
                blended[key] = (memory, current + 0.35 * norm, vec)
            else:
                blended[key] = (MemoryRecord.from_vector_record(record),
                                0.35 * norm, record)

        ranked = sorted(blended.values(), key=lambda item: item[1], reverse=True)[:k]
        return [(memory, score) for memory, score, _vec in ranked]

    def semantic(self, query: str, k: int = 5, **kwargs: Any) -> List[Tuple[MemoryRecord, float]]:
        return self.retrieve(query, k=k, tier=MemoryTier.SEMANTIC, **kwargs)

    def procedures(self, query: str = "", k: int = 3) -> List[MemoryRecord]:
        if not query:
            return [MemoryRecord.from_vector_record(r)
                    for r in self.vector_store.all(tier=MemoryTier.PROCEDURAL.value, limit=k)]
        pairs = self.retrieve(query, k=k, tier=MemoryTier.PROCEDURAL)
        return [memory for memory, _score in pairs]

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        record = self.vector_store.get(record_id)
        return MemoryRecord.from_vector_record(record) if record else None

    def all(self, *, tier: Optional[MemoryTier | str] = None, limit: int = 500) -> List[MemoryRecord]:
        tier_value = MemoryTier.coerce(tier).value if tier else None
        return [MemoryRecord.from_vector_record(r)
                for r in self.vector_store.all(tier=tier_value, limit=limit)]

    def count(self, tier: Optional[MemoryTier | str] = None) -> int:
        if tier is None:
            return int(self.vector_store.count())
        return len(self.vector_store.all(tier=MemoryTier.coerce(tier).value, limit=100_000))

    def forget(self, record_id: str) -> bool:
        return bool(self.vector_store.delete(record_id))

    def touch(self, ids: Iterable[str], *, boost: float = 0.02) -> None:
        self.vector_store.touch(list(ids), boost=boost)

    # ── maintenance ────────────────────────────────────────────────────────
    def prune(self, *, max_records: int = 20000) -> int:
        return int(self.vector_store.prune(max_records=max_records))

    def decay(self, *, half_life_days: float = 45.0, floor: float = 0.05) -> int:
        """Reduce importance of stale, unused memories (never below ``floor``)."""
        adjusted = 0
        for record in self.vector_store.all(limit=20_000):
            if record.tier in (MemoryTier.PROCEDURAL.value,):
                continue
            age = max(0.0, (time.time() - record.created_at) / 86_400)
            accesses = 1.0 + min(3.0, record.access_count / 5.0)
            decayed = record.importance * (0.5 ** (age / max(1.0, half_life_days))) * (0.6 + 0.4 * accesses)
            decayed = max(floor, min(1.0, round(decayed, 4)))
            if abs(decayed - record.importance) > 0.01:
                self.vector_store.set_importance(record.id, decayed)
                adjusted += 1
        if adjusted:
            log.debug(f"decayed {adjusted} memories")
        return adjusted

    def stats(self) -> Dict[str, Any]:
        base = self.vector_store.stats()
        base.update({"writes": self.writes, "duplicates_skipped": self.duplicates_skipped})
        return base
