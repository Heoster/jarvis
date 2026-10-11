"""The study coach: retention analytics + a scheduled daily brief.

Two ideas, one module.

**Weak-topic analytics.** Every review JARVIS records is kept in the spaced
repetition store (grade, card, note, date).  From that it can say which subject
is actually slipping — *"Physics: 6 lapses in 14 reviews (57% retention)"* — and
which notes to attack first.  Weakness combines lapses, low ease, overdue-ness
and cards stuck in learning.

**The daily brief.** Instead of waiting to be asked, the coach assembles what
matters right now (due cards, overdue tasks, exam countdown, weak topics, streak)
and writes it into today's daily note under ``### JARVIS brief``, optionally
firing a desktop notification and reading a one-line summary aloud.  Run it once
(``jarvis coach brief``) or leave it watching (``jarvis coach watch --at 19:30``).

Everything is free, offline and derived from files already in the vault.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from .flashcards import SrsStore
from .study import exam_countdown, revision_queue
from .vault import ObsidianError, daily_note, load_notes, upsert_section, write_note

log = get_logger("tools.obsidian.coach")

BRIEF_HEADING = "JARVIS brief"
WEEKLY_FOLDER = "05 Revision"
TIME_FORMAT = "%H:%M"
DEFAULT_TIMES = ("19:30",)
GRADES = ("again", "hard", "good", "easy")


# ── small helpers ───────────────────────────────────────────────────────────
def parse_times(spec: str | Sequence[str], *, default: Sequence[str] = DEFAULT_TIMES
                ) -> List[str]:
    """``"19:30, 07:00"`` → ``["07:00", "19:30"]`` (validated, de-duplicated)."""
    if isinstance(spec, str):
        raw = [part.strip() for part in spec.replace(";", ",").split(",")]
    else:
        raw = [str(part).strip() for part in spec]
    out: List[str] = []
    for item in raw:
        if not item:
            continue
        try:
            parsed = datetime.strptime(item, TIME_FORMAT)
        except ValueError as exc:
            raise ValueError(f"{item!r} is not a time like 19:30") from exc
        normalised = parsed.strftime(TIME_FORMAT)
        if normalised not in out:
            out.append(normalised)
    if not out:
        out = [str(item) for item in default]
    return sorted(out)


def next_occurrence(times: Sequence[str], *, now: Optional[datetime] = None) -> datetime:
    """The next scheduled moment strictly after ``now``."""
    now = now or datetime.now()
    candidates: List[datetime] = []
    for item in times:
        when = datetime.strptime(item, TIME_FORMAT).time()
        today = datetime.combine(now.date(), when)
        candidates.append(today if today > now else today + timedelta(days=1))
    if not candidates:
        raise ValueError("no times given")
    return min(candidates)


def subject_of(note_rel: str, subjects: Sequence[str] = ()) -> str:
    """Map a note path to a subject: ``01 Subjects/Physics/…`` → ``Physics``."""
    parts = [part for part in str(note_rel).replace("\\", "/").split("/") if part]
    lower = [part.lower() for part in parts]
    if "01 subjects" in lower:
        index = lower.index("01 subjects")
        if index + 1 < len(parts):
            return parts[index + 1]
    for subject in subjects:
        if subject and subject.lower() in lower:
            return subject
    for folder in ("03 Flashcards", "04 Exams", "05 Revision", "02 Daily Notes"):
        if folder.lower() in lower:
            return folder.split(" ", 1)[1]
    return "General"


def _configured_subjects(config: Optional[Mapping[str, Any]]) -> List[str]:
    from ...config import get as cfg_get

    raw = cfg_get(config or {}, "obsidian.subjects", []) or []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [str(item) for item in raw]


# ── retention ───────────────────────────────────────────────────────────────
@dataclass
class TopicStats:
    """What the SRS store knows about one note (one topic)."""

    note: str
    subject: str
    topic: str
    cards: int = 0
    reviews: int = 0
    lapses: int = 0
    due: int = 0
    new: int = 0
    last_review: str = ""
    worst_question: str = ""
    worst_lapses: int = 0
    ease: float = 2.5
    overdue: int = 0
    score: float = 0.0

    @property
    def retention(self) -> int:
        if not self.reviews:
            return 0
        return max(0, min(100, round(100 * (1 - self.lapses / self.reviews))))

    def as_dict(self) -> Dict[str, Any]:
        data = dict(self.__dict__)
        data["retention"] = self.retention
        return data


def _topic_name(note_rel: str) -> str:
    return Path(str(note_rel)).stem or str(note_rel)


def topic_stats(vault: Path, config: Optional[Mapping[str, Any]] = None,
                *, days: Optional[int] = None) -> List[TopicStats]:
    """Aggregate the SRS store per note, scored by how much trouble it gives."""
    from ...config import get as cfg_get

    config = config or {}
    window = days if days is not None else int(cfg_get(config, "obsidian.coach.window_days", 30))
    subjects = _configured_subjects(config)
    today = date.today()
    store = SrsStore(config=config)
    records = (store._data.get("cards") or {})          # noqa: SLF001 - same package
    log_entries = list(store._data.get("log") or [])

    recent: Dict[str, int] = {}
    cutoff = (today - timedelta(days=max(0, window))).isoformat()
    for entry in log_entries:
        if str(entry.get("d") or "") >= cutoff:
            recent[str(entry.get("n") or "")] = recent.get(str(entry.get("n") or ""), 0) + 1

    by_note: Dict[str, TopicStats] = {}
    for record in records.values():
        note_rel = str(record.get("note") or "")
        if not note_rel:
            continue
        stats = by_note.setdefault(note_rel, TopicStats(
            note=note_rel, subject=subject_of(note_rel, subjects), topic=_topic_name(note_rel)))
        reps = int(record.get("reps") or 0)
        lapses = int(record.get("lapses") or 0)
        ease = float(record.get("ease") or 2.5)
        due = str(record.get("due") or "")
        stats.cards += 1
        stats.reviews += reps
        stats.lapses += lapses
        stats.ease = round(min(stats.ease, ease), 3)
        if not reps:
            stats.new += 1
        if due and due <= today.isoformat():
            stats.due += 1
            stats.overdue += max(0, (today - date.fromisoformat(due)).days)
        last = str(record.get("last") or "")
        if last > stats.last_review:
            stats.last_review = last
        if lapses > stats.worst_lapses:
            stats.worst_lapses = lapses
            stats.worst_question = str(record.get("question") or "")[:80]

    for stats in by_note.values():
        retention_gap = (100 - stats.retention) if stats.reviews else 0
        stats.score = round(
            3.0 * stats.lapses
            + retention_gap / 20.0
            + min(3.0, stats.overdue / 3.0)
            + max(0.0, 2.5 - stats.ease)
            + (1.5 if recent.get(stats.note, 0) == 0 and stats.reviews else 0.0),
            2)
    return sorted(by_note.values(), key=lambda item: (-item.score, item.subject, item.topic))


def retention_report(vault: Path, config: Optional[Mapping[str, Any]] = None, *,
                     days: Optional[int] = None,
                     stale_days: Optional[int] = None) -> Dict[str, Any]:
    """Per-subject retention, weakest topics, and subjects that went quiet."""
    from ...config import get as cfg_get

    config = config or {}
    window = days if days is not None else int(cfg_get(config, "obsidian.coach.window_days", 30))
    quiet_after = (stale_days if stale_days is not None
                   else int(cfg_get(config, "obsidian.coach.stale_days", 7)))
    store = SrsStore(config=config)
    topics = topic_stats(vault, config, days=window)
    today = date.today()

    subjects: Dict[str, Dict[str, Any]] = {}
    for stats in topics:
        row = subjects.setdefault(stats.subject, {
            "subject": stats.subject, "cards": 0, "reviews": 0, "lapses": 0, "due": 0,
            "new": 0, "topics": 0, "last_review": "", "retention": 0, "days_since": None})
        row["cards"] += stats.cards
        row["reviews"] += stats.reviews
        row["lapses"] += stats.lapses
        row["due"] += stats.due
        row["new"] += stats.new
        row["topics"] += 1
        row["last_review"] = max(row["last_review"], stats.last_review)

    for row in subjects.values():
        row["retention"] = (max(0, round(100 * (1 - row["lapses"] / row["reviews"])))
                            if row["reviews"] else 0)
        if row["last_review"]:
            try:
                row["days_since"] = (today - date.fromisoformat(row["last_review"])).days
            except ValueError:
                row["days_since"] = None
        row["stale"] = bool(row["reviews"] and row["days_since"] is not None
                            and row["days_since"] >= quiet_after)

    totals = {
        "reviewed_cards": sum(1 for record in (store._data.get("cards") or {}).values()
                              if int(record.get("reps") or 0)),
        "reviews": sum(int(record.get("reps") or 0)
                       for record in (store._data.get("cards") or {}).values()),
        "lapses": sum(int(record.get("lapses") or 0)
                      for record in (store._data.get("cards") or {}).values()),
        "reviews_in_window": sum(1 for entry in (store._data.get("log") or [])
                                 if str(entry.get("d") or "") >=
                                 (today - timedelta(days=max(0, window))).isoformat()),
        "window_days": window,
    }
    totals["retention"] = (max(0, round(100 * (1 - totals["lapses"] / totals["reviews"])))
                           if totals["reviews"] else 0)
    return {"subjects": sorted(subjects.values(), key=lambda row: (row["retention"],
                                                                   -row["lapses"])),
            "topics": [stats.as_dict() for stats in topics],
            "weakest": [stats.as_dict() for stats in topics if stats.reviews][:10],
            "stale": [row["subject"] for row in subjects.values() if row["stale"]],
            "totals": totals, "stats": store.stats()}


def weak_topics(vault: Path, config: Optional[Mapping[str, Any]] = None, *,
                limit: int = 5, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """The topics that deserve attention first (most trouble per card)."""
    report = retention_report(vault, config, days=days)
    ranked = [topic for topic in report["topics"] if topic["reviews"] or topic["due"]]
    return ranked[:max(1, int(limit))]


# ── the brief ───────────────────────────────────────────────────────────────
def build_brief(vault: Path, config: Optional[Mapping[str, Any]] = None, *,
                day: Optional[date] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    """Everything worth knowing today, in one dict."""
    from ...config import get as cfg_get

    config = config or {}
    day = day or date.today()
    limit = int(limit if limit is not None
                else cfg_get(config, "obsidian.coach.max_items", 6))
    queue = revision_queue(vault, limit=max(limit, 8))
    report = retention_report(vault, config)
    exams = exam_countdown(vault, config)
    minutes = int(cfg_get(config, "obsidian.study.session_minutes", 45))

    due_cards = [_card_row(card) for card in queue["due_cards"]]
    tasks = [_task_row(task) for task in queue["tasks"]]
    weak = [topic for topic in report["weakest"] if topic["reviews"]] or \
        [topic for topic in report["topics"] if topic["due"]]
    focus = weak[0] if weak else None

    headline = _headline(len(due_cards), len(tasks), exams, focus)
    return {
        "day": day.isoformat(),
        "headline": headline,
        "minutes": minutes,
        "due_cards": due_cards[:limit],
        "due_card_count": len(due_cards),
        "tasks": tasks[:limit],
        "task_count": len(tasks),
        "revision_notes": queue["notes"][:limit],
        "exams": exams[:2],
        "weak": weak[:3],
        "stale": report["stale"][:3],
        "streak": report["stats"].get("streak", 0),
        "reviews_in_window": report["totals"]["reviews_in_window"],
        "retention": report["totals"]["retention"],
        "generated_at": datetime.now().isoformat(timespec="minutes"),
    }


def _card_row(card: Any) -> Dict[str, Any]:
    """A due card as a plain dict (the queue hands out Card dataclasses)."""
    if isinstance(card, Mapping):
        return dict(card)
    return {"question": getattr(card, "question", ""), "note": getattr(card, "note", ""),
            "lapses": int(getattr(card, "lapses", 0) or 0),
            "state": getattr(card, "state", "new"), "due": getattr(card, "due", ""),
            "ease": float(getattr(card, "ease", 2.5) or 2.5),
            "reps": int(getattr(card, "reps", 0) or 0)}


def _task_row(task: Any) -> Dict[str, Any]:
    """A task as a plain dict (the queue hands out Task dataclasses)."""
    if isinstance(task, Mapping):
        return dict(task)
    return {"text": getattr(task, "text", ""), "note": getattr(task, "note", ""),
            "due": getattr(task, "due", ""), "priority": getattr(task, "priority", ""),
            "line": int(getattr(task, "line", 0) or 0)}


def _headline(cards: int, tasks: int, exams: Sequence[Mapping[str, Any]],
              focus: Optional[Mapping[str, Any]]) -> str:
    if cards or tasks:
        pieces = []
        if cards:
            pieces.append(f"{cards} card(s) due")
        if tasks:
            pieces.append(f"{tasks} task(s) overdue")
        text = " · ".join(pieces)
    elif exams:
        text = "nothing due — get ahead of the next exam"
    else:
        text = "all clear"
    if focus:
        gap = f"{focus['subject']} / {focus['topic']}"
        if focus.get("reviews"):
            text += f" — weakest: {gap} ({focus['retention']}% retention)"
        else:
            text += f" — start with {gap}"
    if exams:
        soonest = exams[0]
        text += f" · {soonest['exam']} in {soonest['days_left']}d"
    return text


def render_brief(brief: Mapping[str, Any]) -> str:
    """Markdown for the brief (used in daily notes, chat and the CLI)."""
    day = date.fromisoformat(str(brief.get("day")))
    lines = [f"### {BRIEF_HEADING}",
             "",
             f"_{day.strftime('%a %d %b')} · {brief.get('minutes', 45)} min planned · "
             f"{brief.get('headline', '')}_",
             ""]

    cards = brief.get("due_cards") or []
    if cards:
        lines.append(f"**Revise now** ({brief.get('due_card_count', len(cards))} card(s))")
        for card in cards:
            flag = "⚠ " if int(card.get("lapses") or 0) else ""
            state = card.get("state") or "new"
            lines.append(f"- {flag}{card.get('question', '')} "
                         f"_({card.get('note', '')} · {state})_")
        lines.append("")
    tasks = brief.get("tasks") or []
    if tasks:
        lines.append(f"**Overdue tasks** ({brief.get('task_count', len(tasks))})")
        for task in tasks:
            due = f" · due {task.get('due')}" if task.get("due") else ""
            lines.append(f"- [ ] {task.get('text', '')} _({task.get('note', '')}{due})_")
        lines.append("")

    weak = brief.get("weak") or []
    if weak:
        lines.append("**Weak spots**")
        for topic in weak:
            if topic.get("reviews"):
                lines.append(f"- {topic['subject']} · {topic['topic']} — "
                             f"{topic['lapses']} lapse(s) in {topic['reviews']} review(s) "
                             f"({topic['retention']}% retention)")
            else:
                lines.append(f"- {topic['subject']} · {topic['topic']} — never reviewed")
        lines.append("")

    exams = brief.get("exams") or []
    if exams:
        lines.append("**Exams**")
        for exam in exams:
            lines.append(f"- {exam['exam']} — {exam['days_left']} day(s) left ({exam['date']})")
        lines.append("")

    if brief.get("stale"):
        lines.append(f"**Gone quiet:** {', '.join(brief['stale'])}")
        lines.append("")

    footer = [f"streak {brief.get('streak', 0)}d",
              f"{brief.get('reviews_in_window', 0)} review(s) this month",
              f"{brief.get('retention', 0)}% retention"]
    lines.append(f"_JARVIS · {' · '.join(footer)}_")
    return "\n".join(lines).rstrip() + "\n"


def write_brief(vault: Path, config: Optional[Mapping[str, Any]] = None, *,
                brief: Optional[Mapping[str, Any]] = None, day: Optional[date] = None,
                heading: str = BRIEF_HEADING) -> Dict[str, Any]:
    """Write (or refresh) today's brief in the daily note — never duplicated."""
    from ...config import get as cfg_get

    config = config or {}
    day = day or date.today()
    brief = brief or build_brief(vault, config, day=day)
    note = daily_note(vault, day=day, config=config)
    level = int(cfg_get(config, "obsidian.coach.brief_level", 3))
    block = render_brief(brief)
    if heading != BRIEF_HEADING:
        block = block.replace(f"### {BRIEF_HEADING}", f"{'#' * level} {heading}", 1)
    upsert_section(vault, note.rel, heading, block, level=level, after="Study Log")
    return {"note": note.rel, "path": str(note.path), "brief": dict(brief)}


