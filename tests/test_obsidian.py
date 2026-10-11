"""Obsidian integration: vault notes, spaced repetition, study workflow, plugin.

Everything runs against a throwaway vault in ``tmp_path`` — no Obsidian, no
network, no model.  That is exactly how the tools behave on the operator's
machine, because a vault is only a folder of markdown files.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from jarvis.tools.obsidian import flashcards as cards_module
from jarvis.tools.obsidian import study as study_module
from jarvis.tools.obsidian import vault as vault_module


@pytest.fixture()
def vault(tmp_path, monkeypatch) -> Path:
    """A small vault with one study note, wired through OBSIDIAN_VAULT."""
    root = tmp_path / "Vault"
    (root / "01 Subjects" / "Physics").mkdir(parents=True)
    (root / "03 Flashcards").mkdir(parents=True)
    (root / "02 Daily Notes").mkdir(parents=True)
    (root / "04 Exams").mkdir(parents=True)
    (root / "01 Subjects" / "Physics" / "Newton's Laws.md").write_text(
        """---
tags: [physics, revision]
type: lecture
---

# Newton's Laws of Motion

- **Inertia** — the tendency of a body to resist changes in motion.
- **Momentum** — mass times velocity, p = m v.
- **Force** — the rate of change of momentum.

See [[Equation Sheet]] and [[Missing Note]] for details.
""", encoding="utf-8")
    (root / "03 Flashcards" / "Cards.md").write_text(
        """# Cards

- What is the SI unit of pressure? :: pascal (Pa)
- Water boils at {{c1::100 °C}} at sea level.

#flashcard The three laws are stated by?
> Newton

