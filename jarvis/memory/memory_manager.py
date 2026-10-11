"""The Smart Memory Manager — JARVIS's single memory interface.

Five tiers, one API:

===========  ==========================================================
working      live conversation window (``ShortTermMemory``)
episodic     turns + tool outcomes, with importance scoring
semantic     distilled facts (profile, knowledge base, learned truths)
procedural   skills / recipes (``SkillLibrary``)
session      durable transcript per conversation (``SessionStore``)
===========  ==========================================================

What makes it "smart" rather than a vector bucket:

* **Importance scoring** — every write is scored (length, specificity,
  imperatives, novelty, whether the operator asked to remember) so trivia does
  not crowd out substance.
* **Dedupe + reinforce** — near-identical memories are merged and their
  importance is bumped instead of duplicated.
* **Hybrid recall** — vector similarity fused with BM25 and *recency/importance
  priors*, packed into an explicit token budget.
* **Consolidation** — periodically rolls old episodes into digests and promotes
  repeated facts to the semantic tier (episodic → semantic promotion).
* **Decay + prune** — unused memories fade (configurable half-life) and the
  store self-limits, so it stays fast on a 2-core CPU forever.
* **Budget awareness** — recall never returns more than the context manager's
  memory budget, so memory can never be the reason a small model overflows.
"""

from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..persona import OPERATOR, profile_as_knowledge_chunks
from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from ..utils.token_counter import TokenCounter
from ..core.messages import ensure_messages, normalise_role, text_of
from .knowledge_base import KnowledgeBase
from .long_term import LongTermMemory, MemoryRecord, MemoryTier
from .session_store import SessionStore
from .short_term import ShortTermMemory
from .skill_library import SkillLibrary
from .user_profile import UserProfileStore

log = get_logger("memory")

__all__ = ["MemoryManager", "Recall", "get_memory_manager"]


@dataclass
class Recall:
    """Everything retrieved for one turn, already budget-packed."""

    memories: List[Tuple[MemoryRecord, float]] = field(default_factory=list)
    knowledge: str = ""
    skills: str = ""
    preferences: str = ""
    tokens_used: int = 0
    tokens_budget: int = 0

    def memory_block(self, *, max_chars: int = 2400) -> str:
        if not self.memories:
            return ""
        lines: List[str] = []
        used = 0
        for record, score in self.memories:
            tier = record.tier.value
            stamp = datetime.fromtimestamp(record.created_at, timezone.utc).strftime("%d %b")
            entry = f"- [{tier}·{stamp}·{score:.2f}] {truncate(record.content, 300)}"
            if used + len(entry) > max_chars:
                break
            lines.append(entry)
            used += len(entry)
        return "\n".join(lines)

    def is_empty(self) -> bool:
        return not (self.memories or self.knowledge or self.skills or self.preferences)

    def summary(self) -> str:
        return (f"{len(self.memories)} memories, "
                f"{len(self.knowledge)} kb chars, "
                f"{'skills' if self.skills else 'no skills'}, "
                f"{self.tokens_used}/{self.tokens_budget} tokens")


# Words that make a memory worth keeping even if it is short.
_IMPORTANT_MARKERS = (
    "remember", "always", "never", "prefer", "api key", "password", "deadline",
    "exam", "important", "jarvis", "heoster", "codeex", "my project",
    "yaad rakho", "hamesha", "kabhi", "zaroori", "mat bhoolna",
)

_ROUTINE_MARKERS = ("hi", "hello", "hey", "thanks", "thank you", "ok", "okay",
                    "hmm", "namaste", "good morning", "good night", "bye")


