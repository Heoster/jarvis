"""Student workflow on top of the vault: study kits, plans and plugins.

This module is what turns "JARVIS can read markdown" into "JARVIS is useful for
Class 11/12 (and any) exam prep":

* **study kit** — scaffolds a vault with subject folders, lecture/formula
  templates, a daily note layout, a revision tracker and a home dashboard.
* **study plan** — spreads topics across the days left before an exam and writes
  each day's targets into that day's daily note.
* **revision queue** — due flashcards + open tasks + notes tagged ``#revision``.
* **plugins** — reports which Obsidian community plugins a student should have,
  and installs JARVIS's own Obsidian plugin into the vault.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...config import get as cfg_get
from ...config import load_config
from .prebuilt import PREBUILT_PLUGINS
from ...utils.logger import get_logger
from .vault import ObsidianError, append_under_heading, daily_note, load_notes, resolve_vault
from .vault import write_note

log = get_logger("tools.obsidian.study")

#: plugins worth having as a student — the catalogue (with repos, so JARVIS can
#: install the prebuilt bundles) lives in :mod:`prebuilt`
RECOMMENDED_PLUGINS: List[Tuple[str, str, str]] = [
    (plugin.id, plugin.name, plugin.why) for plugin in PREBUILT_PLUGINS
]

DEFAULT_SUBJECTS = ["Physics", "Chemistry", "Maths", "English", "Computer Science"]
FOLDERS = ["00 Inbox", "01 Subjects", "02 Daily Notes", "03 Flashcards", "04 Exams",
           "05 Revision", "99 Templates", "Attachments"]


# ── plugins ─────────────────────────────────────────────────────────────────
@dataclass
class PluginInfo:
    id: str
    name: str
    version: str = ""
    enabled: bool = False
    installed: bool = False


def obsidian_config_dir(vault: Path) -> Path:
    return vault / ".obsidian"


def community_plugin_dir(vault: Path) -> Path:
    return obsidian_config_dir(vault) / "plugins"


def enabled_plugin_ids(vault: Path) -> List[str]:
    path = obsidian_config_dir(vault) / "community-plugins.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(f"community-plugins.json unreadable: {exc}")
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def installed_plugins(vault: Path) -> List[PluginInfo]:
    enabled = set(enabled_plugin_ids(vault))
    plugins: Dict[str, PluginInfo] = {}
    for folder in sorted(community_plugin_dir(vault).glob("*")):
        if not folder.is_dir():
            continue
        info = PluginInfo(id=folder.name, name=folder.name, enabled=folder.name in enabled,
                          installed=True)
        manifest = folder / "manifest.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                info.name = str(data.get("name") or folder.name)
                info.version = str(data.get("version") or "")
            except Exception:
                pass
        plugins[folder.name] = info
    for plugin_id in enabled:
        if plugin_id not in plugins:
            plugins[plugin_id] = PluginInfo(id=plugin_id, name=plugin_id, enabled=True,
                                            installed=False)
    return list(plugins.values())


def plugin_source_dir() -> Path:
    """Where JARVIS's Obsidian plugin lives inside this project."""
    return Path(__file__).resolve().parents[3] / "obsidian-plugin" / "jarvis"


def install_obsidian_plugin(vault: Path, *, source: Optional[Path] = None,
                            enable: bool = True) -> Dict[str, Any]:
    """Copy the JARVIS plugin into ``<vault>/.obsidian/plugins/jarvis``."""
    src = Path(source) if source else plugin_source_dir()
    if not src.is_dir():
        raise ObsidianError(f"plugin files not found at {src}")
    for required in ("manifest.json", "main.js"):
        if not (src / required).is_file():
            raise ObsidianError(f"plugin is incomplete: {src / required} is missing")

    destination = community_plugin_dir(vault) / "jarvis"
    destination.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for item in sorted(src.iterdir()):
        if item.is_dir() or item.name.startswith("."):
            continue
        shutil.copy2(item, destination / item.name)
        copied.append(item.name)

    enabled_now = False
    listing = obsidian_config_dir(vault) / "community-plugins.json"
    ids = enabled_plugin_ids(vault)
    if enable and "jarvis" not in ids:
        ids.append("jarvis")
        enabled_now = True
    if enable:
        obsidian_config_dir(vault).mkdir(parents=True, exist_ok=True)
        listing.write_text(json.dumps(ids, indent=2), encoding="utf-8")

    return {"ok": True, "plugin_dir": str(destination), "files": copied,
            "enabled": enabled_now or "jarvis" in ids,
            "restart_hint": "In Obsidian: Settings → Community plugins → turn off "
                            "Restricted mode, then enable “JARVIS”."}


