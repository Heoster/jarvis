"""The universal system prompt: content, contracts and budget behaviour.

The prompt is the only part of JARVIS a small local model actually reads, so it
is tested like an API: the sections that must always be present, the tool names
that must match the registry, the memory rules, and the promise that a tiny
budget never produces a rules-free prompt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.core import system_prompt as sp


@pytest.fixture()
def env(config):
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    return sp.collect_environment(config, registry=registry), registry


# ── environment facts ───────────────────────────────────────────────────────
def test_environment_reports_the_live_build(env, config):
    environment, registry = env
    assert environment.os and environment.python
    assert environment.tools == len(registry.names())
    assert environment.tool_categories.get("browser", 0) >= 15
    from jarvis.config import get as cfg_get
    expected_model = cfg_get(config, "llm.model") or "llama3.2:3b"
    assert environment.model == expected_model, (
        f"environment.model={environment.model!r} != config model={expected_model!r}"
    )
    assert "stub" in environment.fallback_chain       # the isolated test chain
    lines = "\n".join(environment.lines())
    assert "Machine:" in lines and "Tools:" in lines and "fallback" in lines
    assert "short_term" not in lines              # stats are summarised, not dumped


def test_memory_line_summarises_tiers():
    line = sp._memory_line({"tiers": {"episodic": 12, "semantic": 3, "procedural": 0},
                            "knowledge": {"documents": 4}, "skills": {"total": 2},
                            "sessions": {"sessions": 7},
                            "short_term": {"message_count": 5}})
    assert "episodic 12" in line and "semantic 3" in line
    assert "knowledge docs 4" in line and "sessions 7" in line and "working 5 msg" in line
    assert sp._memory_line({}) == "empty"


# ── the prompt itself ───────────────────────────────────────────────────────
def test_prompt_contains_every_contract_section(env):
    environment, registry = env
    text = sp.build_universal_prompt(environment=environment, tools=registry.get_all_tools()[:9],
                                     memories="- favourite song: Kesariya",
                                     knowledge="- vault at /tmp/vault",
                                     session="- Session: abc (turn 2)")
    for section in ("## OPERATOR", "## RUNTIME", "## HOW YOU RESPOND", "## TOOL PROTOCOL",
                    "## MEMORY", "## PLAYBOOKS", "## HARD RULES", "## RECALLED MEMORY",
                    "## KNOWLEDGE BASE", "## SESSION CONTEXT"):
        assert section in text, section
    assert "Harsh Saini" in text and "Heoster" in text
    assert "recall / search_memory" in text.lower() or "search_memory" in text
    assert "Act, then verify, then answer" in text


def test_prompt_lists_only_the_bound_tool_names(env):
    environment, registry = env
    bound = [registry.get(name) for name in ("media_play", "page_type", "page_click")]
    text = sp.build_universal_prompt(environment=environment, tools=bound)
    line = next(line for line in text.splitlines() if "Bound this turn" in line)
    assert "media_play" in line and "page_type" in line
    assert "obsidian_study_kit" not in line              # not bound → not advertised
    assert "only these exist right now" in text


def test_prompt_handles_a_full_registry_without_listing_it(env):
    environment, registry = env
    text = sp.build_universal_prompt(environment=environment, tools=registry.get_all_tools())
    assert "all" in text and "registered tools" in text
    assert len(text) < 20000                              # the list must not explode


def test_playbooks_cover_the_requested_workflows(env):
    environment, _registry = env
    text = sp.build_universal_prompt(environment=environment)
    assert "**BROWSER**" in text and "**MEDIA**" in text
    assert "media_play" in text and "media_history" in text
    assert "page_tab_switch" in text and "new_tab=true" in text
    assert "URL injection" in text
    assert "paused (autoplay was blocked)" in text


# ── budgets ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("budget", [1200, 2500, 4000, 8000, 12000])
def test_any_budget_keeps_the_contract(env, budget):
    environment, _registry = env
    text = sp.build_universal_prompt(environment=environment, memories="- m" * 400,
                                     knowledge="- k" * 400, max_chars=budget)
    assert len(text) <= budget + 1
    assert "RULES" in text                                    # never rules-free
    assert "TOOLS" in text or "TOOL PROTOCOL" in text          # nor contract-free
    assert "never invent a tool result" in text.lower()


def test_larger_budget_includes_more_of_the_prompt(env):
    environment, registry = env
    small = sp.build_universal_prompt(environment=environment, tools=registry.get_all_tools()[:9],
                                      memories="- m" * 200, max_chars=4000)
    large = sp.build_universal_prompt(environment=environment, tools=registry.get_all_tools()[:9],
                                      memories="- m" * 200, max_chars=12000)
    assert len(large) > len(small)
    assert "## PLAYBOOKS" in large
    assert "## HARD RULES" in large and "RULES" in small


# ── brain wiring ────────────────────────────────────────────────────────────
def test_brain_uses_the_universal_prompt_by_default(config):
    from jarvis.core.brain import Brain
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    brain = Brain(config)
    brain.bind_tools(registry.get_all_tools())
    brain.attach_registry(registry)
    brain.set_session_context("- Session: test-session (turn 3)")

    text = brain.system_prompt(memory="- Heoster's favourite song is Kesariya")
    assert "## TOOL PROTOCOL" in text and "## PLAYBOOKS" in text
    assert "test-session" in text
    assert "Kesariya" in text
    assert "123 registered tools" in text or "registered tools" in text


def test_brain_can_still_use_the_short_persona_prompt(config):
    from jarvis.core.brain import Brain

    config["brain"] = dict(config.get("brain") or {})
    config["brain"]["system_prompt"] = "persona"
    brain = Brain(config)
    text = brain.system_prompt()
    assert "## TOOL PROTOCOL" not in text
    assert "JARVIS" in text and "## OPERATOR" in text


def test_messages_start_with_the_system_prompt(config):
    from jarvis.core.brain import Brain

    brain = Brain(config)
    brain.bind_tools([])
    messages = brain.build_messages([], "play my favourite song")
    assert messages[0].role == "system"
    assert "PLAYBOOKS" in str(messages[0].content) or "MEDIA" in str(messages[0].content)
    assert messages[-1].role in ("user", "human")
