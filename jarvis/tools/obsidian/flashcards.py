"""Flashcards + spaced repetition for student study, straight out of the vault.

Formats understood (so the notes stay plain markdown and work with the
Obsidian *Spaced Repetition* plugin too):

* ``Question :: Answer`` — one card, inline, anywhere in a note.
* ``{{c1::hidden text}}`` — cloze deletion; several clozes become several cards.
* a ``#flashcard`` block — the question is the paragraph, the answer is the
  ``> answer`` line that follows it.

Scheduling is a compact SM-2 variant (the same family Anki uses), stored in a
JSON file so it never fights with the vault's own plugins.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...config import get as cfg_get
from ...config import load_config
from ...utils.logger import get_logger

log = get_logger("tools.obsidian.cards")

INLINE_CARD_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)?(.{3,300}?)\s*::\s*(.+?)\s*$")
CLOZE_RE = re.compile(r"\{\{c(\d+)::(.*?)(?:::[^}]*)?\}\}")
FLASHCARD_TAG_RE = re.compile(r"#flashcard\b", re.IGNORECASE)
ANSWER_LINE_RE = re.compile(r"^\s*>\s*(.+)$")
BOLD_TERM_RE = re.compile(r"(?:\*\*|__)([^*_]{2,60}?)(?:\*\*|__)")
DEFINITION_RE = re.compile(r"^\s*[-*+]\s+(?:\*\*|__)?([^:]{2,80}?)(?:\*\*|__)?\s*[—\-–:]\s+(.{10,400})$")

GRADES = ("again", "hard", "good", "easy")


@dataclass
class Card:
    """One question/answer pair, tied back to the note it lives in."""

    id: str
    note: str
    question: str
    answer: str
    kind: str = "qa"           # qa | cloze | flashcard-tag
    line: int = 0
    tags: List[str] = field(default_factory=list)
    # scheduling
    due: str = ""
    interval: float = 0.0
    ease: float = 2.5
    reps: int = 0
    lapses: int = 0
    last: str = ""
    state: str = "new"         # new | learning | review | mature

    @property
    def is_new(self) -> bool:
        return self.reps == 0

    def due_date(self) -> date:
        if not self.due:
            return date.today()
        try:
            return date.fromisoformat(self.due)
        except ValueError:
            return date.today()

    def overdue_days(self, today: Optional[date] = None) -> int:
        return ((today or date.today()) - self.due_date()).days

    def summary(self) -> str:
        bits = [f"[{self.state}]"]
        if self.reps:
            bits.append(f"ease {self.ease:.2f}, every {self.interval:.0f}d")
        else:
            bits.append("never reviewed")
        if self.due:
            bits.append(f"due {self.due}")
        return f"{self.question.strip()[:90]} ({', '.join(bits)})"


def card_id(note_rel: str, question: str) -> str:
    digest = hashlib.sha1(f"{note_rel}|{question.strip().lower()}".encode("utf-8"))
    return digest.hexdigest()[:16]


# ── extraction ──────────────────────────────────────────────────────────────
def cards_from_text(note_rel: str, body: str) -> List[Card]:
    """Every card the text contains, in file order."""
    cards: List[Card] = []
    lines = body.splitlines()
    pending_question: Optional[Tuple[int, str]] = None
    in_fence = False

    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence          # code blocks are documentation,
            continue                          # never study material
        if in_fence:
            continue
        if FLASHCARD_TAG_RE.search(line):
            question = FLASHCARD_TAG_RE.sub("", line).strip(" -#*")
            if question:
                pending_question = (number, question)
                continue
        if pending_question is not None:
            answer_match = ANSWER_LINE_RE.match(line)
            if answer_match:
                _, question = pending_question
                cards.append(Card(id=card_id(note_rel, question), note=note_rel,
                                  question=question, answer=answer_match.group(1).strip(),
                                  kind="flashcard-tag", line=pending_question[0]))
                pending_question = None
                continue
            if line.strip():
                pending_question = None

        for match in CLOZE_RE.finditer(line):
            index, hidden = match.group(1), match.group(2)
            if not hidden.strip():
                continue
            masked = CLOZE_RE.sub(lambda m: "[…]" if m.group(1) == index else m.group(0), line)
            masked = CLOZE_RE.sub(lambda m: m.group(2), masked)
            question = re.sub(r"\s+", " ", masked).strip(" -#*>")
            cards.append(Card(id=card_id(note_rel, f"{question}|{hidden}"), note=note_rel,
                              question=f"Fill in: {question}" if question else "Cloze",
                              answer=hidden.strip(), kind="cloze", line=number))

        inline = INLINE_CARD_RE.match(line)
        # a {{c1::…}} cloze also contains "::" — never double-count it as a QA card
        if (inline and "::" in line and not CLOZE_RE.search(line)
                and not line.strip().startswith("<!--")):
            question, answer = inline.group(1).strip(), inline.group(2).strip()
            if question and answer and not question.startswith("#"):
                cards.append(Card(id=card_id(note_rel, question), note=note_rel,
                                  question=question, answer=answer, kind="qa",
                                  line=number))

    seen: set[str] = set()
    unique: List[Card] = []
    for card in cards:
        if card.id in seen:
            continue
        seen.add(card.id)
        unique.append(card)
    return unique


def generate_cards_from_note(note_body: str, *, max_cards: int = 20) -> List[Card]:
    """Heuristically turn study notes into flashcard pairs — no model required.

    Two reliable patterns are mined:

    * definition bullets: ``- **Term** — explanation``
    * bold key terms in prose: ``The **mitochondria** produce ATP`` → cloze
    """
    cards: List[Card] = []
    for line in note_body.splitlines():
        definition = DEFINITION_RE.match(line)
        if definition:
            term, explanation = definition.group(1).strip(), definition.group(2).strip()
            question = (term if term.lower().startswith(("what", "why", "how", "define"))
                        else f"What is {term}?")
            cards.append(Card(id=card_id("generated", question), note="", question=question,
                              answer=explanation, kind="qa"))
            continue
        if len(cards) >= max_cards:
            break
        for match in BOLD_TERM_RE.finditer(line):
            term = match.group(1).strip()
            if not term or term.endswith("?") or ":" in term or len(term.split()) > 5:
                continue
            sentence = line.strip(" -#*")
            question = re.sub(r"\s+", " ",
                              BOLD_TERM_RE.sub("[…]", sentence, count=1)).strip()
            if len(question) < 20:
                continue
            cards.append(Card(id=card_id("generated", question), note="",
                              question=f"Fill in: {question}", answer=term, kind="cloze"))
        if len(cards) >= max_cards:
            break

    seen: set[str] = set()
    unique: List[Card] = []
    for card in cards:
        if card.id in seen:
            continue
        seen.add(card.id)
        unique.append(card)
    return unique[:max_cards]


def render_cards(cards: Sequence[Card], *, as_cloze: bool = False) -> str:
    """Markdown for a ``## Flashcards`` section (plain + plugin compatible)."""
    lines: List[str] = []
    for card in cards:
        if as_cloze and card.kind == "cloze":
            lines.append(f"- {card.question} :: {card.answer}")
        else:
            lines.append(f"- {card.question} :: {card.answer}")
    return "\n".join(lines)


