"""Embeddings that always work.

Order of preference:

1. **Ollama** ``nomic-embed-text`` — real embeddings, local, free.  Used when
   ``ollama serve`` is running and the model is pulled.
2. **LocalHashingEmbedder** — a dependency-free, deterministic bag-of-features
   embedder (words + character trigrams, sublinear TF, L2-normalised, signed
   hashing into ``embedding_dim`` buckets).  It is not a neural net, but it is
   *good enough for recall* — especially because the memory manager fuses it
   with BM25 keyword search, which is where exact-name recall comes from.

This matters: JARVIS must never depend on a paid embedding API, and must keep
working when the machine is offline mid-session.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..utils.logger import get_logger

log = get_logger("embed")

__all__ = ["Embedder", "LocalHashingEmbedder", "OllamaEmbedder", "get_embedder",
           "EMBEDDING_DIM", "tokenize"]

EMBEDDING_DIM = 384

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "for", "with", "by", "from", "as", "it", "its",
    "i", "you", "he", "she", "we", "they", "me", "my", "your", "his", "her",
    "our", "their", "do", "does", "did", "have", "has", "had", "will", "would",
    "can", "could", "should", "may", "might", "must", "not", "no", "yes",
    "so", "very", "just", "about", "into", "over", "after", "before", "also",
    "there", "here", "what", "which", "who", "when", "where", "how", "why",
}

_WORD_RE = re.compile(r"[a-z0-9_]+|[\u0900-\u097f]+")


def tokenize(text: str) -> List[str]:
    """Lowercase word tokens; keeps code identifiers and Devanagari."""
    return _WORD_RE.findall((text or "").lower())


def stem(word: str) -> str:
    """Very light English stemmer — enough that "lives" ≈ "live", "tiers" ≈ "tier".

    Deliberately conservative: the offline embedder is lexical, so over-stemming
    would hurt more than it helps.  Word forms that do not clearly reduce are
    returned untouched, and code identifiers (digits/underscores) never change.
    """
    if not word or len(word) < 4 or any(ch.isdigit() or ch == "_" for ch in word):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "ches", "shes", "xes", "zes")):
        return word[:-2]
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("edly") and len(word) > 6:
        return word[:-4]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]
    if word.endswith("ly") and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def stem_tokens(tokens: Sequence[str]) -> List[str]:
    return [stem(token) for token in tokens]


def _sign_hash(token: str, seed: int = 0) -> tuple[int, float]:
    digest = hashlib.blake2b(f"{seed}:{token}".encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % EMBEDDING_DIM
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


class LocalHashingEmbedder:
    """Offline embedder: hashed word + trigram features, L2-normalised."""

    name = "local"
    model = "hashing-tfidf-v1"

    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim
        self._idf_cache: Dict[str, float] = {}

    # ── vector maths ───────────────────────────────────────────────────────
    def embed(self, text: str) -> List[float]:
        vector = [0.0] * self.dim
        tokens = [t for t in tokenize(text) if t not in _STOPWORDS and len(t) > 1]
        if not tokens:
            tokens = tokenize(text) or ["<empty>"]

        # index both the surface form and its stem: exact matches score highest
        # while morphology ("lives" ↔ "live") still retrieves.
        weighted: Counter = Counter()
        for token, count in Counter(tokens).items():
            weight = 1.0 + math.log(count)            # sublinear TF
            weighted[token] += weight
            root = stem(token)
            if root != token:
                weighted[root] += weight * 0.9

        for token, weight in weighted.items():
            self._add_feature(vector, token, weight)
            if len(token) > 4:                        # partial-match help
                for gram in _trigrams(token):
                    self._add_feature(vector, f"#{gram}", weight * 0.35)

        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def _add_feature(self, vector: List[float], token: str, weight: float) -> None:
        index, sign = _sign_hash(token, seed=self.dim)
        vector[index] += sign * weight

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    @staticmethod
    def similarity(a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        # vectors are already normalised; clamp for float noise
        return max(-1.0, min(1.0, dot))


def _trigrams(token: str) -> List[str]:
    return [token[i:i + 3] for i in range(len(token) - 2)]


class OllamaEmbedder:
    """Real embeddings from a local Ollama model (default ``nomic-embed-text``)."""

    name = "ollama"

    def __init__(self, model: str = "nomic-embed-text",
                 base_url: str = "http://localhost:11434", timeout: float = 20.0):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def embed(self, text: str) -> List[float]:
        vectors = self.embed_many([text])
        return vectors[0]

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        import httpx

        out: List[List[float]] = []
        with httpx.Client(timeout=self.timeout) as client:
            for text in texts:
                resp = client.post(f"{self.base_url}/api/embeddings",
                                   json={"model": self.model, "prompt": text or " "})
                resp.raise_for_status()
                out.append([float(v) for v in resp.json().get("embedding", [])])
        return out

    def healthy(self) -> bool:
        import httpx

        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.post(f"{self.base_url}/api/embeddings",
                                   json={"model": self.model, "prompt": "ping"})
            return resp.status_code == 200 and bool(resp.json().get("embedding"))
        except Exception:
            return False


class Embedder:
    """Facade that picks a backend and transparently degrades.

    ``provider`` may be ``auto``, ``ollama`` or ``local``.  In ``auto`` mode the
    Ollama backend is *probed once*; if it is not usable we stay local (no
    per-call timeouts, no latency surprises).
    """

    def __init__(self, config: Optional[Mapping[str, Any]] = None):
        cfg = config or {}
        if "memory" in cfg and isinstance(cfg["memory"], Mapping):
            cfg = dict(cfg["memory"])
        lt = cfg.get("long_term") if isinstance(cfg.get("long_term"), Mapping) else {}
        self.provider = str(cfg.get("embedding_provider")
                            or (lt or {}).get("embedding_provider", "auto")).lower()
        self.model = str(cfg.get("embedding_model")
                         or (lt or {}).get("embedding_model", "nomic-embed-text"))
        self.dim = int(cfg.get("embedding_dim")
                       or (lt or {}).get("embedding_dim", EMBEDDING_DIM))
        from ..core.ollama_runtime import resolve_base_url

        self.base_url = resolve_base_url({"llm": cfg.get("llm") or {}})

        self._local = LocalHashingEmbedder(self.dim)
        self._ollama: Optional[OllamaEmbedder] = None
        self._backend = "local"
        self._cache: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._resolved = False
        self.calls = 0
        self.cache_hits = 0

    # ── backend selection ──────────────────────────────────────────────────
    def backend(self) -> str:
        with self._lock:
            if not self._resolved:
                self._resolve()
            return self._backend

    def _resolve(self) -> None:
        self._resolved = True
        if self.provider in ("local", "none", "off"):
            self._backend = "local"
            return
        try:
            self._ollama = OllamaEmbedder(self.model, self.base_url)
            if self._ollama.healthy():
                self._backend = "ollama"
                log.info(f"embeddings: ollama/{self.model}")
                return
            log.info(f"embeddings: ollama unavailable (model '{self.model}' not "
                     f"pulled?) — using local hashing embedder")
        except Exception as exc:
            log.info(f"embeddings: ollama probe failed ({exc}) — using local embedder")
        self._ollama = None
        self._backend = "local"

    def active_backend(self) -> str:
        return self.backend()

    # ── embedding ──────────────────────────────────────────────────────────
    def embed(self, text: str) -> List[float]:
        key = hashlib.blake2b((text or "").encode("utf-8"), digest_size=16).hexdigest()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached

        vector: List[float]
        if self.backend() == "ollama" and self._ollama is not None:
            try:
                vector = self._ollama.embed(text)
            except Exception as exc:
                log.warning(f"ollama embedding failed ({exc}); falling back to local")
                with self._lock:
                    self._backend = "local"
                vector = self._local.embed(text)
        else:
            vector = self._local.embed(text)

        with self._lock:
            self.calls += 1
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[key] = vector
        return vector

    def embed_many(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    @staticmethod
    def similarity(a: Sequence[float], b: Sequence[float]) -> float:
        if not a or not b:
            return 0.0
        if len(a) != len(b):
            n = min(len(a), len(b))
            a, b = a[:n], b[:n]
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return max(-1.0, min(1.0, dot / (na * nb)))

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            model = self.model if self._backend == "ollama" else self._local.model
            return {"backend": self._backend, "model": model, "dim": self.dim,
                    "calls": self.calls, "cache_hits": self.cache_hits,
                    "cache_size": len(self._cache)}


_EMBEDDER: Optional[Embedder] = None


def get_embedder(config: Optional[Mapping[str, Any]] = None) -> Embedder:
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = Embedder(config)
    return _EMBEDDER