```markdown
- This line is documentation :: and must never become a card
```
""", encoding="utf-8")
    (root / "04 Exams" / "Boards.md").write_text(
        "---\nexam_date: %s\ntags: [exam, revision]\n---\n\n# Boards\n\n"
        "- [ ] Revise optics 📅 %s\n- [ ] Attempt mock paper 📅 %s\n"
        % ((date.today() + timedelta(days=30)).isoformat(),
           (date.today() - timedelta(days=1)).isoformat(),
           (date.today() + timedelta(days=9)).isoformat()), encoding="utf-8")
    monkeypatch.setenv("OBSIDIAN_VAULT", str(root))
    return root


# ── vault layer ───────────────────────────────────────────────────────────
def test_vault_discovery_and_missing_vault_message(vault, monkeypatch, home):
    assert vault_module.resolve_vault(explicit="") == vault.resolve()
    monkeypatch.delenv("OBSIDIAN_VAULT", raising=False)
    monkeypatch.chdir(home)                      # no vault anywhere near here
    with pytest.raises(vault_module.ObsidianError) as error:
        vault_module.resolve_vault(explicit="")
    assert "OBSIDIAN_VAULT" in str(error.value)


def test_note_parsing_reads_frontmatter_tags_links_and_title(vault):
    note = vault_module.read_note(vault, "Newton's Laws")
    assert note.title == "Newton's Laws of Motion"
    assert note.rel == "01 Subjects/Physics/Newton's Laws.md"
    assert "physics" in note.tags and "revision" in note.tags
    assert note.links == ["Equation Sheet", "Missing Note"]
    assert note.field("type") == "lecture"
    assert note.word_count > 20


def test_note_resolution_accepts_title_path_and_wikilink(vault):
    for reference in ("Newton's Laws", "01 Subjects/Physics/Newton's Laws.md",
                      "[[Newton's Laws]]", "01 Subjects/Physics/Newton's Laws"):
        assert vault_module.resolve_note(vault, reference).name == "Newton's Laws.md"
    with pytest.raises(vault_module.ObsidianError):
        vault_module.resolve_note(vault, "does not exist")


def test_write_read_append_and_prepend(vault):
    first = vault_module.write_note(vault, "01 Subjects/Physics/Unit 1", "Line A",
                                    tags=["physics"])
    assert first.rel == "01 Subjects/Physics/Unit 1.md"       # no folder doubling
    assert first.tags == ["physics"]

    vault_module.write_note(vault, "Unit 1", "Line B", mode="append")
    vault_module.write_note(vault, "Unit 1", "Line C", mode="prepend")
    body = vault_module.read_note(vault, "Unit 1").body
    assert body.index("Line C") < body.index("Line A") < body.index("Line B")

    created = vault_module.resolve_note(vault, "Fresh note", create=True,
                                        folder="03 Flashcards")
    assert created == vault / "03 Flashcards" / "Fresh note.md"


def test_append_under_heading_creates_and_reuses_sections(vault):
    vault_module.append_under_heading(vault, "Cards", "Flashcards", "- Q1 :: A1")
    vault_module.append_under_heading(vault, "Cards", "Flashcards", "- Q2 :: A2")
    body = vault_module.read_note(vault, "Cards").body
    assert body.count("## Flashcards") == 1
    assert "- Q1 :: A1" in body and "- Q2 :: A2" in body


def test_search_vault_by_text_tag_and_regex(vault):
    assert [hit.note for hit in vault_module.search_vault(vault, "momentum")], \
        "plain text search must find the physics note"
    tagged = vault_module.search_vault(vault, "#physics")
    assert any("Newton's Laws" in hit.note for hit in tagged)
    regex = vault_module.search_vault(vault, r"p\s*=\s*m\s*v", regex=True)
    assert regex and "Newton's Laws" in regex[0].note
    assert vault_module.search_vault(vault, "no-such-string-anywhere") == []


def test_links_backlinks_and_orphans(vault):
    data = vault_module.links_for(vault, "Newton's Laws")
    assert "Missing Note" in data["broken"]
    assert "Equation Sheet" in data["outgoing"]
    assert vault_module.orphan_notes(vault), "unlinked notes should be reported"


def test_tasks_list_and_complete(vault):
    tasks = vault_module.all_tasks(vault)
    assert tasks, "the exam note has open tasks"
    overdue = vault_module.all_tasks(vault, due_before=date.today().isoformat())
    assert overdue and overdue[0].due < date.today().isoformat()
    target = tasks[0]
    done = vault_module.toggle_task(vault, target.note, target.line, done=True)
    assert done.done is True
    assert all(not task.text == target.text for task in vault_module.all_tasks(vault))


def test_daily_note_creation_and_append(vault, config):
    note = vault_module.daily_note(vault, day=date(2026, 1, 5), config=config)
    assert note.rel == "02 Daily Notes/2026-01-05.md"
    assert "## Focus" in note.body
    updated = vault_module.append_under_heading(vault, note.rel, "Study log", "- physics")
    assert "- physics" in updated.body
    assert (vault / "02 Daily Notes" / "2026-01-05.md").is_file()


# ── flashcards ────────────────────────────────────────────────────────────
def test_cards_from_text_understands_every_format(vault):
    body = vault_module.read_note(vault, "Cards").body
    found = cards_module.cards_from_text("03 Flashcards/Cards.md", body)
    kinds = {card.kind for card in found}
    questions = " | ".join(card.question for card in found)
    assert "qa" in kinds and "cloze" in kinds and "flashcard-tag" in kinds
    assert "SI unit of pressure" in questions
    assert "100 °C" not in questions, "the cloze answer must stay hidden"
    assert "must never become a card" not in questions, "code fences are ignored"


def test_generate_cards_from_a_lecture_note(vault):
    note = vault_module.read_note(vault, "Newton's Laws")
    generated = cards_module.generate_cards_from_note(note.body)
    assert len(generated) >= 3
    joined = " ".join(card.question for card in generated)
    assert "What is Inertia?" in joined
    assert any("Momentum" in card.answer or "Momentum" in card.question
               for card in generated)


def test_scheduler_follows_sm2_and_persists(vault, home):
    store = cards_module.SrsStore()
    card = cards_module.Card(id="abc123", note="n.md", question="Q", answer="A")
    store.get(card)
    assert card.state == "new" and card.ease == 2.5

    cards_module.schedule(card, "good")
    assert card.interval == 1.0 and card.state == "review"
    assert card.due == (date.today() + timedelta(days=1)).isoformat()
    store.update(card)

    reloaded = cards_module.SrsStore().get(
        cards_module.Card(id="abc123", note="n.md", question="Q", answer="A"))
    assert reloaded.interval == 1.0 and reloaded.reps == 1

    cards_module.schedule(reloaded, "easy")
    assert reloaded.interval > 2.0 and reloaded.ease > 2.5
    cards_module.schedule(reloaded, "again")
    assert reloaded.state == "learning" and reloaded.lapses == 1
    stats = store.stats()
    assert stats["tracked"] >= 1 and stats["reviews"] >= 1 and stats["streak"] >= 1


def test_due_cards_and_forecast_ordering(vault, home):
    store = cards_module.SrsStore()
    collection = cards_module.collect_cards(vault, store=store)
    assert collection, "the fixture vault has cards"
    due = cards_module.due_cards(collection, limit=3)
    assert 1 <= len(due) <= 3
    forecast = cards_module.review_forecast(collection)
    assert date.today().isoformat() in forecast
    assert set(cards_module.next_review_dates(collection)) == {
        (date.today() + timedelta(days=offset)).isoformat() for offset in range(7)}


# ── study workflow ────────────────────────────────────────────────────────
def test_study_kit_scaffolds_and_is_idempotent(vault, config):
    result = study_module.create_study_kit(vault, subjects=["Physics", "Maths"])
    assert result["ok"] and "JARVIS Home.md" in result["created"]
    for folder in ("00 Inbox", "01 Subjects", "02 Daily Notes", "03 Flashcards",
                   "04 Exams", "05 Revision", "99 Templates"):
        assert (vault / folder).is_dir()
    for template in study_module.TEMPLATES:
        assert (vault / "99 Templates" / template).is_file()

    again = study_module.create_study_kit(vault, subjects=["Physics", "Maths"])
    assert again["created"] == [], "a second run must not duplicate anything"


def test_study_plan_writes_plan_and_daily_targets_once(vault, config):
    inventory = vault / "01 Subjects" / "Physics" / "Physics.md"
    inventory.write_text("# Physics\n\n## Chapters\n\n- [ ] Units and Measurements\n"
                         "- [ ] Laws of Motion\n- [ ] Thermodynamics\n", encoding="utf-8")
    exam = (date.today() + timedelta(days=10)).isoformat()
    plan = study_module.build_study_plan(vault, exam_date=exam, subjects=["Physics"],
                                         topics_per_day=2, config=config)
    assert plan["days"] == 10 and plan["topics"] == 3
    assert (vault / plan["plan_note"]).is_file()

    first = (vault / "02 Daily Notes" / f"{date.today().isoformat()}.md").read_text()
    assert "Units and Measurements" in first and "Target for today" in first
    study_module.build_study_plan(vault, exam_date=exam, subjects=["Physics"],
                                  topics_per_day=2, config=config)
    second = (vault / "02 Daily Notes" / f"{date.today().isoformat()}.md").read_text()
    assert second.count("### Target for today") == 1, "the planner must be idempotent"
    with pytest.raises(study_module.ObsidianError):
        study_module.build_study_plan(vault, exam_date="not-a-date", config=config)


def test_exam_countdown_and_revision_queue(vault, config):
    exams = study_module.exam_countdown(vault, config)
    assert exams and exams[0]["days_left"] == 30
    queue = study_module.revision_queue(vault, limit=5)
    assert queue["due_cards"], "new cards belong in the revision queue"
    assert queue["tasks"], "the overdue exam task belongs in the queue"
    assert "streak" in queue["counts"]


def test_study_report_mentions_the_essentials(vault, config):
    report = study_module.study_report(vault, config)
    assert "Vault" in report and "Cards" in report and "Exams" in report


# ── the plugin ────────────────────────────────────────────────────────────
def test_plugin_files_are_valid_and_installable(vault):
    source = study_module.plugin_source_dir()
    assert (source / "manifest.json").is_file()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "jarvis" and manifest["version"]
    assert (source / "main.js").is_file() and (source / "styles.css").is_file()

    result = study_module.install_obsidian_plugin(vault)
    installed = vault / ".obsidian" / "plugins" / "jarvis"
    assert result["ok"] and installed.is_dir()
    assert (installed / "main.js").is_file()
    assert "jarvis" in json.loads(
        (vault / ".obsidian" / "community-plugins.json").read_text())
    status = study_module.jarvis_plugin_status(vault)
    assert status["installed"] and status["enabled"]

    installed_list = study_module.installed_plugins(vault)
    assert any(plugin.id == "jarvis" and plugin.enabled for plugin in installed_list)
    assert len(study_module.RECOMMENDED_PLUGINS) >= 8


def test_plugin_javascript_is_at_least_syntactically_valid():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    main_js = study_module.plugin_source_dir() / "main.js"
    check = subprocess.run([node, "--check", str(main_js)],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


# ── tool layer ────────────────────────────────────────────────────────────
def test_obsidian_tools_are_registered(registry):
    names = {tool.name for tool in registry.get_by_category("obsidian")}
    assert len(names) >= 15
    assert {"obsidian_search", "obsidian_write_note", "obsidian_flashcards_due",
            "obsidian_study_plan", "obsidian_daily_note", "obsidian_plugins",
            "obsidian_revision_queue"} <= names


def test_tools_work_through_the_registry(registry, vault):
    search = registry.execute_sync("obsidian_search", {"query": "momentum"})
    assert search.ok and "Newton" in search.content

    written = registry.execute_sync("obsidian_write_note", {
        "note": "05 Revision/Formula sheet", "content": "- v = u + a t", "tags": "revision"})
    assert written.ok and "Formula sheet" in written.content

    daily = registry.execute_sync("obsidian_daily_note",
                                  {"content": "- [ ] Revise optics", "section": "Revision"})
    assert daily.ok and "Daily note" in daily.content

    added = registry.execute_sync("obsidian_flashcards_add", {
        "note": "03 Flashcards/Tool cards",
        "cards": "What is the unit of force? :: newton\nWhat is g? :: 9.8 m/s²"})
    assert added.ok and "2 card(s)" in added.content

    due = registry.execute_sync("obsidian_flashcards_due", {"limit": 5})
    assert due.ok and "Q:" in due.content

    reviewed = registry.execute_sync("obsidian_flashcards_review", {
        "note": "03 Flashcards/Tool cards", "question": "unit of force", "grade": "good"})
    assert reviewed.ok and "next review" in reviewed.content

    status = registry.execute_sync("obsidian_status", {})
    assert status.ok and "Vault" in status.content

    report = registry.execute_sync("obsidian_plugins", {})
    assert report.ok and "Recommended" in report.content


def test_missing_vault_fails_with_guidance(registry, monkeypatch, home):
    monkeypatch.delenv("OBSIDIAN_VAULT", raising=False)
    monkeypatch.chdir(home)
    result = registry.execute_sync("obsidian_status", {})
    assert result.ok is False
    assert "OBSIDIAN_VAULT" in (result.error or "")