def weekly_review(vault: Path, config: Optional[Mapping[str, Any]] = None, *,
                  day: Optional[date] = None) -> Dict[str, Any]:
    """A Sunday-style review note: what stuck, what slipped, what is next."""
    config = config or {}
    day = day or date.today()
    report = retention_report(vault, config)
    exams = exam_countdown(vault, config)
    week_start = day - timedelta(days=day.weekday())
    week_end = week_start + timedelta(days=6)

    lines = [f"# Weekly review — {week_start.isoformat()} → {week_end.isoformat()}", "",
             f"_Written by JARVIS on {datetime.now().isoformat(timespec='minutes')}_", "",
             "## Retention", "",
             f"- Reviews in the last {report['totals']['window_days']} days: "
             f"**{report['totals']['reviews_in_window']}**",
             f"- Overall retention: **{report['totals']['retention']}%** "
             f"({report['totals']['lapses']} lapse(s) in {report['totals']['reviews']} reviews)",
             f"- Streak: **{report['stats'].get('streak', 0)} day(s)**", "",
             "| subject | cards | reviews | lapses | retention | last review |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in report["subjects"]:
        lines.append(f"| {row['subject']} | {row['cards']} | {row['reviews']} | "
                     f"{row['lapses']} | {row['retention']}% | {row['last_review'] or '—'} |")
    if not report["subjects"]:
        lines.append("| _nothing reviewed yet_ |  |  |  |  |  |")

    weak = report["weakest"][:5]
    lines += ["", "## Weakest topics", ""]
    lines += [f"- **{topic['subject']} · {topic['topic']}** — {topic['lapses']} lapse(s) in "
              f"{topic['reviews']} review(s) ({topic['retention']}% retention)"
              for topic in weak] or ["- _none yet_"]
    if report["stale"]:
        lines += ["", f"## Gone quiet", "",
                  "- " + ", ".join(report["stale"]) + " — no reviews in the window"]

    lines += ["", "## Next week", ""]
    if exams:
        for exam in exams[:3]:
            lines.append(f"- [ ] {exam['exam']} in {exam['days_left']} day(s) — "
                         f"keep to the plan ({exam['note']})")
    if weak:
        lines.append(f"- [ ] Drill {weak[0]['subject']} · {weak[0]['topic']} first")
    lines.append("- [ ] One card per fact: split anything with an “and” in it")
    lines += ["", f"[[02 Daily Notes/{day.isoformat()}]]"]

    rel = f"{WEEKLY_FOLDER}/Weekly review — {day.isoformat()}.md"
    note = write_note(vault, rel, "\n".join(lines), folder=WEEKLY_FOLDER,
                      tags=["review", "weekly"])
    return {"note": note.rel, "path": str(note.path), "week": [week_start.isoformat(),
                                                               week_end.isoformat()]}


# ── running it ──────────────────────────────────────────────────────────────
@dataclass
class CoachRun:
    """What one coach cycle did."""

    day: str
    headline: str = ""
    note: str = ""
    notified: str = ""
    spoken: str = ""
    weekly: str = ""
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="minutes"))

    def line(self) -> str:
        bits = [f"[{self.at}] {self.headline or 'brief written'}"]
        if self.note:
            bits.append(f"→ {self.note}")
        if self.weekly:
            bits.append(f"· weekly review → {self.weekly}")
        if self.notified:
            bits.append(f"· 🔔 {self.notified}")
        if self.spoken:
            bits.append(f"· 🔊 spoken")
        return " ".join(bits)