def jarvis_plugin_status(vault: Path) -> Dict[str, Any]:
    folder = community_plugin_dir(vault) / "jarvis"
    manifest: Dict[str, Any] = {}
    if (folder / "manifest.json").is_file():
        try:
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    return {"installed": folder.is_dir(), "enabled": "jarvis" in enabled_plugin_ids(vault),
            "path": str(folder), "version": manifest.get("version", ""),
            "source": str(plugin_source_dir())}


# ── study kit ───────────────────────────────────────────────────────────────
TEMPLATES: Dict[str, str] = {
    "Lecture Note.md": """---
type: lecture
subject: 
topic: 
date: {{date}}
tags: [lecture]
---

# {{title}}

> [!info] Class details
> Teacher: 
> Chapter: 
> Source: NCERT / board / coaching

## Key ideas

- 

## Definitions & formulas

| Term | Meaning / Formula | Units |
| --- | --- | --- |
|  |  |  |

## Worked example

```text
Problem:
Solution:
```

## Doubts

- [ ] 

## Revision cards (JARVIS can fill these)

## Flashcards

""",
    "Formula Sheet.md": """---
type: formula-sheet
subject: 
tags: [formula, revision]
---

# {{title}} — formula sheet

| Formula | When to use | Watch out for |
| --- | --- | --- |
|  |  |  |

## Constants & values

## Common mistakes

## Flashcards

""",
    "Exam Plan.md": """---
type: exam-plan
tags: [exam, plan]
---

# {{title}} — exam plan

> Exam date: {{date}}

## Syllabus checklist

- [ ] 

## Day-by-day plan

## Mock tests

- [ ] Paper 1 — date:  — score: 
- [ ] Paper 2 — date:  — score: 

## Weak spots to revisit

""",
    "Daily Note.md": """# {{date}}

## Focus

- [ ] 

## Classes / lectures

## Study log

## Revision

## Notes & doubts

""",
    "Weekly Review.md": """---
type: weekly-review
tags: [review]
---

# Week of {{date}}

## What went well

## What slipped

- [ ] 

## Cards due / revision health

## Next week's plan

""",
}

HOME_NOTE = """---
type: dashboard
tags: [jarvis, home]
---

# JARVIS home

> [!tip] This vault is wired to JARVIS
> Run `python -m jarvis obsidian status` (or ask JARVIS in chat) to search notes,
> write daily entries, generate flashcards and plan revision.

## Subjects

{subjects}

## Today

- [[{today}]]

## Quick actions

```bash
python -m jarvis obsidian daily                  # today's note
python -m jarvis obsidian cards due              # what to revise now
python -m jarvis obsidian plan --exam 2027-03-01  # revision plan
python -m jarvis obsidian search "thermodynamics" # find anything
```

## Revision health

Run `python -m jarvis obsidian cards stats`.

<!-- Dataview blocks (install the Dataview plugin to see these live)

```dataview
TABLE file.mtime AS "Modified", subject AS "Subject"
FROM "01 Subjects"
WHERE type = "lecture"
SORT file.mtime DESC
LIMIT 10
```

```dataview
TASK
WHERE !completed AND due <= date(today) + dur(7 days)
SORT due ASC
```
-->
"""


