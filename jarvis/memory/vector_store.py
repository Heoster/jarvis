"""Vector + keyword store backed by SQLite (ChromaDB optional).

Why SQLite by default?  Because ChromaDB drags in ~200 MB of transitive
dependencies (onnxruntime, tokenizers, duckdb…) which is exactly the kind of
thing a CPU-first, zero-cost agent should avoid.  The built-in store gives:

* float32 vectors stored as compact BLOBs with pure-Python cosine scoring;
* a real inverted index for **BM25 keyword search** (crucial: embeddings are
  fuzzy, but "GROQ_API_KEY" or a file path must match *exactly*);
* metadata filtering, importance/recency bookkeeping and access counters —
  the inputs the smart memory manager needs for decay and consolidation.

ChromaDB is still supported: set ``memory.long_term.backend: chromadb``.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from .embedder import Embedder, get_embedder, tokenize

log = get_logger("vectors")

__all__ = ["VectorRecord", "VectorStore", "get_vector_store", "SqliteVectorStore"]


def _now() -> float:
    return time.time()


@dataclass
class VectorRecord:
    """One memory entry."""

    id: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    vector: Optional[List[float]] = None
    tier: str = "episodic"
    importance: float = 0.5
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    last_accessed: float = field(default_factory=_now)
    access_count: int = 0
    tags: List[str] = field(default_factory=list)
    source: str = ""

    def to_row(self) -> Tuple[Any, ...]:
        return (
            self.id, self.content, json.dumps(self.metadata, ensure_ascii=False),
            _pack(self.vector), len(self.vector or []), self.created_at,
            self.updated_at, self.last_accessed, int(self.access_count),
            float(self.importance), self.tier, json.dumps(self.tags),
            self.source,
        )

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "VectorRecord":
        (rid, content, meta, blob, _dim, created, updated, accessed, count,
         importance, tier, tags, source) = row
        return cls(
            id=rid, content=content,
            metadata=json.loads(meta) if meta else {},
            vector=_unpack(blob),
            tier=tier or "episodic", importance=float(importance or 0.5),
            created_at=float(created), updated_at=float(updated),
            last_accessed=float(accessed), access_count=int(count or 0),
            tags=json.loads(tags) if tags else [], source=source or "",
        )

    def preview(self, limit: int = 140) -> str:
        return truncate(re.sub(r"\s+", " ", self.content), limit)


def _pack(vector: Optional[Sequence[float]]) -> Optional[bytes]:
    if not vector:
        return None
    return struct.pack(f"<{len(vector)}f", *[float(v) for v in vector])


def _unpack(blob: Optional[bytes]) -> Optional[List[float]]:
    if not blob:
        return None
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = na = nb = 0.0
    for i in range(n):
        x, y = a[i], b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class SqliteVectorStore:
    """Default store: SQLite + BLOBs + inverted index for BM25."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 embedder: Optional[Embedder] = None, *, path: Optional[str | Path] = None):
        cfg: Mapping[str, Any] = config or {}
        lt: Mapping[str, Any] = {}
        if "memory" in cfg and isinstance(cfg["memory"], Mapping):
            mem = cfg["memory"]
            if isinstance(mem.get("long_term"), Mapping):
                lt = mem["long_term"]
        directory = Path(path or lt.get("persist_directory")
                         or (Path(__file__).resolve().parents[2] / "memory_store"))
        directory.mkdir(parents=True, exist_ok=True)
        self.db_path = directory / "vectors.sqlite3"
        self.collection = str(lt.get("collection_name", "jarvis_memory"))
        self.embedder = embedder or get_embedder(config)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        log.info(f"vector store: sqlite at {self.db_path} "
                 f"({self.count()} records, backend={self.embedder.active_backend()})")

    # ── schema ─────────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    metadata TEXT,
                    vector BLOB,
                    dim INTEGER,
                    created_at REAL,
                    updated_at REAL,
                    last_accessed REAL,
                    access_count INTEGER DEFAULT 0,
                    importance REAL DEFAULT 0.5,
                    tier TEXT DEFAULT 'episodic',
                    tags TEXT,
                    source TEXT
                )
                """
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS terms ("
                "  term TEXT NOT NULL, doc_id TEXT NOT NULL, tf INTEGER NOT NULL,"
                "  PRIMARY KEY (term, doc_id))"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_terms_doc ON terms(doc_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_vec_tier ON vectors(tier)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vec_created ON vectors(created_at)")

    # ── writing ────────────────────────────────────────────────────────────
    def add(self, record: VectorRecord, *, embed: bool = True) -> VectorRecord:
        if not record.id:
            record.id = uuid.uuid4().hex[:16]
        if embed and record.vector is None:
            record.vector = self.embedder.embed(record.content)
        record.updated_at = _now()
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO vectors (id, content, metadata, vector, dim, created_at,
                        updated_at, last_accessed, access_count, importance, tier, tags, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        content=excluded.content, metadata=excluded.metadata,
                        vector=excluded.vector, dim=excluded.dim,
                        updated_at=excluded.updated_at, importance=excluded.importance,
                        tier=excluded.tier, tags=excluded.tags, source=excluded.source
                    """,
                    record.to_row(),
                )
                self._index_terms(record)
        return record

    def add_many(self, records: Sequence[VectorRecord]) -> int:
        count = 0
        for record in records:
            self.add(record)
            count += 1
        return count

    def _index_terms(self, record: VectorRecord) -> None:
        self._conn.execute("DELETE FROM terms WHERE doc_id=?", (record.id,))
        tokens = [t for t in tokenize(record.content) if len(t) > 1]
        counts: Dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        if counts:
            self._conn.executemany(
                "INSERT OR REPLACE INTO terms (term, doc_id, tf) VALUES (?,?,?)",
                [(term, record.id, tf) for term, tf in counts.items()],
            )

    def delete(self, record_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM vectors WHERE id=?", (record_id,))
            self._conn.execute("DELETE FROM terms WHERE doc_id=?", (record_id,))
        return cur.rowcount > 0

    def delete_where(self, **metadata_eq: Any) -> int:
        records = self.all(limit=100_000)
        removed = 0
        for record in records:
            if all(record.metadata.get(k) == v for k, v in metadata_eq.items()):
                removed += int(self.delete(record.id))
        return removed

    def update_content(self, record_id: str, content: str, *,
                       metadata: Optional[Dict[str, Any]] = None) -> bool:
        record = self.get(record_id)
        if record is None:
            return False
        record.content = content
        if metadata:
            record.metadata.update(metadata)
        record.vector = self.embedder.embed(content)
        self.add(record)
        return True

    def touch(self, record_ids: Iterable[str], *, boost: float = 0.02) -> None:
        """Record an access: bump counters and nudge importance up."""
        ids = list(record_ids)
        if not ids:
            return
        now = _now()
        with self._lock, self._conn:
            for rid in ids:
                self._conn.execute(
                    "UPDATE vectors SET last_accessed=?, access_count=access_count+1,"
                    " importance=MIN(1.0, importance+?) WHERE id=?",
                    (now, boost, rid),
                )

    def set_importance(self, record_id: str, value: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE vectors SET importance=? WHERE id=?",
                               (max(0.0, min(1.0, value)), record_id))

    # ── reading ────────────────────────────────────────────────────────────
    def get(self, record_id: str) -> Optional[VectorRecord]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM vectors WHERE id=?", (record_id,))
            row = cur.fetchone()
        return VectorRecord.from_row(tuple(row)) if row else None

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM vectors")
            return int(cur.fetchone()[0])

    def all(self, *, tier: Optional[str] = None, limit: int = 1000,
            order: str = "created_at DESC") -> List[VectorRecord]:
        query = "SELECT * FROM vectors"
        params: List[Any] = []
        if tier:
            query += " WHERE tier=?"
            params.append(tier)
        query += f" ORDER BY {order} LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [VectorRecord.from_row(tuple(r)) for r in rows]

    def search_vector(self, query: str | Sequence[float], k: int = 5, *,
                      tier: Optional[str] = None,
                      metadata_filter: Optional[Mapping[str, Any]] = None,
                      min_score: float = -1.0) -> List[Tuple[VectorRecord, float]]:
        vector = list(query) if not isinstance(query, str) else self.embedder.embed(query)
        sql = "SELECT * FROM vectors WHERE vector IS NOT NULL"
        params: List[Any] = []
        if tier:
            sql += " AND tier=?"
            params.append(tier)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        scored: List[Tuple[VectorRecord, float]] = []
        for row in rows:
            record = VectorRecord.from_row(tuple(row))
            if metadata_filter and not all(
                    record.metadata.get(k) == v for k, v in metadata_filter.items()):
                continue
            score = _cosine(vector, record.vector or [])
            if score >= min_score:
                scored.append((record, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def search_keywords(self, query: str, k: int = 5, *,
                        tier: Optional[str] = None,
                        metadata_filter: Optional[Mapping[str, Any]] = None) -> List[Tuple[VectorRecord, float]]:
        """BM25 over the inverted index — the exact-match workhorse."""
        terms = [t for t in dict.fromkeys(tokenize(query)) if len(t) > 1]
        if not terms:
            return []
        with self._lock:
            total_docs = max(1, int(self._conn.execute(
                "SELECT COUNT(*) FROM vectors").fetchone()[0]))
        placeholders = ",".join("?" for _ in terms)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT term, doc_id, tf FROM terms WHERE term IN ({placeholders})",
                terms,
            ).fetchall()
        if not rows:
            return []

        postings: Dict[str, List[Tuple[str, int]]] = {}
        for term, doc_id, tf in rows:
            postings.setdefault(term, []).append((doc_id, int(tf)))

        k1, b = 1.5, 0.75
        scores: Dict[str, float] = {}
        with self._lock:
            lengths = {
                r[0]: int(r[1]) for r in self._conn.execute(
                    "SELECT doc_id, SUM(tf) FROM terms GROUP BY doc_id").fetchall()
            }
        avg_len = (sum(lengths.values()) / max(1, len(lengths))) or 1.0
        for term, entries in postings.items():
            df = len(entries)
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            for doc_id, tf in entries:
                dl = lengths.get(doc_id, 1)
                denom = tf + k1 * (1 - b + b * dl / avg_len)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (k1 + 1)) / (denom or 1)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[: max(k * 3, k)]
        out: List[Tuple[VectorRecord, float]] = []
        for doc_id, score in ranked:
            record = self.get(doc_id)
            if record is None:
                continue
            if tier and record.tier != tier:
                continue
            if metadata_filter and not all(
                    record.metadata.get(k2) == v for k2, v in metadata_filter.items()):
                continue
            out.append((record, score))
            if len(out) >= k:
                break
        return out

    def find_similar(self, text: str, *, threshold: float = 0.93,
                     limit: int = 5) -> List[Tuple[VectorRecord, float]]:
        """Near-duplicate detection used before writing."""
        return [pair for pair in self.search_vector(text, k=limit, min_score=threshold)]

    # ── maintenance ────────────────────────────────────────────────────────
    def prune(self, *, max_records: int, protect_tiers: Sequence[str] = ("semantic", "procedural")) -> int:
        """Drop the least valuable records once the store exceeds ``max_records``."""
        total = self.count()
        if total <= max_records:
            return 0
        records = self.all(limit=100_000)
        def value(record: VectorRecord) -> float:
            age_days = max(0.0, (_now() - record.created_at) / 86_400)
            return (record.importance * 2.0 + min(1.0, record.access_count / 10.0)
                    - age_days / 365.0)
        candidates = [r for r in records if r.tier not in protect_tiers]
        candidates.sort(key=value)
        to_remove = total - max_records
        removed = 0
        for record in candidates[:to_remove]:
            removed += int(self.delete(record.id))
        if removed:
            log.info(f"pruned {removed} low-value memories (store now {self.count()})")
        return removed

    def vacuum(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("VACUUM")

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            tiers = {row[0]: int(row[1]) for row in self._conn.execute(
                "SELECT tier, COUNT(*) FROM vectors GROUP BY tier").fetchall()}
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {"backend": "sqlite_vector", "path": str(self.db_path),
                "records": self.count(), "tiers": tiers,
                "size_mb": round(size / 1e6, 2),
                "embedder": self.embedder.stats()}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


class ChromaVectorStore:
    """Optional ChromaDB backend (better ANN, heavier install)."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 embedder: Optional[Embedder] = None):
        import chromadb  # noqa: WPS433 - optional dependency
        from chromadb.config import Settings

        cfg = config or {}
        lt = (cfg.get("memory", {}) or {}).get("long_term", {}) if isinstance(cfg.get("memory"), Mapping) else {}
        directory = str(lt.get("persist_directory")
                        or (Path(__file__).resolve().parents[2] / "memory_store"))
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.embedder = embedder or get_embedder(config)
        self.client = chromadb.PersistentClient(
            path=directory, settings=Settings(anonymized_telemetry=False))
        self.collection = self.client.get_or_create_collection(
            name=str(lt.get("collection_name", "jarvis_memory")),
            metadata={"hnsw:space": "cosine"})
        log.info(f"vector store: chromadb ({self.collection.count()} records)")

    # A pragmatic subset of the SQLite API — the memory manager only needs these.
    def count(self) -> int:
        return int(self.collection.count())

    def add(self, record: VectorRecord, *, embed: bool = True) -> VectorRecord:
        self.collection.upsert(
            ids=[record.id],
            documents=[record.content],
            embeddings=[record.vector] if record.vector else None,
            metadatas=[{**record.metadata, "tier": record.tier,
                        "importance": record.importance, "tags": ",".join(record.tags),
                        "source": record.source}],
        )
        return record

    def add_many(self, records: Sequence[VectorRecord]) -> int:
        for record in records:
            self.add(record)
        return len(records)

    def get(self, record_id: str) -> Optional[VectorRecord]:
        data = self.collection.get(ids=[record_id])
        if not data["ids"]:
            return None
        meta = (data.get("metadatas") or [{}])[0] or {}
        return VectorRecord(id=record_id, content=(data.get("documents") or [""])[0],
                            metadata=dict(meta), tier=str(meta.get("tier", "episodic")),
                            importance=float(meta.get("importance", 0.5)),
                            source=str(meta.get("source", "")))

    def all(self, *, tier: Optional[str] = None, limit: int = 1000,
            order: str = "created_at DESC") -> List[VectorRecord]:
        where = {"tier": tier} if tier else None
        data = self.collection.get(where=where, limit=limit)
        out: List[VectorRecord] = []
        for i, rid in enumerate(data.get("ids") or []):
            meta = (data.get("metadatas") or [{}])[i] or {}
            out.append(VectorRecord(
                id=rid, content=(data.get("documents") or [""])[i],
                metadata=dict(meta), tier=str(meta.get("tier", "episodic")),
                importance=float(meta.get("importance", 0.5))))
        return out

    def search_vector(self, query: str | Sequence[float], k: int = 5, *,
                      tier: Optional[str] = None,
                      metadata_filter: Optional[Mapping[str, Any]] = None,
                      min_score: float = -1.0) -> List[Tuple[VectorRecord, float]]:
        where: Dict[str, Any] = dict(metadata_filter or {})
        if tier:
            where["tier"] = tier
        result = self.collection.query(
            query_texts=[query] if isinstance(query, str) else None,
            query_embeddings=None if isinstance(query, str) else [list(query)],
            n_results=max(1, k), where=where or None)
        out: List[Tuple[VectorRecord, float]] = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0] or [0.0] * len(ids)
        for i, rid in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            score = 1.0 - float(dists[i]) if i < len(dists) else 0.0
            if score < min_score:
                continue
            out.append((VectorRecord(id=rid, content=docs[i] if i < len(docs) else "",
                                     metadata=dict(meta or {}),
                                     tier=str((meta or {}).get("tier", "episodic")),
                                     importance=float((meta or {}).get("importance", 0.5))),
                        score))
        return out

    def search_keywords(self, query: str, k: int = 5, **kwargs: Any) -> List[Tuple[VectorRecord, float]]:
        # Chroma has no built-in BM25 → approximate with a vector query, but
        # prefer substring hits found in a small scan of recent records.
        needle = query.lower().strip()
        if needle:
            hits: List[Tuple[VectorRecord, float]] = []
            for record in self.all(limit=500):
                if needle in record.content.lower():
                    hits.append((record, 1.0))
                    if len(hits) >= k:
                        return hits
            if hits:
                return hits
        return self.search_vector(query, k=k)

    def find_similar(self, text: str, *, threshold: float = 0.93,
                     limit: int = 5) -> List[Tuple[VectorRecord, float]]:
        return [p for p in self.search_vector(text, k=limit, min_score=threshold)]

    def touch(self, record_ids: Iterable[str], *, boost: float = 0.02) -> None:
        for rid in record_ids:
            record = self.get(rid)
            if record:
                record.importance = min(1.0, record.importance + boost)
                self.add(record)

    def delete(self, record_id: str) -> bool:
        self.collection.delete(ids=[record_id])
        return True

    def prune(self, *, max_records: int, protect_tiers: Sequence[str] = ("semantic", "procedural")) -> int:
        total = self.count()
        if total <= max_records:
            return 0
        records = sorted(self.all(limit=100_000), key=lambda r: r.importance)
        removed = 0
        for record in records[: total - max_records]:
            if record.tier in protect_tiers:
                continue
            self.delete(record.id)
            removed += 1
        return removed

    def stats(self) -> Dict[str, Any]:
        return {"backend": "chromadb", "records": self.count(),
                "embedder": self.embedder.stats()}

    def vacuum(self) -> None:
        return None

    def close(self) -> None:
        return None


class VectorStore:
    """Facade choosing between SQLite and ChromaDB per configuration."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 embedder: Optional[Embedder] = None):
        cfg = config or {}
        backend = "auto"
        if isinstance(cfg.get("memory"), Mapping):
            lt = cfg["memory"].get("long_term") or {}
            backend = str(lt.get("backend", "auto")).lower()
        self.backend_requested = backend
        self.impl: Any
        if backend in ("chromadb", "chroma"):
            try:
                self.impl = ChromaVectorStore(cfg, embedder)
            except Exception as exc:
                log.warning(f"chromadb requested but unusable ({exc}); using sqlite")
                self.impl = SqliteVectorStore(cfg, embedder)
        elif backend == "auto":
            # Chroma only when explicitly installed *and* configured, never by surprise
            self.impl = SqliteVectorStore(cfg, embedder)
        else:
            self.impl = SqliteVectorStore(cfg, embedder)

    # ── delegation ─────────────────────────────────────────────────────────
    def __getattr__(self, item: str) -> Any:
        return getattr(self.impl, item)

    @property
    def embedder(self) -> Embedder:
        return self.impl.embedder

    def stats(self) -> Dict[str, Any]:
        stats = self.impl.stats()
        stats["requested_backend"] = self.backend_requested
        return stats


_STORE: Optional[VectorStore] = None
_STORE_LOCK = threading.Lock()


def get_vector_store(config: Optional[Mapping[str, Any]] = None, *,
                     refresh: bool = False) -> VectorStore:
    """Process-wide vector store (the memory subsystem's single index).

    ``refresh=True`` builds a new store, which matters in tests and when the
    config (and therefore the database path) changes at runtime.
    """
    global _STORE
    with _STORE_LOCK:
        if _STORE is None or refresh:
            if _STORE is not None:
                try:
                    _STORE.close()
                except Exception:
                    pass
            _STORE = VectorStore(config)
        return _STORE


def reset_vector_store() -> None:
    """Close and forget the singleton (used by tests / memory reset)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            try:
                _STORE.close()
            except Exception:
                pass
        _STORE = None