def _notify(title: str, message: str, *, speak_text: str = "") -> Tuple[str, str]:
    """Desktop notification + optional speech, through the tool layer."""
    notified = spoken = ""
    try:
        from ..communication.notification import send_notification

        result = send_notification.invoke({"title": title, "message": message})
        notified = "sent" if result.ok else (result.error or "failed")[:60]
    except Exception as exc:                        # pragma: no cover - platform
        notified = f"unavailable ({exc})"
    if speak_text:
        try:
            from ..communication.notification import speak as speak_tool

            result = speak_tool.invoke({"text": speak_text[:280]})
            spoken = "ok" if result.ok else (result.error or "failed")[:60]
        except Exception as exc:                    # pragma: no cover - platform
            spoken = f"unavailable ({exc})"
    return notified, spoken


def run_once(vault: Path, config: Optional[Mapping[str, Any]] = None, *,
             day: Optional[date] = None, write: bool = True, notify: bool = False,
             speak: bool = False, weekly: bool = False) -> CoachRun:
    """One cycle: build the brief, write it, optionally notify/speak/review."""
    config = config or {}
    day = day or date.today()
    brief = build_brief(vault, config, day=day)
    run = CoachRun(day=day.isoformat(), headline=brief["headline"])

    if write:
        written = write_brief(vault, config, brief=brief, day=day)
        run.note = written["note"]
        log.info(f"brief written to {written['note']}")

    if weekly and day.weekday() == 6:
        review = weekly_review(vault, config, day=day)
        run.weekly = review["note"]
        log.info(f"weekly review written to {review['note']}")

    if notify or speak:
        spoken_text = ""
        if speak:
            spoken_text = f"Revision: {brief['headline']}"
        run.notified, run.spoken = _notify("JARVIS study coach", brief["headline"],
                                           speak_text=spoken_text)
    return run