def create_study_kit(vault: Path, *, subjects: Optional[Sequence[str]] = None,
                     overwrite_templates: bool = False) -> Dict[str, Any]:
    """Scaffold folders, templates, subject indexes and the home dashboard."""
    subjects = [s.strip() for s in (subjects or DEFAULT_SUBJECTS) if s.strip()]
    created: List[str] = []

    for folder in FOLDERS:
        path = vault / folder
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(folder + "/")

    for subject in subjects:
        subject_dir = vault / "01 Subjects" / subject
        subject_dir.mkdir(parents=True, exist_ok=True)
        index = subject_dir / f"{subject}.md"
        if not index.exists():
            index.write_text(
                f"---\ntype: subject\ntags: [subject, {subject.lower().replace(' ', '-')}]\n"
                f"---\n\n# {subject}\n\n"
                f"## Chapters\n\n- [ ] \n\n## Notes\n\n"
                f"## Formula sheets\n\n## Flashcards\n",
                encoding="utf-8")
            created.append(f"01 Subjects/{subject}/{subject}.md")

    for name, body in TEMPLATES.items():
        target = vault / "99 Templates" / name
        if target.exists() and not overwrite_templates:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        created.append(f"99 Templates/{name}")

    home = vault / "JARVIS Home.md"
    if not home.exists():
        links = "\n".join(f"- [[{subject}]]" for subject in subjects)
        home.write_text(HOME_NOTE.format(subjects=links, today=date.today().isoformat()),
                        encoding="utf-8")
        created.append("JARVIS Home.md")

    guide = vault / "00 Inbox" / "Start here — JARVIS + Obsidian.md"
    if not guide.exists():
        guide.parent.mkdir(parents=True, exist_ok=True)
        guide.write_text(START_HERE, encoding="utf-8")
        created.append("00 Inbox/Start here — JARVIS + Obsidian.md")

    return {"ok": True, "vault": str(vault), "subjects": subjects, "created": created,
            "folders": FOLDERS}


START_HERE = """---
tags: [jarvis, guide]
---

# Start here — JARVIS + Obsidian

Your vault is now wired to JARVIS. Everything below is free and offline-first.

## 1 · Turn the JARVIS plugin on

Obsidian → Settings → **Community plugins** → turn off *Restricted mode* →
enable **JARVIS**. Then open the JARVIS panel from the ribbon (robot icon) and ask
questions about the note you are reading.

If the plugin is not listed, install it from the terminal:

```bash
python -m jarvis obsidian plugins --install
```

## 2 · Flashcards that schedule themselves

Write a card anywhere:

```markdown
- What is the SI unit of pressure? :: pascal (Pa)
```

or use cloze deletion:

```markdown
Photosynthesis happens in the {{c1::chloroplast}}.
```

Then:

```bash
python -m jarvis obsidian cards due        # what to revise today
python -m jarvis obsidian cards review 4 good
```

JARVIS keeps the schedule in `data/obsidian_srs.json`; the *Spaced Repetition*
plugin understands the same syntax if you prefer its UI.

## 3 · Study plan before an exam

```bash
python -m jarvis obsidian plan --exam 2027-03-01 --subjects Physics,Chemistry,Maths
```

Each remaining day's daily note gets its targets, and `04 Exams/` gets the plan.

## 4 · Ask JARVIS about your notes

In chat:

> search my vault for thermodynamics and make flashcards from the best note

JARVIS has vault tools: `obsidian_search`, `obsidian_read_note`,
`obsidian_write_note`, `obsidian_daily_note`, `obsidian_tasks`,
`obsidian_flashcards_*`, `obsidian_study_plan`, `obsidian_plugins`.

## 5 · Recommended plugins (all free)

Dataview · Spaced Repetition · Tasks · Calendar · Templater · Git · Excalidraw ·
Kanban. See `python -m jarvis obsidian plugins` for the why of each.
"""


# ── study plan ──────────────────────────────────────────────────────────────
def _topic_source_notes(vault: Path, subjects: Sequence[str]) -> Dict[str, List[str]]:
    """Chapter/topic names per subject, taken from the subject index notes."""
    topics: Dict[str, List[str]] = {}
    for subject in subjects:
        index = vault / "01 Subjects" / subject / f"{subject}.md"
        found: List[str] = []
        if index.is_file():
            body = index.read_text(encoding="utf-8")
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
                    name = stripped[5:].strip()
                    if name:
                        found.append(name)
        if not found:
            found = [f"{subject} revision"]
        topics[subject] = found
    return topics