class MemoryManager:
    """Unified, budget-aware memory facade used by the orchestrator and tools."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *,
                 short_term: Optional[ShortTermMemory] = None,
                 long_term: Optional[LongTermMemory] = None,
                 knowledge: Optional[KnowledgeBase] = None,
                 sessions: Optional[SessionStore] = None,
                 skills: Optional[SkillLibrary] = None,
                 profile: Optional[UserProfileStore] = None):
        self.config = dict(config or {})
        mem_cfg = dict(self.config.get("memory") or {})
        self.cfg = dict(mem_cfg.get("manager") or {})

        self.counter = TokenCounter()
        self.short = short_term or ShortTermMemory(self.config, self.counter)
        self.long = long_term or LongTermMemory(self.config)
        self.knowledge = knowledge or KnowledgeBase(self.config, self.long)
        self.sessions = sessions or SessionStore(self.config)
        self.skills = skills or SkillLibrary(self.config, self.long)
        self.profile = profile or UserProfileStore(self.config, self.long)

        self.importance_threshold = float(self.cfg.get("importance_threshold", 0.35))
        self.dedupe_similarity = float(self.cfg.get("dedupe_similarity", 0.93))
        self.consolidation_interval = int(self.cfg.get("consolidation_interval", 25))
        self.max_entries = int(self.cfg.get("max_long_term_entries", 20000))
        self.decay_half_life = float(self.cfg.get("decay_half_life_days", 45))
        self.recall_k = int(self.cfg.get("recall_k", 6))
        self.hybrid = bool(self.cfg.get("hybrid_search", True))
        self.session_digest = bool(self.cfg.get("session_digest", True))

        memory_budget = ((self.config.get("context") or {}).get("memory_budget", 3500))
        self.memory_token_budget = int(memory_budget)

        self._lock = threading.RLock()
        self._turns_since_consolidation = 0
        self._seeded = False
        self._trace: List[Dict[str, Any]] = []
        log.info(f"memory manager ready: long-term={self.long.count()} records, "
                 f"kb={len(self.knowledge.discover())} docs, "
                 f"skills={len(self.skills.skills)}")

    # ── startup ────────────────────────────────────────────────────────────
    def bootstrap(self, *, ingest_knowledge: bool = True,
                  resume_session: bool = False) -> Dict[str, Any]:
        """Seed operator facts, (optionally) ingest docs, open a session."""
        report: Dict[str, Any] = {}
        if not self._seeded:
            report["seeded_facts"] = self.seed_operator_profile()
            self._seeded = True
        if ingest_knowledge and self.knowledge.enabled:
            report["knowledge"] = self.knowledge.ingest()
        report["session"] = self.sessions.start(resume=resume_session).as_dict()
        return report

    def seed_operator_profile(self, *, force: bool = False) -> int:
        """Write persona facts into semantic memory once (idempotent)."""
        marker = "operator-profile-seed"
        if not force:
            existing = self.long.vector_store.all(limit=400)
            if any(r.metadata.get("seed") == marker for r in existing):
                return 0
        written = 0
        for chunk in profile_as_knowledge_chunks():
            record = self.long.store(
                chunk["content"], tier=MemoryTier.SEMANTIC, importance=0.95,
                tags=["operator", "profile"], source=chunk["source"],
                metadata={"seed": marker, "topic": chunk["topic"]},
                dedupe_threshold=0.97)
            written += int(record is not None)
        log.info(f"seeded operator profile: {written} fact(s)")
        return written

    # ── importance scoring ─────────────────────────────────────────────────
    @staticmethod
    def score_importance(user_input: str, response: str, *,
                         tool_calls: int = 0, explicit_remember: bool = False) -> float:
        """Heuristic 0–1 salience score for a completed turn."""
        text = f"{user_input} {response}".strip()
        lowered = text.lower()
        if not text:
            return 0.0
        score = 0.34

        length = len(text)
        score += min(0.18, length / 6000)

        if explicit_remember or any(m in lowered for m in _IMPORTANT_MARKERS):
            score += 0.32
        if tool_calls:
            score += min(0.14, 0.05 * tool_calls)
        if re.search(r"\b(my|mera|mere|our|hamara)\b", lowered):
            score += 0.07
        if re.search(r"\b(always|never|must|important|deadline|remember)\b", lowered):
            score += 0.08
        if any(marker in lowered for marker in _ROUTINE_MARKERS) and length < 60:
            score -= 0.22
        if "@" in text or re.search(r"\b(https?://|/[\w./-]{4,})\b", text):
            score += 0.05
        if "```" in text:
            score -= 0.04

        return max(0.0, min(1.0, round(score, 3)))

    # ── writing ────────────────────────────────────────────────────────────
    def record_turn(self, user_input: str, response: str, *,
                    tool_calls: Optional[Sequence[Mapping[str, Any]]] = None,
                    tokens_used: int = 0,
                    metadata: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Persist one complete turn across the tiers that deserve it."""
        calls = list(tool_calls or [])
        importance = self.score_importance(user_input, response,
                                           tool_calls=len(calls),
                                           explicit_remember=bool(re.search(
                                               r"(?i)\b(remember|yaad rakho|note that)\b",
                                               user_input or "")))

        self.short.add_exchange(user_input, response, tool_calls=calls)
        self.sessions.append_messages(ensure_messages(self.short.get_recent()[-2:]))
        self.sessions.bump(turns=1, tool_calls=len(calls), tokens=tokens_used)

        stored: Optional[MemoryRecord] = None
        if importance >= self.importance_threshold:
            stored = self.long.store_exchange(
                user_input, response, importance=importance,
                tags=["conversation"] + (["tools"] if calls else []),
                metadata={"tools": [c.get("name") for c in calls],
                          "tokens": tokens_used, **dict(metadata or {})},
                dedupe_threshold=self.dedupe_similarity)

        learned = self.profile.observe(user_input)
        skill = None
        if len(calls) >= 3 and stored is not None:
            skill = self.skills.learn_from_trace(user_input, calls,
                                                 success=bool(response))

        with self._lock:
            self._turns_since_consolidation += 1
            due = self._turns_since_consolidation >= self.consolidation_interval
        consolidation = self.consolidate() if due else None

        outcome = {
            "importance": importance,
            "stored": stored is not None,
            "memory_id": stored.id if stored else "",
            "profile_entries": [e.key for e in learned],
            "skill": skill.name if skill else "",
            "consolidation": consolidation,
        }
        self._trace.append({"at": time.time(), **outcome})
        if len(self._trace) > 200:
            self._trace = self._trace[-200:]
        log.debug(f"turn recorded: importance={importance} stored={stored is not None}")
        return outcome

    def remember(self, fact: str, *, importance: float = 0.8,
                 tags: Optional[Sequence[str]] = None,
                 source: str = "explicit") -> Optional[MemoryRecord]:
        """Explicit "remember this" path used by the memory tool."""
        record = self.long.store_fact(fact, importance=importance,
                                      tags=list(tags or ["explicit"]), source=source)
        if self.sessions.current is not None:
            self.sessions.current.metadata.setdefault("explicit_facts", [])
            self.sessions.current.metadata["explicit_facts"].append(truncate(fact, 200))
        return record

    def add_knowledge(self, text: str, *, source: str, topic: str = "") -> int:
        return self.knowledge.add_text(text, source=source, topic=topic)

    # ── recall ─────────────────────────────────────────────────────────────
    def recall(self, query: str, *, k: Optional[int] = None,
               token_budget: Optional[int] = None,
               include_knowledge: bool = True,
               include_skills: bool = True,
               memory_weight: float = 1.0) -> Recall:
        """Hybrid recall packed into the memory token budget."""
        budget = int(token_budget or self.memory_token_budget)
        k = int(k or self.recall_k)
        result = Recall(tokens_budget=budget)

        pairs = self.long.retrieve(query, k=max(k * 2, k)) if query else []
        # Boost by recency and importance: a 0.6-similar memory from today beats
        # a 0.62-similar memory from three months ago.
        rescored: List[Tuple[MemoryRecord, float]] = []
        now = time.time()
        for record, score in pairs:
            age_days = max(0.0, (now - record.created_at) / 86_400)
            recency = math.exp(-age_days / 60.0)
            value = (score * (0.75 + 0.25 * record.importance * 2)
                     + 0.10 * recency) * memory_weight
            rescored.append((record, value))
        rescored.sort(key=lambda pair: pair[1], reverse=True)

        used = 0
        for record, score in rescored[:k]:
            cost = self.counter.count(record.content) + 8
            if used + cost > budget * 0.75:
                break
            result.memories.append((record, score))
            used += cost
        self.long.touch([r.id for r, _s in result.memories])

        if include_knowledge:
            kb_budget = max(300, int((budget - used) * 0.6))
            kb_text = self.knowledge.query(query, k=3, budget_chars=kb_budget * 4) \
                if query else ""
            if kb_text:
                kb_tokens = self.counter.count(kb_text)
                if used + kb_tokens <= budget:
                    result.knowledge = kb_text
                    used += kb_tokens

        if include_skills:
            hints = self.skills.hints_for(query) if query else ""
            if hints:
                hint_tokens = self.counter.count(hints)
                if used + hint_tokens <= budget:
                    result.skills = hints
                    used += hint_tokens

        prefs = self.profile.preferences_block()
        if prefs:
            pref_tokens = self.counter.count(prefs)
            if used + pref_tokens <= budget:
                result.preferences = prefs
                used += pref_tokens

        result.tokens_used = used
        log.debug(f"recall({truncate(query, 40)!r}) → {result.summary()}")
        return result

    def context_blocks(self, query: str, **kwargs: Any) -> Dict[str, str]:
        """Prompt-ready blocks for the brain."""
        recall = self.recall(query, **kwargs)
        extra = ""
        if recall.preferences:
            extra = f"## OPERATOR PREFERENCES (learned)\n{recall.preferences}"
        return {
            "memory": recall.memory_block(),
            "knowledge": recall.knowledge,
            "extra": "\n\n".join(p for p in (recall.skills, extra) if p),
            "recall": recall,
        }

    def recall_text(self, query: str, *, k: int = 5, max_chars: int = 1800) -> str:
        recall = self.recall(query, k=k, include_knowledge=False, include_skills=False)
        return recall.memory_block(max_chars=max_chars)

    # ── maintenance ────────────────────────────────────────────────────────
    def consolidate(self, *, force: bool = False) -> Dict[str, Any]:
        """Digest old episodes, promote durable facts, decay and prune."""
        started = time.time()
        report: Dict[str, Any] = {"started": datetime.now(timezone.utc).isoformat()}

        evicted = self.short.pending_evicted()
        if evicted:
            transcript = "\n".join(
                f"{normalise_role(m.role)}: {truncate(text_of(m), 240)}" for m in evicted)
            digest = self._summarise_block(transcript)
            if digest:
                self.long.store(digest, tier=MemoryTier.EPISODIC, importance=0.55,
                                tags=["digest"], source="consolidation",
                                metadata={"kind": "session-digest",
                                          "covers": len(evicted)})
                report["digest"] = True
            self.short.set_summary(self.short.summary + "\n" + transcript
                                   if self.short.summary else transcript)
            report["evicted_messages"] = len(evicted)

        promoted = 0
        for record in self.long.all(tier=MemoryTier.EPISODIC, limit=400):
            if self._promotable(record):
                self.long.store(
                    self._distil(record), tier=MemoryTier.SEMANTIC,
                    importance=min(0.95, record.importance + 0.15),
                    tags=list({*record.tags, "promoted"}),
                    source=record.source or "consolidation",
                    metadata={"promoted_from": record.id},
                    dedupe_threshold=0.9)
                promoted += 1
        report["promoted"] = promoted

        report["decayed"] = self.long.decay(half_life_days=self.decay_half_life)
        report["pruned"] = self.long.prune(max_records=self.max_entries)

        with self._lock:
            self._turns_since_consolidation = 0
        report["elapsed_s"] = round(time.time() - started, 2)
        if any(report.get(key) for key in ("digest", "promoted", "pruned", "decayed")):
            log.info(f"memory consolidation: {report}")
        return report

    @staticmethod
    def _promotable(record: MemoryRecord) -> bool:
        """Repeated or strongly-worded episodes graduate to semantic memory."""
        if record.importance < 0.7:
            return False
        if record.access_count >= 2:
            return True
        return bool(re.search(r"(?i)\b(always|never|my|prefer|remember|project)\b",
                              record.content))

    @staticmethod
    def _distil(record: MemoryRecord) -> str:
        """Collapse an episode into a fact-shaped sentence."""
        text = record.content
        if text.startswith("Operator asked:"):
            parts = text.split("JARVIS answered:", 1)
            ask = parts[0].replace("Operator asked:", "").strip()
            answer = parts[1].strip() if len(parts) > 1 else ""
            answer = truncate(answer, 260)
            return f"On {truncate(ask, 200)} — outcome: {answer}"
        return truncate(re.sub(r"\s+", " ", text), 420)

    @staticmethod
    def _summarise_block(text: str, *, max_chars: int = 1400) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        keep = lines[-30:]
        body = "\n".join(keep)
        return truncate(body, max_chars)

    def end_session(self) -> Dict[str, Any]:
        """Close the session with a digest that survives into the next one."""
        digest = ""
        if self.session_digest:
            digest = self._summarise_block(self.short.transcript(limit=40))
            if digest:
                self.long.store(f"Session digest — {digest}", tier=MemoryTier.EPISODIC,
                                importance=0.6, tags=["digest", "session"],
                                source="session-end", metadata={"kind": "session-digest"})
        session = self.sessions.end(summary=digest)
        self.consolidate(force=True)
        return {"session": session.as_dict() if session else None,
                "digest_written": bool(digest)}

    # ── reporting ──────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        long_stats = self.long.stats()
        return {
            "short_term": self.short.get_stats(),
            "long_term": long_stats,
            "tiers": {tier.value: self.long.count(tier) for tier in MemoryTier},
            "knowledge": self.knowledge.stats(),
            "skills": self.skills.stats(),
            "profile": self.profile.stats(),
            "sessions": self.sessions.stats(),
            "budget": {"memory_tokens": self.memory_token_budget,
                       "importance_threshold": self.importance_threshold,
                       "dedupe": self.dedupe_similarity,
                       "consolidate_every": self.consolidation_interval},
            "recent_turns": self._trace[-5:],
        }

    def report(self) -> str:
        s = self.stats()
        tiers = " · ".join(f"{name}:{count}" for name, count in s["tiers"].items())
        return "\n".join([
            f"working      : {s['short_term']['message_count']}/"
            f"{s['short_term']['max_messages']} messages, "
            f"{s['short_term']['tokens']} tokens"
            + (" (summary active)" if s["short_term"]["has_summary"] else ""),
            f"long-term    : {s['long_term']['records']} records "
            f"({s['long_term']['size_mb']} MB) · {tiers}",
            f"embedder     : {s['long_term']['embedder']['backend']}"
            f"/{s['long_term']['embedder']['model']}",
            f"knowledge    : {s['knowledge']['documents']} docs, "
            f"{s['knowledge']['supported_files']} files",
            f"skills       : {s['skills']['skills']} "
            f"({s['skills']['macros']} macros), "
            f"{s['skills']['total_uses']} uses",
            f"profile      : {s['profile']['learned_entries']} learned entries "
            f"{s['profile']['by_kind']}",
            f"sessions     : {s['sessions']['sessions']} stored, "
            f"{s['sessions']['messages']} messages",
            f"budget       : {self.memory_token_budget} tokens for recall, "
            f"consolidate every {self.consolidation_interval} turns",
        ])

    def search(self, query: str, *, limit: int = 10) -> List[Dict[str, Any]]:
        """Search across memory tiers *and* past sessions (for ``/memory <query>``)."""
        out: List[Dict[str, Any]] = []
        for record, score in self.long.retrieve(query, k=limit):
            out.append({"where": f"memory/{record.tier.value}", "score": round(score, 3),
                        "content": truncate(record.content, 220),
                        "when": datetime.fromtimestamp(record.created_at,
                                                       timezone.utc).isoformat()[:19]})
        for row in self.sessions.search(query, limit=max(3, limit // 2)):
            out.append({"where": f"session/{row['role']}", "score": None,
                        "content": row["content"], "when": row["when"][:19]})
        out.sort(key=lambda item: (item["score"] is not None, item["score"] or 0),
                 reverse=True)
        return out[:limit]

    def close(self) -> None:
        """Release resources.

        The vector store is a process-wide singleton shared by every consumer
        (tools, agents, the orchestrator), so this only closes the session
        database — :func:`reset_memory_manager` / ``reset_vector_store`` are what
        actually tear the index down.
        """
        try:
            self.sessions.close()
        except Exception as exc:
            log.debug(f"session close failed: {exc}")


_MANAGER: Optional[MemoryManager] = None
_LOCK = threading.Lock()


def get_memory_manager(config: Optional[Mapping[str, Any]] = None, *,
                       refresh: bool = False) -> MemoryManager:
    global _MANAGER
    with _LOCK:
        if _MANAGER is None or refresh:
            if _MANAGER is not None:
                _MANAGER.close()
            if config is None:
                from ..config import load_config

                config = load_config()
            _MANAGER = MemoryManager(config)
        return _MANAGER


def reset_memory_manager(*, drop_index: bool = False) -> None:
    """Drop the singleton (used by tests and by ``--fresh`` style flags).

    ``drop_index=True`` also closes the shared vector store — do that only when
    the caller really wants the index torn down (tests, config changes).
    """
    global _MANAGER
    with _LOCK:
        if _MANAGER is not None:
            try:
                _MANAGER.close()
            except Exception:
                pass
        _MANAGER = None
    if drop_index:
        from .vector_store import reset_vector_store

        reset_vector_store()
