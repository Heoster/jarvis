"""The built-in workflows (skills/builtin_workflows.json) and what the agent learns.

A workflow that names a tool or argument that does not exist would silently
send the model down a dead end, so every step is checked against the real
tool schemas.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKFLOW_FILE = Path(__file__).resolve().parents[1] / "skills" / "builtin_workflows.json"


@pytest.fixture(scope="module")
def workflows():
    return json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tool_params():
    """tool name -> set of accepted argument names, from the live registry."""
    from jarvis.config import load_config
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(load_config())
    params = {}
    for tool in registry.get_all_tools():
        schema = tool.schema() if callable(getattr(tool, "schema", None)) else tool.schema
        function = schema.get("function", schema) if isinstance(schema, dict) else {}
        params[tool.name] = set((function.get("parameters") or {}).get("properties", {}))
    return params


def test_there_are_between_10_and_20_workflows(workflows):
    assert 10 <= len(workflows) <= 20
    names = [w["name"] for w in workflows]
    assert len(names) == len(set(names))


def test_every_step_names_a_real_tool_with_real_arguments(workflows, tool_params):
    problems = []
    for workflow in workflows:
        assert workflow["steps"], workflow["name"]
        for step in workflow["steps"]:
            tool = step["tool"]
            if not tool:  # a judgement step (no tool) is allowed
                continue
            if tool not in tool_params:
                problems.append(f"{workflow['name']}: unknown tool {tool}")
                continue
            unknown = set(step["args"]) - tool_params[tool]
            if unknown:
                problems.append(f"{workflow['name']}: {tool} has no argument {sorted(unknown)}")
    assert not problems, "\n".join(problems)


def test_library_loads_the_builtin_workflows(config, workflows):
    from jarvis.memory.skill_library import SkillLibrary

    library = SkillLibrary(config)
    for workflow in workflows:
        skill = library.get(workflow["name"])
        assert skill is not None, workflow["name"]
        assert skill.source == "builtin"


def test_a_typical_request_gets_the_matching_workflow(config):
    from jarvis.memory.skill_library import SkillLibrary

    library = SkillLibrary(config)
    names = [s.name for s in library.match("open yt and play a random song", limit=3)]
    assert "play_random_song" in names or "open_youtube_and_play" in names
    hints = library.hints_for("open yt and play a random song")
    assert "LEARNED SKILLS" in hints and "media_play" in hints


def test_judgement_steps_are_shown_but_never_treated_as_macros(workflows):
    from jarvis.memory.skill_library import Skill

    skill = Skill.from_dict(next(w for w in workflows if w["name"] == "search_web_and_answer"))
    assert "(no tool)" in skill.outline()
    assert not skill.is_macro()


def test_failed_turn_is_not_learned(config):
    from jarvis.memory.memory_manager import get_memory_manager

    manager = get_memory_manager(config, refresh=True)
    before = set(manager.skills.skills)
    manager.record_turn(
        "open yt and play some broken song",
        "(no answer produced)",
        tool_calls=[{"name": "media_play", "args": {"query": "x"}, "ok": False}] * 3)
    assert set(manager.skills.skills) == before


def test_working_turn_with_three_good_calls_is_learned(config):
    from jarvis.memory.memory_manager import get_memory_manager

    manager = get_memory_manager(config, refresh=True)
    manager.record_turn(
        "zz quartz frobnicate pipeline check",
        "Done: the pipeline ran and checked out.",
        tool_calls=[{"name": "read_file", "args": {"path": "a"}, "ok": True},
                    {"name": "read_file", "args": {"path": "b"}, "ok": True},
                    {"name": "write_file", "args": {"path": "c"}, "ok": True}])
    assert any("quartz" in name for name in manager.skills.skills)