# ── scheduling (SM-2 lite) ──────────────────────────────────────────────────
def schedule(card: Card, grade: str, *, today: Optional[date] = None) -> Card:
    """Apply a review grade and return the updated card."""
    grade = (grade or "good").strip().lower()
    if grade not in GRADES:
        raise ValueError(f"grade must be one of {GRADES}, not {grade!r}")
    today = today or date.today()
    card.reps += 1
    card.last = today.isoformat()

    if grade == "again":
        card.lapses += 1
        card.ease = max(1.3, card.ease - 0.2)
        card.interval = 0 if card.reps < 3 else max(1.0, card.interval * 0.5)
        card.state = "learning"
    elif grade == "hard":
        card.ease = max(1.3, card.ease - 0.15)
        card.interval = max(1.0, (card.interval or 0.5) * 1.2)
        card.state = "review"
    elif grade == "good":
        card.interval = 1.0 if not card.interval else card.interval * card.ease
        card.state = "review"
    else:  # easy
        card.ease = min(3.2, card.ease + 0.15)
        card.interval = 4.0 if not card.interval else card.interval * card.ease * 1.3
        card.state = "review"

    if card.interval >= 21:
        card.state = "mature"
    days = max(0, round(card.interval))
    card.due = (today + timedelta(days=days)).isoformat()
    if card.interval == 0:
        # "again" comes back later the same day; store a same-day due date
        card.due = today.isoformat()
    return card


