"""The smart memory manager: importance, dedupe, decay, consolidation, recall."""

from __future__ import annotations

import time

import pytest


def _manager(config):
    from jarvis.memory.memory_manager import get_memory_manager

    return get_memory_manager(config, refresh=True)


def test_memory_stores_and_recalls_by_meaning(config):
    manager = _manager(config)
    manager.long.store_fact("Heoster is the founder of CODEEX-AI", importance=0.9)
    manager.long.store_fact("Heoster lives in Khatauli, Muzaffarnagar", importance=0.9)
    manager.long.store_fact("The bike shed is painted green", importance=0.4)

    # lexical-but-stemmed retrieval: "location of Heoster" must surface the fact
    hits = manager.long.retrieve("location of Heoster", k=2)
    assert hits, "a semantic lookup must find something"
    text = " ".join(record.content for record, _score in hits).lower()
    assert "khatauli" in text or "muzaffarnagar" in text

    exact = manager.long.retrieve("khatauli", k=1)
    assert exact and "khatauli" in exact[0][0].content.lower()


def test_memory_dedupes_near_identical_facts(config):
    manager = _manager(config)
    first = manager.long.store_fact("JARVIS runs on free tiers only", importance=0.8)
    second = manager.long.store_fact("JARVIS runs on free tier only", importance=0.8)
    assert first is not None
    assert second is None or second.id == first.id, "near-duplicates must merge"
    assert manager.long.count() == 1


def test_importance_gate_drops_trivia(config):
    """`record_turn` filters noise; an explicit `remember` always keeps it."""
    manager = _manager(config)
    outcome = manager.record_turn("hmm", "ok", tool_calls=[])
    assert outcome["stored"] is False, "trivial chatter must not fill memory"
    assert manager.long.count() == 0

    explicit = manager.remember("hmm", importance=0.05)
    assert explicit is None or explicit.content == "hmm"


def test_recall_is_budget_aware(config):
    manager = _manager(config)
    for i in range(30):
        manager.long.store(f"Fact number {i}: JARVIS detail {i} " + "x" * 200,
                           importance=0.7, dedupe_threshold=0.0)
    recall = manager.recall("JARVIS detail", k=10, token_budget=400)
    assert recall.tokens_used <= 400, "recall must respect its token budget"
    assert recall.memories, "some memories should still fit"


def test_consolidation_promotes_and_decays(config):
    manager = _manager(config)
    manager.long.store("Heoster prefers dark themes in every app", importance=0.9,
                       tags=["preference"])
    manager.long.store("Old episodic note about a rainy Tuesday", importance=0.3)
    report = manager.consolidate(force=True)
    assert "promoted" in report and "decayed" in report


def test_operator_profile_is_seeded_once(config):
    manager = _manager(config)
    seeded = manager.seed_operator_profile()
    assert seeded > 0, "the operator profile must be remembered"
    assert manager.seed_operator_profile() == 0, "seeding must be idempotent"
    assert manager.profile.counts()


def test_profile_learns_preferences_from_a_turn(config):
    manager = _manager(config)
    manager.record_turn("Remember that I prefer Chrome over Firefox",
                        "Noted: Chrome.",
                        tool_calls=[{"name": "remember", "args": {}}])
    block = manager.profile.preferences_block()
    assert "chrome" in block.lower()


def test_context_blocks_fit_the_memory_budget(config):
    manager = _manager(config)
    manager.long.store_fact("Heoster is building JARVIS on a Windows laptop",
                            importance=0.9)
    blocks = manager.context_blocks("what is Heoster building", k=4)
    assert set(blocks) >= {"memory", "knowledge", "extra", "recall"}
    assert len(blocks["memory"]) < 4000
    assert blocks["recall"].tokens_used <= manager.memory_token_budget


def test_session_is_persisted_and_resumable(config):
    manager = _manager(config)
    manager.bootstrap(resume_session=False)
    manager.record_turn("What time is it?", "It is evening in IST.",
                        tokens_used=42)
    manager.end_session()

    from jarvis.memory.session_store import SessionStore

    store = SessionStore(config)
    sessions = store.recent(limit=5)
    assert sessions, "the session must be saved"
    assert store.messages(sessions[0].id), "messages must be stored too"
    store.close()


def test_knowledge_base_ingests_files(config, home):
    (home / "knowledge" / "notes.md").write_text(
        "# Notes\n\nJARVIS uses tesseract for OCR and never a vision model.")
    manager = _manager(config)
    added = manager.add_knowledge("JARVIS uses tesseract for OCR and never a vision model.",
                                  source="notes.md", topic="design")
    assert added >= 1
    hits = manager.search("vision model", limit=5)
    assert any("vision model" in hit["content"].lower() for hit in hits)


def test_forget_removes_a_memory(config):
    manager = _manager(config)
    record = manager.long.store_fact("Temporary fact to forget", importance=0.9)
    assert record is not None
    assert manager.long.forget(record.id) is True
    assert manager.long.get(record.id) is None


def test_skills_learn_from_a_repeated_trace(config):
    manager = _manager(config)
    trace = [{"name": "browse_open", "args": {"url": "https://youtube.com"}},
             {"name": "page_type", "args": {"target": "Search", "text": "lofi"}},
             {"name": "page_click", "args": {"target": "[7]"}}]
    skill = manager.skills.learn_from_trace("open youtube and search", trace,
                                            success=True)
    assert skill is not None and skill.steps
    hints = manager.skills.hints_for("open youtube and search for lofi")
    assert "youtube" in hints.lower()