def _upsert_daily_targets(vault: Path, day: date, block: str) -> str:
    """Write (or refresh) a day's targets inside its daily note.

    Running the planner twice must not pile up duplicate blocks, so a previous
    "Target for today" section is *replaced* rather than appended.
    """
    from .vault import render_frontmatter, split_frontmatter

    note = daily_note(vault, day=day)
    meta, body = split_frontmatter(note.raw)
    lines = body.splitlines()
    new_block = block.strip().splitlines()

    start = next((index for index, line in enumerate(lines)
                  if line.strip().lower() == "### target for today"), None)
    if start is not None:
        # the block owns its own "### Cards" sub-section, so only a level-2
        # heading ("## Revision" …) ends it
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].strip().startswith("## "):
                end = index
                break
        lines[start:end] = new_block + [""]
    else:
        insert_at = len(lines)
        section = next((index for index, line in enumerate(lines)
                        if line.strip().lower() == "## study log"), None)
        if section is not None:
            insert_at = len(lines)
            for index in range(section + 1, len(lines)):
                if lines[index].strip().startswith("## "):
                    insert_at = index
                    break
        lines[insert_at:insert_at] = ["", ""] + new_block + [""]

    note.path.write_text(render_frontmatter(meta) + "\n".join(lines).rstrip("\n") + "\n",
                         encoding="utf-8")
    return note.rel


