"""Obsidian tools exposed to the LLM (category: ``obsidian``).

The whole set is deliberately vault-only: JARVIS reads and writes markdown
files, so it works with Obsidian closed, with any Obsidian version, and with
any future fork.  Every tool reports a precise failure (``ok=False`` + reason)
when no vault is configured.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from ..base import failure, tool
from ...config import load_config
from ...utils.logger import get_logger
from ...utils.sanitizer import truncate
from .flashcards import (GRADES, SrsStore, cards_from_text, collect_cards, due_cards,
                         generate_cards_from_note, review_forecast, schedule)
from .prebuilt import enabled_prebuilt_ids
from .study import (DEFAULT_SUBJECTS, build_study_plan,
                    create_study_kit, exam_countdown, install_obsidian_plugin,
                    installed_plugins, jarvis_plugin_status, revision_queue,
                    study_report, subject_overview)
from .vault import (ObsidianError, all_tasks, daily_note, links_for, load_notes,
                    orphan_notes, read_note, resolve_vault, resolve_note, search_vault,
                    toggle_task, write_note)

log = get_logger("tools.obsidian.tools")


def _vault(config: Optional[Dict[str, Any]] = None, explicit: str = ""):
    """Resolve the vault or raise a message the model can act on."""
    return resolve_vault(config or load_config(), explicit=explicit)


# ── status & discovery ──────────────────────────────────────────────────────
@tool(category="obsidian", timeout=60)
def obsidian_status(vault_path: str = "") -> str:
    """Report the Obsidian vault JARVIS is connected to, plus study state.

    Use this first: it tells you the vault location, how many notes exist, which
    exams are coming up, how many flashcards are due, and whether the JARVIS
    Obsidian plugin is installed.

    Args:
        vault_path: Optional vault folder (overrides OBSIDIAN_VAULT / config)
    """
    try:
        vault = _vault(explicit=vault_path)
    except ObsidianError as exc:
        return failure(str(exc))
    report = study_report(vault, load_config())
    plugins = jarvis_plugin_status(vault)
    report += (f"\n\n**JARVIS plugin:** "
               f"{'enabled' if plugins['enabled'] else 'installed (enable in Obsidian)' if plugins['installed'] else 'not installed'}"
               f"\n_{plugins['path']}_")
    if not plugins["installed"]:
        report += "\nInstall it with `obsidian_plugins(install=true)`."
    return report


@tool(category="obsidian", timeout=60)
def obsidian_search(query: str, limit: int = 20, folder: str = "",
                    regex: bool = False) -> str:
    """Search every note in the Obsidian vault (full text, tags, or regex).

    Use this whenever the operator asks about their notes, class material,
    formulas, or "what did I write about X".

    Args:
        query: Text to find. Start with # for a tag (e.g. "#physics")
        limit: Maximum matches
        folder: Restrict to a folder, e.g. "01 Subjects/Physics"
        regex: Treat query as a regular expression
    """
    try:
        vault = _vault()
        hits = search_vault(vault, query, limit=max(1, min(limit, 100)), folder=folder,
                            regex=regex)
    except ObsidianError as exc:
        return failure(str(exc))
    if not hits:
        return f"No notes matched '{query}'. Try a broader word or obsidian_list_notes."
    lines = [f"**{len(hits)} match(es) for '{query}':**"]
    for hit in hits:
        lines.append(f"- [[{hit.note[:-3] if hit.note.endswith('.md') else hit.note}]]"
                     f":{hit.line} — {truncate(' '.join(hit.text.split()), 150)}")
    return "\n".join(lines)


@tool(category="obsidian", timeout=60)
def obsidian_list_notes(folder: str = "", tag: str = "", limit: int = 40,
                        sort: str = "modified") -> str:
    """List notes in the vault, newest first by default.

    Args:
        folder: Restrict to a folder ("01 Subjects/Physics"), empty = whole vault
        tag: Only notes carrying this tag (without the #)
        limit: How many to list
        sort: "modified", "title" or "words"
    """
    try:
        vault = _vault()
        notes = load_notes(vault, folder=folder)
    except ObsidianError as exc:
        return failure(str(exc))
    if tag:
        wanted = tag.lstrip("#").lower()
        notes = [note for note in notes
                 if any(t.lower() == wanted or t.lower().startswith(wanted + "/")
                        for t in note.tags)]
    if not notes:
        return failure(f"No notes found{' with tag #' + tag if tag else ''}"
                       f"{' in ' + folder if folder else ''}.")
    key = {"modified": lambda n: -n.mtime, "title": lambda n: n.title.lower(),
           "words": lambda n: -n.word_count}.get(sort, lambda n: -n.mtime)
    notes.sort(key=key)
    lines = [f"**{len(notes)} note(s)**{' in ' + folder if folder else ''}:"]
    for note in notes[:max(1, min(limit, 200))]:
        lines.append(f"- [[{note.rel[:-3]}]] · {note.metadata_line()}")
    if len(notes) > limit:
        lines.append(f"… and {len(notes) - limit} more")
    return "\n".join(lines)


@tool(category="obsidian", timeout=60)
def obsidian_read_note(note: str, max_chars: int = 6000) -> str:
    """Read one note from the vault (by title, path, or [[wikilink]]).

    Args:
        note: Note reference, e.g. "Thermodynamics", "01 Subjects/Physics/Thermo.md"
        max_chars: Truncate the note body at this length
    """
    try:
        vault = _vault()
        loaded = read_note(vault, note)
    except ObsidianError as exc:
        return failure(str(exc))
    meta = ", ".join(f"{key}: {value}" for key, value in list(loaded.frontmatter.items())[:6])
    header = (f"**{loaded.title}** (`{loaded.rel}`)\n{loaded.metadata_line()}"
              + (f"\n_{meta}_" if meta else ""))
    body = loaded.body if len(loaded.body) <= max_chars else (
        loaded.body[:max_chars] + f"\n\n[…{len(loaded.body) - max_chars} more characters]")
    return f"{header}\n\n{body}"


# ── writing ─────────────────────────────────────────────────────────────────
@tool(category="obsidian", timeout=60)
def obsidian_write_note(note: str, content: str = "", mode: str = "write",
                        tags: str = "", folder: str = "", title: str = "") -> str:
    """Create or update a note in the vault (the way to save anything for later).

    Use it to write lecture notes, summaries, answers, formula sheets or todo
    lists the student asked you to keep.  Frontmatter (tags, title, dates) is
    maintained for you.

    Args:
        note: Note name or path ("Physics/Laws of Motion")
        content: Markdown to write
        mode: "write" (replace), "append" or "prepend"
        tags: Comma/space separated tags to add
        folder: Folder to create the note in when it does not exist yet
        title: Optional title for the frontmatter
    """
    try:
        vault = _vault()
        saved = write_note(vault, note, content, mode=mode,
                           tags=[t for t in tags.replace(",", " ").split() if t],
                           folder=folder, title=title)
    except ObsidianError as exc:
        return failure(str(exc))
    verb = {"write": "Saved", "append": "Appended to", "prepend": "Prepended to"}.get(
        (mode or "write").lower(), "Saved")
    return f"✅ {verb} [[{saved.rel[:-3]}]] ({saved.metadata_line()})"


@tool(category="obsidian", timeout=60)
def obsidian_daily_note(content: str = "", day: str = "", section: str = "Study log") -> str:
    """Open (create if needed) a daily note and optionally add to a section.

    Daily notes are the student's study log: every lecture, revision session and
    doubt lands there.

    Args:
        content: Text to add (markdown). Empty = just create/open the note
        day: Date as YYYY-MM-DD (default: today)
        section: Heading to append under, e.g. "Study log", "Revision", "Doubts"
    """
    from .vault import append_under_heading

    try:
        vault = _vault()
        config = load_config()
        when = date.fromisoformat(day) if day else date.today()
        note = daily_note(vault, day=when, config=config)
        if content:
            try:
                note = append_under_heading(vault, note.rel, section, content)
            except ObsidianError:
                note = append_under_heading(vault, note.rel, section, content,
                                            create_heading=True)
    except (ObsidianError, ValueError) as exc:
        return failure(str(exc))
    tail = f" under **{section}**" if content else ""
    return (f"✅ Daily note [[{note.rel[:-3]}]] ready{tail} "
            f"({note.word_count} words)")


@tool(category="obsidian", timeout=60)
def obsidian_tasks(complete: str = "", due_before: str = "", include_done: bool = False,
                   limit: int = 25) -> str:
    """List open tasks from every note, or tick one off.

    Tasks come from standard Obsidian checkboxes (``- [ ]``) including due dates
    (``📅 2027-03-01``), priorities and tags.

    Args:
        complete: "note.md:line" to mark that task done (line number from the list)
        due_before: Only tasks due on/before this date (YYYY-MM-DD)
        include_done: Include finished tasks in the listing
        limit: Maximum tasks to list
    """
    try:
        vault = _vault()
        if complete:
            reference, _, line_text = complete.rpartition(":")
            if not reference or not line_text.isdigit():
                return failure("Use the form \"note.md:12\" with the line number "
                               "shown in the task list.")
            task = toggle_task(vault, reference, int(line_text), done=True)
            return f"✅ Completed: {task.text} ({task.note}:{task.line})"
        tasks = all_tasks(vault, include_done=include_done, due_before=due_before)
    except ObsidianError as exc:
        return failure(str(exc))
    if not tasks:
        return "No open tasks found in the vault."
    today = date.today().isoformat()
    lines = [f"**{len(tasks)} open task(s):**"]
    for task in tasks[:max(1, min(limit, 100))]:
        flag = ""
        if task.due:
            flag = " ⏰ overdue" if task.due < today else f" (due {task.due})"
        lines.append(f"- [ ] {task.text}{flag} — `{task.note}:{task.line}`")
    return "\n".join(lines)


@tool(category="obsidian", timeout=60)
def obsidian_links(note: str = "") -> str:
    """Show a note's backlinks, outgoing links and broken links.

    With no note, lists knowledge gaps: notes nothing links to, and links that
    point at notes which do not exist yet.

    Args:
        note: Note reference (empty = vault-wide link health)
    """
    try:
        vault = _vault()
        if note:
            data = links_for(vault, note)
            return (f"**{data['note']}**\n"
                    f"Backlinks ({len(data['backlinks'])}): "
                    f"{', '.join('[[%s]]' % b[:-3] for b in data['backlinks']) or 'none'}\n"
                    f"Outgoing ({len(data['outgoing'])}): "
                    f"{', '.join('[[%s]]' % o for o in data['outgoing']) or 'none'}\n"
                    f"Broken ({len(data['broken'])}): "
                    f"{', '.join(data['broken']) or 'none'}")
        orphans = orphan_notes(vault)
        broken: List[str] = []
        for candidate in load_notes(vault):
            broken.extend(links_for(vault, candidate.rel)["broken"])
        lines = [f"**Orphan notes ({len(orphans)}):** nothing links to these"]
        lines.extend(f"- [[{rel[:-3]}]]" for rel in orphans[:20])
        unique_broken = sorted(set(broken))[:20]
        lines.append(f"\n**Missing notes ({len(unique_broken)}):** linked but not created")
        lines.extend(f"- [[{name}]]" for name in unique_broken)
        return "\n".join(lines)
    except ObsidianError as exc:
        return failure(str(exc))


# ── flashcards ──────────────────────────────────────────────────────────────
@tool(category="obsidian", timeout=90)
def obsidian_flashcards_add(note: str = "", cards: str = "", from_note: str = "",
                            folder: str = "03 Flashcards") -> str:
    """Add flashcards to a note — typed by you, or generated from an existing note.

    Two ways to use it:
      • ``cards`` = lines of "Question :: Answer" (what students write by hand)
      • ``from_note`` = a note reference; JARVIS mines definitions and bold terms
        and writes the cards into that note's Flashcards section.

    Args:
        note: Note to write cards into (created if missing)
        cards: Newline separated "Question :: Answer" pairs
        from_note: Generate cards from this existing note instead
        folder: Folder used when the note has to be created
    """
    from .vault import append_under_heading

    try:
        vault = _vault()
        if from_note:
            source = read_note(vault, from_note)
            generated = generate_cards_from_note(source.body)
            if not generated:
                return failure(f"No card-worthy content found in {source.rel}. Add "
                               "definition bullets like \"- **Pressure** — force per area\".")
            block = "\n".join(f"- {card.question} :: {card.answer}" for card in generated)
            append_under_heading(vault, source.rel, "Flashcards", block)
            return (f"✅ Added {len(generated)} generated card(s) to "
                    f"[[{source.rel[:-3]}]]\n\n{truncate(block, 900)}")
        pairs = [line.strip(" -•\t") for line in (cards or "").splitlines() if line.strip()]
        valid = [pair for pair in pairs if "::" in pair and pair.count("::") >= 1]
        if not valid:
            return failure("No cards given. Use lines like "
                           "\"What is the SI unit of pressure? :: pascal\".")
        target = note or f"Cards {datetime.now().strftime('%Y-%m-%d %H%M')}"
        block = "\n".join(f"- {pair}" for pair in valid)
        if note:
            try:
                source = read_note(vault, note)
            except ObsidianError:
                # the chosen note does not exist yet — create it with the section
                saved = write_note(vault, note, f"## Flashcards\n\n{block}\n",
                                   folder=folder, tags=["flashcard"])
            else:
                append_under_heading(vault, source.rel, "Flashcards", block)
                saved = read_note(vault, source.rel)
        else:
            saved = write_note(vault, target, f"## Flashcards\n\n{block}\n", folder=folder,
                               tags=["flashcard"],
                               frontmatter={"type": "flashcards", "tags": ["flashcard"]})
    except ObsidianError as exc:
        return failure(str(exc))
    return f"✅ Saved {len(valid)} card(s) to [[{saved.rel[:-3]}]]"


@tool(category="obsidian", timeout=90)
def obsidian_flashcards_due(limit: int = 12, include_new: bool = True,
                            folder: str = "") -> str:
    """List the flashcards due for revision right now (spaced repetition queue).

    This is the study loop: ask for due cards, quiz yourself, then report each
    answer with `obsidian_flashcards_review`.

    Args:
        limit: How many cards to show
        include_new: Include cards never reviewed yet
        folder: Restrict to a folder (e.g. "01 Subjects/Chemistry")
    """
    try:
        vault = _vault()
        store = SrsStore()
        cards = collect_cards(vault, folder=folder, store=store)
    except ObsidianError as exc:
        return failure(str(exc))
    if not cards:
        return ("No flashcards in the vault yet. Write \"Question :: Answer\" lines, or "
                "use obsidian_flashcards_add with from_note to generate them.")
    due = due_cards(cards, limit=max(1, min(limit, 50)), include_new=include_new)
    if not due:
        forecast = review_forecast(cards)
        return f"Nothing due right now — you are ahead. 🎉\n\n{forecast}"
    lines = [f"**{len(due)} card(s) to review:**", ""]
    for index, card in enumerate(due, 1):
        where = f" ({card.note})" if card.note else ""
        status = "new" if card.is_new else f"due {card.due}"
        lines.append(f"{index}. **Q:** {card.question}{where} _[{status}]_")
        lines.append(f"   **A:** ||{card.answer}||")
    lines.append("")
    lines.append("_Answer first, then record it:_ `obsidian_flashcards_review("
                 "note=\"…\", question=\"…\", grade=\"good|hard|easy|again\")`")
    return "\n".join(lines)


@tool(category="obsidian", timeout=60)
def obsidian_flashcards_review(note: str, question: str, grade: str = "good") -> str:
    """Record how well a card was recalled, and schedule the next review.

    The schedule follows SM-2: "again" repeats today, "hard" shortens the
    interval, "good" multiplies it by the card's ease, "easy" stretches further.

    Args:
        note: Note the card lives in
        question: The card's question (or its first words)
        grade: again | hard | good | easy
    """
    grade = (grade or "good").lower()
    if grade not in GRADES:
        return failure(f"grade must be one of {', '.join(GRADES)}")
    try:
        vault = _vault()
        store = SrsStore()
        source = read_note(vault, note)
        cards = cards_from_text(source.rel, source.body)
        needle = (question or "").strip().lower()
        match = next((card for card in cards
                      if needle and needle in card.question.lower()), None)
        if match is None and cards and not question:
            match = cards[0]
        if match is None:
            return failure(f"No card matching {question!r} in {source.rel}. "
                           "Use obsidian_flashcards_due to see the exact questions.")
        store.get(match)
        schedule(match, grade)
        store.update(match, grade)
    except ObsidianError as exc:
        return failure(str(exc))
    return (f"✅ [{grade}] {truncate(match.question, 70)} → next review {match.due} "
            f"(interval {match.interval:.0f}d, ease {match.ease:.2f}, "
            f"{match.state}, streak {store.streak()}d)")


@tool(category="obsidian", timeout=60)
def obsidian_revision_queue(limit: int = 12) -> str:
    """What to study right now: due cards + overdue tasks + #revision notes.

    Args:
        limit: Maximum items per section
    """
    try:
        vault = _vault()
        queue = revision_queue(vault, limit=max(1, min(limit, 40)))
    except ObsidianError as exc:
        return failure(str(exc))
    lines = ["**Revision queue**", ""]
    cards = queue["due_cards"]
    lines.append(f"### Cards ({len(cards)})")
    lines.extend(f"- {card.summary()}" for card in cards) if cards else lines.append("- none due")
    tasks = queue["tasks"]
    lines.append(f"\n### Tasks ({len(tasks)})")
    lines.extend(f"- [ ] {task.text} — `{task.note}:{task.line}`" for task in tasks) \
        if tasks else lines.append("- none")
    notes = queue["notes"]
    lines.append(f"\n### Notes tagged #revision ({len(notes)})")
    lines.extend(f"- [[{rel[:-3]}]]" for rel in notes) if notes else lines.append("- none")
    counts = queue["counts"]
    lines.append(f"\n_Streak {counts['streak']}d · {counts['reviews']} reviews recorded · "
                 f"{counts['tracked']} cards tracked_")
    return "\n".join(lines)


# ── student workflow ────────────────────────────────────────────────────────
@tool(category="obsidian", timeout=120, dangerous=True)
def obsidian_study_kit(subjects: str = "", overwrite_templates: bool = False) -> str:
    """Build a complete student vault: subject folders, templates, dashboard.

    Creates (only when missing — nothing existing is overwritten unless you ask):
    `01 Subjects/<subject>`, `99 Templates` (lecture note, formula sheet, exam
    plan, daily note, weekly review), `04 Exams`, `03 Flashcards`, `JARVIS Home`
    and a getting-started guide.

    Args:
        subjects: Comma separated subjects (default Physics, Chemistry, Maths,
                  English, Computer Science)
        overwrite_templates: Replace existing template files
    """
    try:
        vault = _vault()
        chosen = [s.strip() for s in subjects.split(",") if s.strip()] or DEFAULT_SUBJECTS
        result = create_study_kit(vault, subjects=chosen,
                                  overwrite_templates=overwrite_templates)
    except ObsidianError as exc:
        return failure(str(exc))
    created = result["created"]
    lines = [f"✅ Study kit ready in **{result['vault']}**",
             f"Subjects: {', '.join(chosen)}"]
    if created:
        lines.append(f"Created {len(created)} item(s):")
        lines.extend(f"- {item}" for item in created[:20])
    lines.append("\nNext: `obsidian_study_plan(exam_date=\"YYYY-MM-DD\")` to schedule "
                 "revision, or `obsidian_plugins(install=true)` for the JARVIS plugin.")
    return "\n".join(lines)


@tool(category="obsidian", timeout=180)
def obsidian_study_plan(exam_date: str, subjects: str = "", topics_per_day: int = 2,
                        session_minutes: int = 45, write_daily: bool = True) -> str:
    """Plan revision between today and an exam date, day by day.

    Reads each subject index (the `- [ ]` checklist of chapters) and spreads the
    topics across the remaining days, writing each day's targets into that day's
    daily note — then keeps the full plan in `04 Exams`.

    Args:
        exam_date: Exam date (YYYY-MM-DD)
        subjects: Comma separated subjects (default: all subject folders)
        topics_per_day: How many topics to schedule per day
        session_minutes: Study time per topic, written into the plan
        write_daily: Also write the targets into the daily notes
    """
    try:
        vault = _vault()
        chosen = [s.strip() for s in subjects.split(",") if s.strip()]
        plan = build_study_plan(vault, exam_date=exam_date, subjects=chosen,
                                topics_per_day=topics_per_day,
                                session_minutes=session_minutes,
                                write_daily=write_daily, config=load_config())
    except (ObsidianError, ValueError) as exc:
        return failure(str(exc))
    lines = [f"✅ Study plan for **{plan['exam_date']}** "
             f"({plan['days']} days, {plan['topics']} topics, {plan['sessions']} sessions)",
             f"Plan note: [[{plan['plan_note'][:-3]}]]",
             f"Daily notes updated: {len(plan['daily_notes'])}"]
    for day_key in sorted(plan["schedule"])[:5]:
        topics = ", ".join(f"{subject}: {topic}" for subject, topic in plan["schedule"][day_key])
        lines.append(f"- {day_key}: {truncate(topics, 150)}")
    if len(plan["schedule"]) > 5:
        lines.append(f"- … {len(plan['schedule']) - 5} more day(s) in the plan note")
    return "\n".join(lines)


@tool(category="obsidian", timeout=120)
def obsidian_exams(within_days: int = 120) -> str:
    """List upcoming exams and how many days are left before each.

    Args:
        within_days: Only show exams in the next N days
    """
    try:
        vault = _vault()
        exams = exam_countdown(vault, load_config(), within_days=max(1, within_days))
        rows = subject_overview(vault)
    except ObsidianError as exc:
        return failure(str(exc))
    if not exams:
        return ("No exam notes found. Create one with "
                "`obsidian_write_note(note=\"04 Exams/Physics Boards\", "
                "content=\"---\\nexam_date: 2027-03-01\\n---\")`.")
    lines = ["**Upcoming exams:**"]
    for exam in exams:
        lines.append(f"- **{exam['exam']}** in {exam['days_left']} day(s) "
                     f"({exam['date']}) — [[{exam['note'][:-3]}]]")
    if rows:
        lines.append("\n**Subjects:** " + " · ".join(
            f"{row['subject']} {row['notes']}n/{row['open_tasks']}t" for row in rows))
    return "\n".join(lines)


@tool(category="obsidian", timeout=90)
def obsidian_study_brief(write: bool = False, day: str = "", limit: int = 6) -> str:
    """Today's study brief: due cards, overdue tasks, weak topics, exams.

    This is the study coach.  It answers "what should I do right now?" with the
    data already in the vault, and (with ``write=true``) drops the brief into
    today's daily note under ``### JARVIS brief`` — refreshing it, never
    duplicating. Use it whenever the student asks what to study or revise.

    Args:
        write: Also save the brief into the daily note
        day: Target day (YYYY-MM-DD), defaults to today
        limit: Maximum items per section
    """
    from .coach import build_brief, render_brief, write_brief

    try:
        vault = _vault()
        config = load_config()
        target = date.fromisoformat(day) if day else None
        brief = build_brief(vault, config, day=target, limit=limit)
        text = render_brief(brief)
        if write:
            written = write_brief(vault, config, brief=brief, day=target)
            text += f"\n✅ Saved to [[{written['note']}]]"
        return text
    except ObsidianError as exc:
        return failure(str(exc))
    except ValueError as exc:
        return failure(str(exc))


@tool(category="obsidian", timeout=90)
def obsidian_weak_topics(limit: int = 5, days: int = 30) -> str:
    """Which subjects and notes the student keeps forgetting (retention analytics).

    Built from every review JARVIS has recorded: lapses, ease, overdue-ness and
    cards stuck in learning. Use it to recommend what to drill before an exam.

    Args:
        limit: How many weak topics to list
        days: Only count reviews from the last N days
    """
    from .coach import coach_status, weak_topics

    try:
        vault = _vault()
        config = load_config()
        report = weak_topics(vault, config, limit=limit, days=days)
        lines = [f"**Weak spots** (last {days} day(s) of reviews):"]
        if not report:
            lines.append("- nothing reviewed yet — no weak spots to report")
        for topic in report:
            if topic["reviews"]:
                lines.append(f"- **{topic['subject']} · {topic['topic']}** — "
                             f"{topic['lapses']} lapse(s) in {topic['reviews']} review(s) "
                             f"({topic['retention']}% retention), "
                             f"{topic['due']} due, ease {topic['ease']:.2f}")
                if topic.get("worst_question"):
                    lines.append(f"    ↳ worst card: _{topic['worst_question']}_")
            else:
                lines.append(f"- **{topic['subject']} · {topic['topic']}** — "
                             f"{topic['new']} unreviewed card(s), {topic['due']} due")
        lines.append("")
        lines.append(coach_status(vault, config).split("\n", 1)[-1])
        return "\n".join(lines)
    except ObsidianError as exc:
        return failure(str(exc))


@tool(category="obsidian", timeout=240)
def obsidian_plugins(action: str = "status", plugins: str = "", offline: bool = False,
                     tag: str = "", install: bool = False) -> str:
    """Obsidian plugins: what is installed, and install the prebuilt community ones.

    JARVIS can install the ready-to-run bundles of the free plugins a student
    needs (Dataview, Spaced Repetition, Tasks, Calendar, Templater, Git,
    Excalidraw, Kanban, Admonition, Syntax Highlight) straight into the vault —
    the same release files Obsidian's community store would give you, verified
    and enabled, no npm or build step.

    Args:
        action: status · list-prebuilt · install (JARVIS's own plugin) ·
            install-prebuilt · update (reinstall the newest release of every
            prebuilt plugin already in the vault)
        plugins: which prebuilt plugins — "all" (default) or a comma separated
            list of ids/names, e.g. "dataview, Tasks, spaced"
        offline: install only from bundles vendored in obsidian-plugin/prebuilt/
        tag: pin a specific release tag instead of the latest release
        install: shorthand for action="install" (kept for older callers)
    """
    from .prebuilt import (PluginFetchError, describe_prebuilt, install_prebuilt,
                           prebuilt_status, resolve_selection)

    wanted = (action or "status").strip().lower()
    if install and wanted == "status":
        wanted = "install"

    try:
        vault = _vault()
    except ObsidianError as exc:
        return failure(str(exc))

    if wanted in ("status", "list", "prebuilt", "list-prebuilt"):
        plugins_installed = installed_plugins(vault)
        status = jarvis_plugin_status(vault)
        try:
            rows = prebuilt_status(vault)
        except Exception as exc:                       # never let reporting fail
            rows = []
            log.warning(f"prebuilt status unavailable: {exc}")
        lines = [f"**Vault plugins** ({len(plugins_installed)} installed):"]
        for plugin in plugins_installed:
            flag = "enabled" if plugin.enabled else "disabled"
            lines.append(f"- {plugin.name} `{plugin.id}` {plugin.version} — {flag}")
        if not plugins_installed:
            lines.append("- none yet")
        lines.append(f"\n**JARVIS plugin:** "
                     f"{'✅ installed' if status['installed'] else 'not installed'} "
                     f"({'enabled' if status['enabled'] else 'disabled'}) — {status['path']}")
        if not status["installed"]:
            lines.append("↳ install with `obsidian_plugins(action=\"install\")`")
        lines.append("")
        lines.append(describe_prebuilt(rows))
        lines.append("\n→ install any of them: `obsidian_plugins(action=\"install-prebuilt\", "
                     "plugins=\"all\")`")
        return "\n".join(lines)

    if wanted in ("install", "jarvis"):
        try:
            result = install_obsidian_plugin(vault)
        except ObsidianError as exc:
            return failure(str(exc))
        return (f"✅ JARVIS plugin installed ({len(result['files'])} files) → "
                f"{result['plugin_dir']}\n↳ {result['restart_hint']}")

    if wanted not in ("install-prebuilt", "install_prebuilt", "prebuilt-install", "update"):
        return failure(f"unknown action '{action}' — use status, list-prebuilt, install, "
                       "install-prebuilt or update")

    if wanted == "update":
        selection, unknown = resolve_selection(plugins or "all")
        if not plugins:
            have = set(enabled_prebuilt_ids(vault))
            selection = [plugin for plugin in selection if plugin.id in have]
            if not selection:
                return ("Nothing to update — no prebuilt plugin is installed yet. "
                        "Install some with action=\"install-prebuilt\".")
    else:
        selection, unknown = resolve_selection(plugins or "all")

    if not selection:
        return failure(f"no matching plugin(s): {', '.join(unknown) or plugins!r} — "
                       "call action=\"list-prebuilt\" for the ids")

    try:
        result = install_prebuilt(vault, selection, enable=True, tag=tag, offline=offline)
    except ObsidianError as exc:
        return failure(str(exc))

    lines = []
    for item in result["installed"]:
        extra = f" · {item['source']}" + (f" {item['tag']}" if item["tag"] else "")
        flag = "enabled" if item["enabled"] else "installed (enable it in Obsidian)"
        lines.append(f"✅ {item['name']} v{item['version']} → {flag} ({extra})")
        if item.get("warning"):
            lines.append(f"   ↳ {item['warning']}")
    for item in result["failed"]:
        lines.append(f"⚠ {item['name']}: {item['error']}")
    if unknown:
        lines.append(f"⚠ unknown plugin(s): {', '.join(unknown)}")
    if result["installed"]:
        lines.append(f"↳ {result['restart_hint']}")
    if not result["installed"]:
        return failure("\n".join(lines) or "nothing was installed")
    return "\n".join(lines)


OBSIDIAN_TOOLS = [
    obsidian_status, obsidian_search, obsidian_list_notes, obsidian_read_note,
    obsidian_write_note, obsidian_daily_note, obsidian_tasks, obsidian_links,
    obsidian_flashcards_add, obsidian_flashcards_due, obsidian_flashcards_review,
    obsidian_revision_queue, obsidian_study_brief, obsidian_weak_topics,
    obsidian_study_kit, obsidian_study_plan, obsidian_exams, obsidian_plugins,
]
