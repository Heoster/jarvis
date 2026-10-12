"""Regression: a normal chat message must not open Chrome.

Three things used to cause a browser launch on ordinary turns:

* the browser playbook told the model to "launch Chrome first" on every turn;
* keyword matching was substring-based, so words such as "display" or "happy"
  bound the browser/screen toolboxes;
* the screen engine spawned a new Chrome window each time it was (re)started.
"""

from __future__ import annotations

import pytest


def test_browser_playbook_does_not_tell_the_model_to_launch_chrome_first():
    from jarvis.core.system_prompt import PLAYBOOK_INDEX, PLAYBOOKS

    first_step = PLAYBOOKS["browser"][0]
    assert not first_step.lower().startswith("launch")
    assert "browser_launch" not in first_step
    assert "ONLY" in first_step
    assert "browser_launch" not in PLAYBOOK_INDEX["browser"].split("→")[0]


@pytest.mark.parametrize("message", [
    "hello, how are you",
    "the display is happy today",
    "what is 2 + 2",
    "show my password policy notes",
    "remember that I like tea",
])
def test_ordinary_messages_do_not_bind_browser_or_screen(config, message):
    from jarvis.core.brain import Brain

    brain = Brain(config)
    categories = brain.categories_for(message)
    assert "browser" not in categories[:2], categories
    assert "screen" not in categories[:2], categories


def test_explicit_browser_request_still_binds_browser(config):
    from jarvis.core.brain import Brain

    brain = Brain(config)
    assert brain.categories_for("open youtube and play lofi")[0] == "browser"
    assert brain.categories_for("open chrome and search cats")[0] in ("browser", "search")


def test_keyword_matching_is_whole_word_with_suffixes():
    from jarvis.core.brain import _keyword_hit

    assert _keyword_hit("open", "i opened the file")
    assert _keyword_hit("tab", "switch to the next tabs")
    assert not _keyword_hit("tab", "this is a table")
    assert not _keyword_hit("play", "the display works")
    assert not _keyword_hit("app", "i am happy")
    assert not _keyword_hit("word", "my password")


def test_screen_engine_reuses_its_window_instead_of_spawning_another(config, monkeypatch):
    from jarvis.tools.browser import harness as harness_module

    launches = []

    class FakeProcess:
        pid = 4242
        port = 9999

        def is_running(self):
            return True

    def fake_launch(*args, **kwargs):
        launches.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(harness_module, "launch_chrome", fake_launch)
    h = harness_module.BrowserHarness(config)
    engine = harness_module.ScreenOnlyEngine(h)
    engine.start()
    engine.stop()
    engine.start()                       # e.g. the engine is restarted after a failure
    other = harness_module.ScreenOnlyEngine(h)
    other.start()
    assert len(launches) == 1, "Chrome must be opened once, not on every restart"
    assert other.process is engine.process


def test_chat_turn_does_not_call_the_browser_harness(config, monkeypatch):
    """End-to-end: a plain message never reaches the Chrome launcher."""
    import asyncio

    from jarvis.core.orchestrator import Orchestrator
    from jarvis.tools.browser import terminal_launcher
    from jarvis.tools.registry import ToolRegistry

    calls = []
    monkeypatch.setattr(terminal_launcher, "launch_chrome",
                        lambda *a, **k: calls.append(a) or None)

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    orchestrator = Orchestrator(config, registry=registry)
    orchestrator.initialize(ingest_knowledge=False, resume_session=False)

    bound = orchestrator.brain.select_relevant_tools("hello, how are you",
                                                     registry.get_all_tools())
    assert not [t for t in bound if getattr(t, "category", "") == "browser"]

    async def run():
        async for _event in orchestrator.process("hello, how are you"):
            pass

    asyncio.run(run())
    assert calls == []
    orchestrator.shutdown()