class SrsStore:
    """Persistent review state, keyed by card id (one JSON file)."""

    def __init__(self, path: Optional[str | Path] = None,
                 config: Optional[Mapping[str, Any]] = None):
        config = config or load_config()
        default = Path(__file__).resolve().parents[3] / "data" / "obsidian_srs.json"
        target = path or cfg_get(config, "obsidian.srs.database", "") or default
        self.path = Path(str(target)).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = {"cards": {}, "reviews": 0, "streak_days": []}
        self._load()

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            self._data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning(f"could not read SRS store {self.path}: {exc}")

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    # ── api ───────────────────────────────────────────────────────────────
    def get(self, card: Card) -> Card:
        record = (self._data["cards"] or {}).get(card.id) or {}
        for key in ("due", "interval", "ease", "reps", "lapses", "last", "state"):
            if key in record:
                setattr(card, key, record[key])
        return card

    def update(self, card: Card) -> Card:
        self._data.setdefault("cards", {})[card.id] = {
            "note": card.note, "question": card.question[:120], "due": card.due,
            "interval": card.interval, "ease": card.ease, "reps": card.reps,
            "lapses": card.lapses, "last": card.last, "state": card.state,
        }
        self._data["reviews"] = int(self._data.get("reviews", 0)) + 1
        today = date.today().isoformat()
        days = self._data.setdefault("streak_days", [])
        if today not in days:
            days.append(today)
            self._data["streak_days"] = days[-400:]
        self.save()
        return card

    def stats(self) -> Dict[str, Any]:
        cards = list((self._data.get("cards") or {}).values())
        today = date.today().isoformat()
        return {
            "tracked": len(cards),
            "due": sum(1 for c in cards if (c.get("due") or "0000") <= today),
            "mature": sum(1 for c in cards if c.get("state") == "mature"),
            "learning": sum(1 for c in cards if c.get("state") == "learning"),
            "reviews": int(self._data.get("reviews", 0)),
            "streak": self.streak(),
            "path": str(self.path),
        }

    def streak(self) -> int:
        days = sorted({str(day) for day in self._data.get("streak_days", [])}, reverse=True)
        if not days:
            return 0
        streak = 0
        cursor = date.today()
        for day in days:
            try:
                parsed = date.fromisoformat(day)
            except ValueError:
                continue
            if parsed == cursor:
                streak += 1
                cursor -= timedelta(days=1)
            elif parsed == cursor - timedelta(days=1) and streak == 0:
                streak += 1
                cursor = parsed - timedelta(days=1)
            elif parsed < cursor:
                break
        return streak


# ── study helpers ───────────────────────────────────────────────────────────
def collect_cards(vault: Path, *, folder: str = "", store: Optional[SrsStore] = None,
                  include_generated: bool = False) -> List[Card]:
    """All cards in the vault, with their scheduling state applied."""
    from .vault import load_notes

    cards: List[Card] = []
    for note in load_notes(vault, folder=folder):
        found = cards_from_text(note.rel, note.body)
        if include_generated and not found and "#flashcard" in note.tags:
            found = generate_cards_from_note(note.body)
        for card in found:
            card.tags = list(note.tags)
            if store:
                store.get(card)
            else:
                card.due = card.due or date.today().isoformat()
            cards.append(card)
    return cards


def due_cards(cards: Sequence[Card], *, today: Optional[date] = None,
              limit: int = 20, include_new: bool = True) -> List[Card]:
    today = today or date.today()
    due: List[Card] = []
    for card in cards:
        if card.is_new and include_new:
            due.append(card)
        elif not card.is_new and card.due_date() <= today:
            due.append(card)
    due.sort(key=lambda card: (card.reps > 0, card.due_date(), card.note))
    return due[:limit]


def next_review_dates(cards: Sequence[Card], *, days: int = 7,
                      today: Optional[date] = None) -> Dict[str, int]:
    """How many cards come back on each of the next ``days`` days."""
    today = today or date.today()
    buckets: Dict[str, int] = {}
    for offset in range(days):
        buckets[(today + timedelta(days=offset)).isoformat()] = 0
    for card in cards:
        if card.is_new:
            continue
        key = card.due_date().isoformat()
        if key in buckets:
            buckets[key] += 1
    return buckets


def review_forecast(cards: Sequence[Card]) -> str:
    """Human summary used by the CLI and the study report."""
    buckets = next_review_dates(cards)
    today = date.today().isoformat()
    lines = [f"Today ({today}): {buckets.get(today, 0)} card(s) due"]
    for key in sorted(buckets)[1:]:
        lines.append(f"  {key}: {buckets[key]}")
    return "\n".join(lines)
