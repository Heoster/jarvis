"""Knowledge base — RAG over local documents.

Ingests anything text-ish (``.md``, ``.txt``, ``.py``, ``.json``, ``.yaml``,
``.csv``, ``.html``, ``.rst``, ``.pdf`` when a parser is available), chunks it
with overlapping windows that respect paragraph/code boundaries, and stores the
chunks in long-term memory as ``semantic`` records.

Crucially the ingester is **incremental**: it keeps a manifest of file hashes so
re-running it (or a chat turn triggering it) only touches what changed.  That is
what makes "auto-ingest on start" cheap enough to leave switched on.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from ..utils.sanitizer import sanitize_text, truncate
from .long_term import LongTermMemory, MemoryTier

log = get_logger("knowledge")

__all__ = ["KnowledgeBase", "chunk_text"]

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".py", ".js", ".ts",
                      ".json", ".yaml", ".yml", ".toml", ".csv", ".html", ".htm",
                      ".tex", ".sql", ".sh", ".pdf"}


def chunk_text(text: str, *, chunk_size: int = 900, overlap: int = 120,
               respect_boundaries: bool = True) -> List[str]:
    """Split text into overlapping chunks.

    When ``respect_boundaries`` is on, the splitter prefers blank lines, then
    sentence ends, then whitespace, so a chunk rarely starts mid-word or
    mid-code-block.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: List[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + chunk_size)
        if end < length and respect_boundaries:
            window = text[start:end]
            best = -1
            for pattern in (r"\n\s*\n", r"(?<=[.!?])\s", r"\n"):
                matches = list(re.finditer(pattern, window))
                if matches:
                    candidate = start + matches[-1].end()
                    if candidate - start >= chunk_size * 0.55:
                        best = candidate
                        break
            if best > start:
                end = best
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks


class KnowledgeBase:
    """Document ingestion + retrieval on top of long-term memory."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 memory: Optional[LongTermMemory] = None):
        cfg = dict(config or {})
        if "memory" in cfg and isinstance(cfg["memory"], Mapping):
            cfg = dict(cfg["memory"])
        kb = dict(cfg.get("knowledge_base") or {})
        self.enabled = bool(kb.get("enabled", True))
        self.directory = Path(kb.get("directory")
                              or (Path(__file__).resolve().parents[2] / "knowledge"))
        self.chunk_size = int(kb.get("chunk_size", 900))
        self.chunk_overlap = int(kb.get("chunk_overlap", 120))
        self.memory = memory or LongTermMemory(config)
        self.manifest_path = self.directory / ".ingest_manifest.json"
        self._manifest: Dict[str, Dict[str, Any]] = self._load_manifest()
        self.last_report: Dict[str, Any] = {}

    # ── manifest ───────────────────────────────────────────────────────────
    def _load_manifest(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self.manifest_path.exists():
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_manifest(self) -> None:
        try:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(json.dumps(self._manifest, indent=2),
                                          encoding="utf-8")
        except OSError as exc:
            log.debug(f"cannot persist manifest: {exc}")

    @staticmethod
    def _file_hash(path: Path) -> str:
        try:
            return hashlib.blake2b(path.read_bytes(), digest_size=12).hexdigest()
        except OSError:
            return ""

    # ── ingestion ──────────────────────────────────────────────────────────
    def discover(self) -> List[Path]:
        if not self.directory.exists():
            return []
        return sorted(
            path for path in self.directory.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
            and not path.name.startswith(".")
            and path.name != self.manifest_path.name
        )

    def ingest(self, *, force: bool = False, max_files: int = 500) -> Dict[str, Any]:
        """Ingest new/changed documents.  Returns a small report."""
        if not self.enabled:
            return {"skipped": "knowledge base disabled"}
        files = self.discover()[:max_files]
        ingested = skipped = failed = 0
        total_chunks = 0
        started = datetime.now(timezone.utc)

        for path in files:
            digest = self._file_hash(path)
            entry = self._manifest.get(str(path))
            if not force and entry and entry.get("hash") == digest:
                skipped += 1
                continue
            try:
                text = self._read(path)
                chunks = chunk_text(text, chunk_size=self.chunk_size,
                                    overlap=self.chunk_overlap)
                for index, chunk in enumerate(chunks):
                    self.memory.store_knowledge(chunk, source=str(path),
                                                topic=path.stem, chunk_index=index)
                self._manifest[str(path)] = {
                    "hash": digest, "chunks": len(chunks),
                    "ingested_at": datetime.now(timezone.utc).isoformat(),
                }
                ingested += 1
                total_chunks += len(chunks)
                log.debug(f"ingested {path.name} → {len(chunks)} chunks")
            except Exception as exc:
                failed += 1
                log.warning(f"failed to ingest {path.name}: {exc}")
        self._save_manifest()

        report = {
            "files_seen": len(files), "ingested": ingested, "unchanged": skipped,
            "failed": failed, "chunks": total_chunks,
            "elapsed_s": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
            "directory": str(self.directory),
        }
        self.last_report = report
        if ingested or failed:
            log.info(f"knowledge ingest: {ingested} file(s), {total_chunks} chunks, "
                     f"{failed} failed, {skipped} unchanged")
        return report

    def _read(self, path: Path) -> str:
        if path.suffix.lower() == ".pdf":
            return self._read_pdf(path)
        raw = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in (".html", ".htm"):
            raw = _html_to_text(raw)
        return sanitize_text(raw, wrap_untrusted=False)

    @staticmethod
    def _read_pdf(path: Path) -> str:
        for module in ("pypdf", "PyPDF2", "pdfminer.high_level"):
            try:
                if module == "pypdf":
                    from pypdf import PdfReader  # type: ignore

                    reader = PdfReader(str(path))
                    return "\n".join((page.extract_text() or "") for page in reader.pages)
                if module == "PyPDF2":
                    from PyPDF2 import PdfReader  # type: ignore

                    reader = PdfReader(str(path))
                    return "\n".join((page.extract_text() or "") for page in reader.pages)
                from pdfminer.high_level import extract_text  # type: ignore

                return extract_text(str(path))
            except ImportError:
                continue
            except Exception as exc:
                log.debug(f"pdf parse failed for {path.name}: {exc}")
                break
        log.warning(f"no PDF parser available for {path.name} (pip install pypdf)")
        return ""

    def add_text(self, text: str, *, source: str, topic: str = "") -> int:
        """Ingest a raw string (used when a crawled page is worth keeping)."""
        chunks = chunk_text(sanitize_text(text, wrap_untrusted=False),
                            chunk_size=self.chunk_size, overlap=self.chunk_overlap)
        for index, chunk in enumerate(chunks):
            self.memory.store_knowledge(chunk, source=source,
                                        topic=topic or "web", chunk_index=index)
        return len(chunks)

    def add_url(self, url: str, text: str, *, topic: str = "") -> int:
        return self.add_text(text, source=url, topic=topic or "web")

    # ── retrieval ──────────────────────────────────────────────────────────
    def query(self, question: str, k: int = 3, *, budget_chars: int = 2500) -> str:
        """Return a formatted, budget-limited knowledge block (or '')."""
        pairs = self.memory.retrieve(question, k=k, tier=MemoryTier.SEMANTIC)
        if not pairs:
            return ""
        lines: List[str] = []
        used = 0
        for memory, score in pairs:
            source = Path(memory.source).name if memory.source else "unknown"
            block = f"[{source} · {score:.2f}] {memory.content.strip()}"
            if used + len(block) > budget_chars:
                block = truncate(block, max(120, budget_chars - used))
            if not block:
                break
            lines.append(block)
            used += len(block)
            if used >= budget_chars:
                break
        return "\n\n".join(lines)

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "documents": len(self._manifest),
            "supported_files": len(self.discover()),
            "chunk_size": self.chunk_size,
            "last_ingest": self.last_report or None,
        }


_BLOCK_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]{2,}")


def _html_to_text(html: str) -> str:
    text = _BLOCK_RE.sub(" ", html)
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#39;", "'"))
    text = _WS_RE.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