def watch(vault: Path, config: Optional[Mapping[str, Any]] = None, *,
          times: str | Sequence[str] = DEFAULT_TIMES, notify: bool = True,
          speak: bool = False, weekly: bool = True, write: bool = True,
          iterations: Optional[int] = None, sleep: Callable[[float], None] = _time.sleep,
          now: Optional[Callable[[], datetime]] = None,
          on_run: Optional[Callable[[CoachRun], None]] = None) -> List[CoachRun]:
    """Sleep until each scheduled time, then run a cycle.

    ``iterations`` and ``sleep``/``now`` exist so the loop can be tested without
    waiting for the clock — in normal use it runs until interrupted.
    """
    clock = now or datetime.now
    schedule = parse_times(times)
    runs: List[CoachRun] = []
    log.info(f"study coach watching: {', '.join(schedule)}"
             + (f" ({iterations} cycle(s))" if iterations else ""))
    while True:
        target = next_occurrence(schedule, now=clock())
        wait = max(0.0, (target - clock()).total_seconds())
        log.info(f"next brief at {target.strftime('%Y-%m-%d %H:%M')} "
                 f"({wait / 60:.1f} min away)")
        sleep(wait)
        run = run_once(vault, config, day=target.date(), write=write, notify=notify,
                       speak=speak, weekly=weekly)
        runs.append(run)
        if on_run:
            on_run(run)
        if iterations is not None and len(runs) >= iterations:
            break
    return runs


