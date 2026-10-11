# JARVIS for students — the Obsidian workflow

Built for Heoster (Class 12 PCM, Maples Academy Khatauli) and for any student who
wants notes, revision and exam prep to run themselves. Everything here is free,
local and offline-first: the vault is a folder of markdown files.

## The loop

```
lecture / chapter  →  notes in Obsidian  →  flashcards  →  daily revision  →  exam plan
        ▲                                                          │
        └──────────────── JARVIS fills the gaps ───────────────────┘
```

1. **Capture** — write or paste the lecture into a subject note, or ask JARVIS:
   *"explain Newton's laws in the style of my lecture notes and append it to
   01 Subjects/Physics/Newton's Laws"*.
2. **Distil** — turn it into cards:
   *"make flashcards from that note"* → `obsidian_flashcards_add(from_note=…)`.
   Key definitions become `Term :: meaning`, important terms become cloze deletions.
3. **Revise** — every day: *"what is due for revision?"* →
   `obsidian_revision_queue` shows due cards, overdue tasks and `#revision` notes.
   Grade each card `again` / `hard` / `good` / `easy`; JARVIS reschedules it (SM-2).
4. **Plan** — before an exam: *"plan my revision for 1 November"* →
   `obsidian_study_plan` spreads the chapters in each subject index across the
   remaining days, writes each day's targets into that day's daily note, and keeps
   the master plan in `04 Exams`.
5. **Track** — *"how many days left for boards?"* → `obsidian_exams`; the study
   report shows streak, due cards and per-subject progress.
6. **Get pushed** — `python -m jarvis coach watch --at 19:30` writes the brief into
   today's daily note every evening, notifies you, and on Sundays leaves a review
   note with your retention table. You no longer have to remember to ask.

## Card syntax JARVIS writes (and reads)

```markdown
- What is the SI unit of pressure? :: pascal (Pa)
- Photosynthesis happens in the {{c1::chloroplast}}.

#flashcard The three laws of motion are stated by?
> Newton
```

All three formats are also understood by the **Spaced Repetition** community
plugin, so you can drill them in Obsidian's UI if you prefer. JARVIS keeps the
schedule in `data/obsidian_srs.json`; reviews are never lost.

## What the study kit creates

```
00 Inbox/              capture anything, sort later
01 Subjects/           Physics · Chemistry · Maths · English · CS (one index note each)
02 Daily Notes/        one note per day: focus, classes, study log, revision, doubts
03 Flashcards/         cards, cloze deletions, generated decks
04 Exams/              exam plans + countdowns (frontmatter: exam_date: YYYY-MM-DD)
05 Revision/           formula sheets and quick-recall sheets
99 Templates/          lecture note · formula sheet · exam plan · daily · weekly review
JARVIS Home.md         dashboard (optional Dataview blocks included)
```

Run it with `python -m jarvis obsidian kit --subjects "Physics,Chemistry,Maths"`.

## The JARVIS plugin inside Obsidian

`obsidian-plugin/jarvis/` is a real Obsidian community plugin (plain JS, no build
step). Install it with:

```bash
python -m jarvis obsidian plugins --install
# then: Settings → Community plugins → disable Restricted mode → enable "JARVIS"
```

Commands (Ctrl/Cmd+P): ask JARVIS · explain note · summarise note · explain
selection · make flashcards · revision queue · research topic · save to memory ·
test connection. The ribbon robot opens a chat panel where every answer can be
inserted into the note or turned into cards.

The plugin talks only to `127.0.0.1` (`python -m jarvis serve`), so nothing leaves
the machine unless a cloud provider was configured.

## Recommended free plugins — installed for you

Dataview · Spaced Repetition · Tasks · Calendar · Templater · Git · Excalidraw ·
Kanban · Admonition · Syntax Highlight. JARVIS installs their **prebuilt release
bundles** itself (the same files Obsidian's store would use — nothing is compiled
on your machine):

```bash
python -m jarvis obsidian plugins --install-prebuilt all
python -m jarvis obsidian plugins --install-prebuilt "dataview, tasks, spaced"
python -m jarvis obsidian plugins --update           # keep them current
```

Each install is verified (sha256 when the release publishes one) and recorded in
`.jarvis-provenance.json`. For a fully offline setup, run
`python scripts/fetch_prebuilt_plugins.py` once with internet, then add
`--offline` to every install.

## What the coach measures

```
python -m jarvis coach weak
  Physics · Optics — 4 lapse(s) in 4 review(s) (0% retention), 4 due
  Maths · Limits   — 0 lapse(s) in 1 review(s) (100% retention)
```

Weakness = lapses + low ease + how overdue + cards stuck in learning + subjects
that went quiet. It is computed from the review log JARVIS keeps, so grading
cards honestly is what makes the advice good. `jarvis coach brief` turns it into
"do this now"; `--write` puts it in the day's note.

## Habits that make it stick

- One card per fact; if a card has "and" in it, split it.
- Answer out loud before revealing; grading honestly is what makes SM-2 work.
- Ten minutes daily beats three hours on Sunday — the streak in `cards stats` is
  the metric that matters.
- Keep `#revision` on notes you want resurfacing; JARVIS pulls them into the queue.
- End every session with a daily-note line: *"add 'revised optics, weak in
  numericals' to today's study log"*.
- Let the coach nag you: `coach watch --at 19:30` is the difference between
  having a revision system and using one.
