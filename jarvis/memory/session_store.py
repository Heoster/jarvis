"""Session persistence (SQLite).

A *session* is one continuous conversation with Heoster.  JARVIS auto-saves on
an interval and on exit, keeps a rolling digest of what happened, and can
resume the most recent session on the next launch — which is what makes the
memory feel continuous across restarts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..core.messages import Message, ensure_messages, message_from_dict
from ..utils.logger import get_logger
from ..utils.sanitizer import truncate

log = get_logger("memory.session")

__all__ = ["Session", "SessionStore"]


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat()


@dataclass
class Session:
    id: str
    title: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    summary: str = ""
    turn_count: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title,
            "started_at": _iso(self.started_at), "updated_at": _iso(self.updated_at),
            "ended_at": _iso(self.ended_at) if self.ended_at else None,
            "summary": self.summary, "turn_count": self.turn_count,
            "tool_calls": self.tool_calls, "tokens_used": self.tokens_used,
            "metadata": self.metadata,
        }

    def age_line(self) -> str:
        minutes = max(0, int((time.time() - self.started_at) / 60))
        return f"{self.id[:8]} · {minutes}m · {self.turn_count} turns · {self.tool_calls} tools"


class SessionStore:
    """SQLite-backed session + message log."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *,
                 path: Optional[str | Path] = None):
        cfg = dict(config or {})
        session_cfg = cfg.get("sessions") if isinstance(cfg.get("sessions"), Mapping) else {}
        session_cfg = dict(session_cfg or {})
        default = Path(__file__).resolve().parents[2] / "sessions" / "jarvis.db"
        db_path = Path(path or session_cfg.get("database") or default).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.auto_save = bool(session_cfg.get("auto_save", True))
        self.save_interval = float(session_cfg.get("save_interval", 60))
        self.max_sessions = int(session_cfg.get("max_sessions", 100))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self.current: Optional[Session] = None
        self._last_save = 0.0

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT, started_at REAL, updated_at REAL,
                    ended_at REAL, summary TEXT, turn_count INTEGER DEFAULT 0,
                    tool_calls INTEGER DEFAULT 0, tokens_used INTEGER DEFAULT 0,
                    metadata TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    role TEXT, content TEXT, payload TEXT, created_at REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_session ON session_messages(session_id, seq)"
            )

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self, *, title: str = "", resume: bool = False,
              metadata: Optional[Mapping[str, Any]] = None) -> Session:
        if resume:
            recent = self.recent(limit=1)
            if recent:
                session = self.load(recent[0].id)
                if session:
                    session.ended_at = None
                    self.current = session
                    self.save(session)
                    log.info(f"resumed session {session.id[:8]} "
                             f"({session.turn_count} turns)")
                    return session
        session = Session(id=uuid.uuid4().hex[:16],
                          title=title or f"session {_iso()[:16]}",
                          metadata=dict(metadata or {}))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (id, title, started_at, updated_at, ended_at,"
                " summary, turn_count, tool_calls, tokens_used, metadata)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session.id, session.title, session.started_at, session.updated_at,
                 None, "", 0, 0, 0, json.dumps(session.metadata)),
            )
        self.current = session
        log.info(f"started session {session.id[:8]}")
        return session

    def save(self, session: Optional[Session] = None, *, force: bool = False) -> bool:
        session = session or self.current
        if session is None:
            return False
        now = time.time()
        if not force and (now - self._last_save) < self.save_interval:
            return False
        session.updated_at = now
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET title=?, updated_at=?, ended_at=?, summary=?,"
                " turn_count=?, tool_calls=?, tokens_used=?, metadata=? WHERE id=?",
                (session.title, session.updated_at, session.ended_at, session.summary,
                 session.turn_count, session.tool_calls, session.tokens_used,
                 json.dumps(session.metadata), session.id),
            )
        self._last_save = now
        return True

    def append_messages(self, messages: Sequence[Any], *,
                        session: Optional[Session] = None) -> int:
        session = session or self.current
        if session is None:
            return 0
        msgs = ensure_messages(messages)
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM session_messages WHERE session_id=?",
                (session.id,))
            seq = int(cur.fetchone()[0])
            with self._conn:
                for msg in msgs:
                    seq += 1
                    self._conn.execute(
                        "INSERT INTO session_messages (session_id, seq, role, content,"
                        " payload, created_at) VALUES (?,?,?,?,?,?)",
                        (session.id, seq, msg.role, msg.content,
                         json.dumps(msg.to_dict(), ensure_ascii=False), time.time()),
                    )
        session.updated_at = time.time()
        return len(msgs)

    def bump(self, *, turns: int = 0, tool_calls: int = 0, tokens: int = 0) -> None:
        if self.current is None:
            return
        self.current.turn_count += turns
        self.current.tool_calls += tool_calls
        self.current.tokens_used += tokens
        if self.auto_save:
            self.save()

    def end(self, *, summary: str = "") -> Optional[Session]:
        if self.current is None:
            return None
        self.current.ended_at = time.time()
        if summary:
            self.current.summary = summary
        self.current.updated_at = self.current.ended_at
        self.save(self.current, force=True)
        ended, self.current = self.current, None
        log.info(f"ended session {ended.id[:8]} "
                 f"({ended.turn_count} turns, {ended.tool_calls} tool calls)")
        self._trim()
        return ended

    def _trim(self) -> None:
        sessions = self.recent(limit=self.max_sessions + 50)
        if len(sessions) <= self.max_sessions:
            return
        stale = sessions[self.max_sessions:]
        with self._lock, self._conn:
            for session in stale:
                self._conn.execute("DELETE FROM sessions WHERE id=?", (session.id,))
                self._conn.execute("DELETE FROM session_messages WHERE session_id=?",
                                   (session.id,))
        log.debug(f"trimmed {len(stale)} old session(s)")

    # ── reading ────────────────────────────────────────────────────────────
    def load(self, session_id: str) -> Optional[Session]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?",
                                     (session_id,)).fetchone()
        if row is None:
            return None
        return Session(
            id=row["id"], title=row["title"] or "", started_at=row["started_at"],
            updated_at=row["updated_at"], ended_at=row["ended_at"],
            summary=row["summary"] or "", turn_count=int(row["turn_count"] or 0),
            tool_calls=int(row["tool_calls"] or 0),
            tokens_used=int(row["tokens_used"] or 0),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    def messages(self, session_id: str, *, limit: int = 500) -> List[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload, role, content FROM session_messages WHERE session_id=?"
                " ORDER BY seq ASC LIMIT ?", (session_id, limit)).fetchall()
        out: List[Message] = []
        for row in rows:
            try:
                out.append(message_from_dict(json.loads(row["payload"])))
            except Exception:
                out.append(Message(content=row["content"] or "", role=row["role"] or "human"))
        return out

    def recent(self, limit: int = 10) -> List[Session]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        out: List[Session] = []
        for row in rows:
            out.append(Session(
                id=row["id"], title=row["title"] or "", started_at=row["started_at"],
                updated_at=row["updated_at"], ended_at=row["ended_at"],
                summary=row["summary"] or "", turn_count=int(row["turn_count"] or 0),
                tool_calls=int(row["tool_calls"] or 0),
                tokens_used=int(row["tokens_used"] or 0),
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            ))
        return out

    def search(self, query: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Full-text-ish search over stored messages."""
        needle = f"%{query}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, role, content, created_at FROM session_messages"
                " WHERE content LIKE ? ORDER BY id DESC LIMIT ?", (needle, limit)).fetchall()
        return [{"session_id": row["session_id"], "role": row["role"],
                 "content": truncate(row["content"] or "", 240),
                 "when": _iso(row["created_at"])} for row in rows]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            sessions = int(self._conn.execute(
                "SELECT COUNT(*) FROM sessions").fetchone()[0])
            messages = int(self._conn.execute(
                "SELECT COUNT(*) FROM session_messages").fetchone()[0])
        size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {"sessions": sessions, "messages": messages,
                "path": str(self.db_path), "size_mb": round(size / 1e6, 2),
                "current": self.current.as_dict() if self.current else None}

    def close(self) -> None:
        try:
            if self.current is not None:
                self.save(self.current, force=True)
            self._conn.close()
        except Exception:
            pass
