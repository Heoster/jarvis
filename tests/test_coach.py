"""The study coach: retention analytics, the daily brief, and the scheduler.

Everything runs against a throwaway vault with a synthetic review history — no
Obsidian, no network, no model, and no waiting for the clock (``watch`` takes an
injected ``now``/``sleep``, which is why the daemon is testable at all).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.tools.obsidian import coach as coach_module
from jarvis.tools.obsidian import flashcards as cards_module
from jarvis.tools.obsidian import vault as vault_module

TODAY = date.today()


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture()
def vault(tmp_path) -> Path:
    root = tmp_path / "Vault"
    (root / "01 Subjects" / "Physics").mkdir(parents=True)
    (root / "01 Subjects" / "Chemistry").mkdir(parents=True)
    (root / "02 Daily Notes").mkdir(parents=True)
    (root / "04 Exams").mkdir(parents=True)
    (root / "05 Revision").mkdir(parents=True)

    (root / "01 Subjects" / "Physics" / "Optics.md").write_text(
        "# Optics\n\n- Focal length :: distance from the lens to the focus\n"
        "- What is dispersion? :: splitting of white light into colours\n"
        f"- [ ] Revise the lens formula 📅 {(TODAY - timedelta(days=2)).isoformat()}\n",
        encoding="utf-8")
    (root / "01 Subjects" / "Chemistry" / "Mole concept.md").write_text(
        "# Mole concept\n\n- Avogadro number :: 6.022 × 10²³\n", encoding="utf-8")
    (root / "04 Exams" / "Board exams.md").write_text(
        f"---\nexam_date: {(TODAY + timedelta(days=21)).isoformat()}\n"
        "tags: [exam]\n---\n\n# Board exams\n", encoding="utf-8")
    return root


@pytest.fixture()
def reviewed_vault(vault) -> Path:
    """The same vault, plus a history: Optics struggles, Chemistry is solid."""
    store = {
        "cards": {
            "o0": {"note": "01 Subjects/Physics/Optics.md", "question": "Lens formula?",
                   "due": (TODAY - timedelta(days=1)).isoformat(), "interval": 2.0,
                   "ease": 1.9, "reps": 4, "lapses": 3,
                   "last": (TODAY - timedelta(days=9)).isoformat(), "state": "learning"},
            "o1": {"note": "01 Subjects/Physics/Optics.md", "question": "Power of a lens?",
                   "due": (TODAY - timedelta(days=3)).isoformat(), "interval": 1.0,
                   "ease": 2.1, "reps": 3, "lapses": 1,
                   "last": (TODAY - timedelta(days=11)).isoformat(), "state": "learning"},
            "c0": {"note": "01 Subjects/Chemistry/Mole concept.md",
                   "question": "Avogadro number?", "due": (TODAY + timedelta(days=6)).isoformat(),
                   "interval": 12.0, "ease": 2.7, "reps": 6, "lapses": 0,
                   "last": TODAY.isoformat(), "state": "review"},
        },
        "reviews": 13,
        "streak_days": [TODAY.isoformat(), (TODAY - timedelta(days=1)).isoformat()],
        "log": [
            {"d": TODAY.isoformat(), "id": "c0", "g": "good",
             "n": "01 Subjects/Chemistry/Mole concept.md"},
            {"d": (TODAY - timedelta(days=9)).isoformat(), "id": "o0", "g": "again",
             "n": "01 Subjects/Physics/Optics.md"},
        ],
    }
    (vault / "srs.json").write_text(json.dumps(store), encoding="utf-8")
    return vault


@pytest.fixture()
def cfg(vault) -> dict:
    return {"obsidian": {
        "vault_path": str(vault),
        "subjects": ["Physics", "Chemistry", "Maths"],
        "daily_notes": {"folder": "02 Daily Notes", "format": "%Y-%m-%d", "template": ""},
        "study": {"session_minutes": 45},
        "coach": {"times": ["19:30"], "notify": False, "speak": False,
                  "weekly_review": True, "window_days": 30, "stale_days": 7,
                  "max_items": 6, "brief_level": 3},
        "srs": {"database": str(vault / "srs.json")},
    }}


# ── scheduling (pure) ───────────────────────────────────────────────────────
def test_parse_times_validates_sorts_and_defaults():
    assert coach_module.parse_times("19:30, 07:00") == ["07:00", "19:30"]
    assert coach_module.parse_times("19:30; 19:30") == ["19:30"]
    assert coach_module.parse_times([]) == ["19:30"]
    assert coach_module.parse_times("", default=["06:15"]) == ["06:15"]
    with pytest.raises(ValueError):
        coach_module.parse_times("half past seven")


def test_next_occurrence_is_strictly_in_the_future():
    morning = datetime(2026, 10, 11, 7, 0)
    assert coach_module.next_occurrence(["07:00"], now=morning) == \
        datetime(2026, 10, 12, 7, 0)                      # exactly now → tomorrow
    assert coach_module.next_occurrence(["07:01"], now=morning) == \
        datetime(2026, 10, 11, 7, 1)
    evening = datetime(2026, 10, 11, 23, 30)
    assert coach_module.next_occurrence(["07:00", "19:30"], now=evening) == \
        datetime(2026, 10, 12, 7, 0)                      # wraps past midnight
    with pytest.raises(ValueError):
        coach_module.next_occurrence([])


def test_subject_of_reads_the_folder_then_falls_back():
    assert coach_module.subject_of("01 Subjects/Physics/Optics.md") == "Physics"
    assert coach_module.subject_of("03 Flashcards/Cards.md") == "Flashcards"
    assert coach_module.subject_of("loose note.md", ["Maths"]) == "General"


# ── retention analytics ─────────────────────────────────────────────────────
def test_retention_report_finds_the_weak_subject(reviewed_vault, cfg):
    report = coach_module.retention_report(reviewed_vault, cfg)
    subjects = {row["subject"]: row for row in report["subjects"]}
    physics, chemistry = subjects["Physics"], subjects["Chemistry"]
    assert physics["lapses"] == 4 and physics["reviews"] == 7
    assert physics["retention"] == 43                 # 1 - 4/7
    assert chemistry["retention"] == 100
    assert report["totals"]["reviews_in_window"] == 2
    assert report["totals"]["retention"] == 69        # 1 - 4/13
    assert report["stale"] == ["Physics"]             # last review 9 days ago
    assert chemistry["due"] == 0 and physics["due"] == 2


def test_weak_topics_rank_trouble_first(reviewed_vault, cfg):
    topics = coach_module.weak_topics(reviewed_vault, cfg, limit=3)
    assert topics[0]["topic"] == "Optics"
    assert topics[0]["subject"] == "Physics"
    assert topics[0]["score"] > 5 and topics[0]["worst_question"] == "Lens formula?"
    assert all(topic["score"] <= topics[0]["score"] for topic in topics)


def test_retention_report_handles_a_vault_with_no_history(vault, cfg):
    report = coach_module.retention_report(vault, cfg)
    assert report["totals"]["reviews"] == 0
    assert report["totals"]["retention"] == 0
    assert report["subjects"] == [] and report["stale"] == []


# ── the brief ───────────────────────────────────────────────────────────────
def test_brief_mentions_cards_tasks_exams_and_the_weak_spot(reviewed_vault, cfg):
    brief = coach_module.build_brief(reviewed_vault, cfg, day=TODAY)
    assert brief["due_card_count"] >= 1
    assert brief["task_count"] == 1                       # the overdue lens-formula task
    assert brief["weak"][0]["topic"] == "Optics"
    assert brief["exams"][0]["days_left"] == 21
    assert brief["streak"] == 2

    text = coach_module.render_brief(brief)
    assert "### JARVIS brief" in text
    assert "Focal length" in text and "Revise the lens formula" in text
    assert "Physics · Optics" in text and "retention" in text
    assert "Board exams" in text and "21 day(s) left" in text


def test_brief_headline_counts_what_is_due(reviewed_vault, cfg):
    brief = coach_module.build_brief(reviewed_vault, cfg, day=TODAY)
    assert "card(s) due" in brief["headline"]
    assert "Optics" in brief["headline"] and "Board exams" in brief["headline"]


def test_write_brief_is_idempotent_and_lands_in_the_daily_note(reviewed_vault, cfg):
    first = coach_module.write_brief(reviewed_vault, cfg, day=TODAY)
    second = coach_module.write_brief(reviewed_vault, cfg, day=TODAY)
    note = Path(second["path"]).read_text(encoding="utf-8")

    assert first["note"] == f"02 Daily Notes/{TODAY.isoformat()}.md"
    assert note.count("### JARVIS brief") == 1            # refreshed, never duplicated
    assert "Focal length" in note
    # the brief sits between "## Study log" and "## Revision"
    assert note.index("## Study log") < note.index("### JARVIS brief")
    assert note.index("### JARVIS brief") < note.index("## Revision")


def test_write_brief_creates_the_daily_note_when_missing(vault, cfg):
    day = TODAY - timedelta(days=3)
    result = coach_module.write_brief(vault, cfg, day=day)
    assert Path(result["path"]).is_file()
    assert "### JARVIS brief" in Path(result["path"]).read_text(encoding="utf-8")


def test_weekly_review_note(reviewed_vault, cfg):
    result = coach_module.weekly_review(reviewed_vault, cfg, day=TODAY)
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert result["note"].startswith("05 Revision/Weekly review — ")
    assert "## Retention" in text and "| subject | cards | reviews |" in text
    assert "| Physics |" in text and "43%" in text
    assert "## Weakest topics" in text and "Optics" in text
    assert "## Next week" in text and "Board exams" in text


# ── running cycles ──────────────────────────────────────────────────────────
def test_run_once_writes_the_brief(reviewed_vault, cfg):
    run = coach_module.run_once(reviewed_vault, cfg, day=TODAY)
    assert run.day == TODAY.isoformat()
    assert run.note.endswith(f"{TODAY.isoformat()}.md")
    assert run.weekly == ""                              # not a Sunday request
    assert "card(s) due" in run.headline
    assert "🔔" not in run.line()


def test_run_once_writes_the_weekly_review_on_sundays(reviewed_vault, cfg):
    sunday = next(day for day in (TODAY + timedelta(days=offset) for offset in range(7))
                  if day.weekday() == 6)
    run = coach_module.run_once(reviewed_vault, cfg, day=sunday, weekly=True)
    assert run.weekly.startswith("05 Revision/Weekly review")
    assert Path(reviewed_vault / "05 Revision"
                / f"Weekly review — {sunday.isoformat()}.md").is_file()


def test_watch_runs_each_scheduled_cycle(reviewed_vault, cfg):
    clock = [datetime(2026, 10, 11, 19, 0)]               # 30 min before the brief
    slept: list[float] = []

    def now() -> datetime:
        return clock[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] = clock[0] + timedelta(seconds=seconds)  # the clock only moves via sleep()

    runs = coach_module.watch(reviewed_vault, cfg, times=["19:30"], notify=False,
                              iterations=2, sleep=sleep, now=now)
    assert len(runs) == 2
    assert slept[0] == pytest.approx(30 * 60)             # waited until 19:30
    assert slept[1] == pytest.approx(24 * 60 * 60)        # then a full day
    assert runs[0].day == "2026-10-11" and runs[1].day == "2026-10-12"
    assert (reviewed_vault / "02 Daily Notes" / "2026-10-11.md").is_file()
    assert (reviewed_vault / "02 Daily Notes" / "2026-10-12.md").is_file()


def test_coach_status_renders_the_summary(reviewed_vault, cfg):
    text = coach_module.coach_status(reviewed_vault, cfg)
    assert "Coach:" in text and "Retention:" in text and "Streak:" in text
    assert "Gone quiet" in text and "Physics" in text
    assert "Weakest topics" in text and "Optics" in text


# ── review history + tools ──────────────────────────────────────────────────
def test_reviews_are_logged_so_retention_can_be_measured(tmp_path):
    store = cards_module.SrsStore(path=tmp_path / "srs.json")
    card = cards_module.Card(id="c1", note="01 Subjects/Physics/Optics.md",
                             question="Lens formula?", answer="1/f = 1/v − 1/u")
    cards_module.schedule(card, "again")
    store.update(card, "again")
    cards_module.schedule(card, "good")
    store.update(card, "good")
    log = store._data["log"]
    assert [entry["g"] for entry in log] == ["again", "good"]
    assert log[0]["n"] == "01 Subjects/Physics/Optics.md"
    assert store.stats()["reviews"] == 2


def _tool_vault(config, home, vault: Path) -> Path:
    """Point the isolated test config at a vault with a synthetic history."""
    import shutil

    target = Path(config["obsidian"]["srs"]["database"])
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(vault / "srs.json", target)
    return vault


def test_review_tool_records_the_grade(config, home, reviewed_vault, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(_tool_vault(config, home, reviewed_vault)))

    from jarvis.config import load_config
    from jarvis.tools.obsidian.tools import obsidian_flashcards_review

    load_config(refresh=True)
    result = obsidian_flashcards_review.invoke({"note": "01 Subjects/Physics/Optics.md",
                                                "question": "Focal length",
                                                "grade": "good"})
    assert result.ok, result.error
    store = cards_module.SrsStore()
    assert any(entry.get("g") == "good" for entry in store._data.get("log", []))


def test_coach_tools_are_registered_and_work(config, home, reviewed_vault, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT", str(_tool_vault(config, home, reviewed_vault)))

    from jarvis.config import load_config
    from jarvis.tools.obsidian.tools import obsidian_study_brief, obsidian_weak_topics

    load_config(refresh=True)
    brief = obsidian_study_brief.invoke({"write": True})
    assert brief.ok, brief.error
    assert "JARVIS brief" in brief.content and "Saved to [[" in brief.content

    weak = obsidian_weak_topics.invoke({"limit": 3})
    assert weak.ok, weak.error
    assert "Optics" in weak.content and "lapse(s)" in weak.content

    # both are discoverable through the registry too
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    names = set(registry.names())
    assert {"obsidian_study_brief", "obsidian_weak_topics"} <= names


# ── staying dependency-light ────────────────────────────────────────────────
def test_frontmatter_works_without_pyyaml(vault, cfg, monkeypatch):
    """A bare Python install (no PyYAML) must still parse and write frontmatter."""
    from jarvis import config as config_module

    monkeypatch.setattr(config_module, "_HAS_YAML", False)
    monkeypatch.setattr(config_module, "_pyyaml", None, raising=False)

    meta, body = vault_module.split_frontmatter(
        "---\ntags: [physics, revision]\nexam_date: 2026-11-01\ndone: true\n---\n\n# Optics\n")
    assert meta == {"tags": ["physics", "revision"], "exam_date": "2026-11-01",
                    "done": True}
    assert body.strip() == "# Optics"

    rendered = vault_module.render_frontmatter(meta)
    assert "tags: [physics, revision]" in rendered and rendered.startswith("---\n")

    result = coach_module.write_brief(vault, cfg, day=TODAY)   # end-to-end on the fallback
    assert "### JARVIS brief" in Path(result["path"]).read_text(encoding="utf-8")