def build_study_plan(vault: Path, *, exam_date: str, subjects: Sequence[str] = (),
                     start: Optional[date] = None, topics_per_day: int = 2,
                     session_minutes: int = 45, write_daily: bool = True,
                     config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Spread revision across the days left before ``exam_date``."""
    try:
        exam = date.fromisoformat(exam_date)
    except ValueError as exc:
        raise ObsidianError(f"exam date must be YYYY-MM-DD, got {exam_date!r}") from exc
    start = start or date.today()
    if exam < start:
        raise ObsidianError("exam date is in the past — pick the real exam date")

    chosen = [s.strip() for s in subjects if s.strip()]
    if not chosen:                       # use the folders that really exist
        root = vault / "01 Subjects"
        chosen = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    subjects = chosen or list(DEFAULT_SUBJECTS)
    topics = _topic_source_notes(vault, subjects)
    queue: List[Tuple[str, str]] = [(subject, topic)
                                    for subject in subjects for topic in topics[subject]]
    days = (exam - start).days
    if days == 0:
        days = 1

    per_day = max(1, min(topics_per_day, len(queue)))
    schedule: Dict[str, List[Tuple[str, str]]] = {}
    cursor = start
    remaining = list(queue)
    while remaining and cursor <= exam:
        slot = remaining[:per_day]
        remaining = remaining[per_day:]
        schedule[cursor.isoformat()] = slot
        cursor += timedelta(days=1)

    # final stretch: leave the last two days for whole-syllabus recall
    if days >= 3:
        for offset in (2, 1, 0):
            day = (exam - timedelta(days=offset)).isoformat()
            schedule[day] = [("All subjects", f"Full revision round {3 - offset}")]

    written: List[str] = []
    for day_key, slots in schedule.items():
        lines = ["### Target for today"]
        for subject, topic in slots:
            lines.append(f"- [ ] {subject} — {topic} ({session_minutes} min)")
        lines.append("")
        lines.append("### Cards")
        lines.append("- [ ] Review due flashcards (`jarvis obsidian cards due`)")
        block = "\n".join(lines)
        if write_daily:
            written.append(_upsert_daily_targets(vault, date.fromisoformat(day_key), block))

    plan_lines = [f"# {exam.strftime('%d %B %Y')} — study plan", "",
                  f"_Generated by JARVIS on {datetime.now().isoformat(timespec='minutes')}_",
                  f"_Days available: {days} · subjects: {', '.join(subjects)}_", "",
                  "## Schedule", ""]
    for day_key in sorted(schedule):
        plan_lines.append(f"### {day_key}")
        for subject, topic in schedule[day_key]:
            plan_lines.append(f"- [ ] {subject} — {topic}")
        plan_lines.append("")
    plan_note = write_note(vault, f"04 Exams/Plan — {exam.isoformat()}.md",
                           "\n".join(plan_lines), folder="04 Exams",
                           frontmatter={"type": "exam-plan", "exam_date": exam.isoformat(),
                                        "tags": ["exam", "plan"]})

    return {"ok": True, "exam_date": exam.isoformat(), "start": start.isoformat(),
            "days": days, "topics": len(queue), "sessions": sum(len(v) for v in schedule.values()),
            "daily_notes": written, "plan_note": plan_note.rel, "schedule": schedule}


def exam_countdown(vault: Path, config: Optional[Mapping[str, Any]] = None,
                   *, within_days: int = 120) -> List[Dict[str, Any]]:
    """Upcoming exams, read from ``04 Exams`` notes and ``exam_date`` metadata."""
    upcoming: List[Dict[str, Any]] = []
    today = date.today()
    for note in load_notes(vault, folder="04 Exams"):
        raw_date = str(note.field("exam_date") or note.field("date") or "").strip()
        if not raw_date:
            continue
        try:
            when = date.fromisoformat(raw_date[:10])
        except ValueError:
            continue
        delta = (when - today).days
        if delta < 0 or delta > within_days:
            continue
        upcoming.append({"exam": note.title, "date": when.isoformat(), "days_left": delta,
                         "note": note.rel})
    upcoming.sort(key=lambda item: item["days_left"])
    return upcoming


def revision_queue(vault: Path, *, limit: int = 15) -> Dict[str, Any]:
    """Everything that should be revised now: cards, tasks with due dates, #revision."""
    from .flashcards import SrsStore, collect_cards, due_cards

    store = SrsStore()
    cards = collect_cards(vault, store=store)
    due = due_cards(cards, limit=limit)
    tasks = []
    for note in load_notes(vault):
        for task in note.tasks:
            if task.done:
                continue
            if task.due and task.due <= date.today().isoformat():
                tasks.append(task)
            elif "revision" in [tag.lower() for tag in note.tags + task.tags]:
                tasks.append(task)
    return {"due_cards": due, "tasks": tasks[:limit],
            "notes": [note.rel for note in load_notes(vault)
                      if "revision" in [tag.lower() for tag in note.tags]][:limit],
            "counts": store.stats()}


def subject_overview(vault: Path) -> List[Dict[str, Any]]:
    """Per-subject: note count, words, last touched, open tasks."""
    rows: List[Dict[str, Any]] = []
    root = vault / "01 Subjects"
    if not root.is_dir():
        return rows
    for subject_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        notes = load_notes(vault, folder=str(subject_dir.relative_to(vault)))
        if not notes and not subject_dir.is_dir():
            continue
        words = sum(note.word_count for note in notes)
        open_tasks = sum(1 for note in notes for task in note.tasks if not task.done)
        last = max((note.mtime for note in notes), default=0.0)
        rows.append({
            "subject": subject_dir.name, "notes": len(notes), "words": words,
            "open_tasks": open_tasks,
            "last_touched": datetime.fromtimestamp(last).strftime("%Y-%m-%d") if last else "—",
        })
    return rows


def study_report(vault: Path, config: Optional[Mapping[str, Any]] = None) -> str:
    """The student-facing summary used by ``jarvis obsidian status``."""
    from .flashcards import SrsStore, collect_cards, review_forecast

    store = SrsStore()
    cards = collect_cards(vault, store=store)
    stats = store.stats()
    lines = [f"**Vault:** {vault}", f"**Notes:** {len(load_notes(vault))}",
             f"**Cards:** {len(cards)} in notes · {stats['tracked']} tracked"]
    today = date.today()
    due_now = len([card for card in cards
                   if not card.is_new and card.due_date() <= today])
    new_cards = len([card for card in cards if card.is_new])
    lines.append(f"**Due now:** {due_now} scheduled + {new_cards} new")
    lines.append(f"**Reviews done:** {stats['reviews']} · streak {stats['streak']} day(s)")
    exams = exam_countdown(vault, config)
    if exams:
        lines.append("**Exams:**")
        for exam in exams[:4]:
            lines.append(f"  · {exam['exam']} in {exam['days_left']} day(s) ({exam['date']})")
    rows = subject_overview(vault)
    if rows:
        lines.append("**Subjects:**")
        for row in rows:
            lines.append(f"  · {row['subject']}: {row['notes']} note(s), "
                         f"{row['words']} words, {row['open_tasks']} open task(s), "
                         f"last {row['last_touched']}")
    lines.append("")
    lines.append(review_forecast(cards))
    return "\n".join(lines)