def coach_status(vault: Path, config: Optional[Mapping[str, Any]] = None) -> str:
    """One paragraph for ``jarvis coach status`` and the status tool."""
    from ...config import get as cfg_get

    config = config or {}
    times = parse_times(cfg_get(config, "obsidian.coach.times", list(DEFAULT_TIMES)))
    report = retention_report(vault, config)
    upcoming = next_occurrence(times)
    lines = [f"**Coach:** next brief {upcoming.strftime('%a %H:%M')} "
             f"(times: {', '.join(times)})",
             f"**Retention:** {report['totals']['retention']}% over "
             f"{report['totals']['reviews']} review(s) · "
             f"{report['totals']['reviews_in_window']} in the last "
             f"{report['totals']['window_days']} day(s)",
             f"**Streak:** {report['stats'].get('streak', 0)} day(s) · "
             f"{report['stats'].get('tracked', 0)} card(s) tracked"]
    if report["stale"]:
        lines.append(f"**Gone quiet:** {', '.join(report['stale'])}")
    weak = report["weakest"][:3]
    if weak:
        lines.append("**Weakest topics:**")
        for topic in weak:
            lines.append(f"- {topic['subject']} · {topic['topic']} — "
                         f"{topic['lapses']} lapse(s) / {topic['reviews']} review(s) "
                         f"({topic['retention']}%)")
    return "\n".join(lines)


__all__ = ["BRIEF_HEADING", "DEFAULT_TIMES", "CoachRun", "TopicStats", "build_brief",
           "coach_status", "next_occurrence", "parse_times", "render_brief",
           "retention_report", "run_once", "subject_of", "topic_stats", "watch",
           "weak_topics", "weekly_review", "write_brief"]
