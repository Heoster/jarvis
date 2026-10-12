"""Tool calls must really happen: text-form calls are recovered, action requests
are nudged once, and a reply that claims an action with no tool run is flagged.

The model is scripted and the tool executor is stubbed, so nothing opens a
browser or writes memory.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.core.text_tool_calls import extract_text_tool_calls

BOUND = ["media_play", "calculate", "remember"]


# ── text-form tool calls ──────────────────────────────────────────────────
def test_tagged_call_is_recovered_and_removed_from_text():
    calls, text = extract_text_tool_calls(
        'Sure.\n<tool_call>{"name": "media_play", "arguments": {"query": "pasuri"}}</tool_call>',
        BOUND)
    assert calls == [{"id": "", "name": "media_play", "args": {"query": "pasuri"}}]
    assert text == "Sure."


def test_fenced_json_call_is_recovered():
    calls, _ = extract_text_tool_calls(
        '```json\n{"name": "calculate", "arguments": {"expression": "2+2"}}\n```', BOUND)
    assert calls[0]["name"] == "calculate" and calls[0]["args"] == {"expression": "2+2"}


def test_stringified_arguments_are_decoded():
    calls, _ = extract_text_tool_calls(
        '{"name": "remember", "arguments": "{\\"content\\": \\"x\\"}"}', BOUND)
    assert calls[0]["args"] == {"content": "x"}


def test_unbound_tool_name_never_runs():
    text = '{"name": "delete_everything", "arguments": {}}'
    calls, remaining = extract_text_tool_calls(text, BOUND)
    assert calls == [] and remaining == text


def test_plain_prose_yields_no_calls():
    calls, text = extract_text_tool_calls("Hello, how can I help?", BOUND)
    assert calls == [] and text == "Hello, how can I help?"


def test_schema_dicts_are_accepted_as_bound_tools():
    schema = [{"type": "function", "function": {"name": "media_play", "parameters": {}}}]
    calls, _ = extract_text_tool_calls('{"name": "media_play", "arguments": {}}', schema)
    assert calls[0]["name"] == "media_play"


# ── orchestrator behaviour ────────────────────────────────────────────────
class ScriptedBrain:
    """Replays scripted LLM responses; records how many times it was asked."""

    def __init__(self, brain, script):
        self.real = brain
        self.script = list(script)
        self.asked = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    async def athink(self, messages, tools=None, **_):
        from jarvis.core.providers import LLMResponse

        self.asked += 1
        step = self.script.pop(0) if self.script else {"content": "(nothing)"}
        return LLMResponse(content=step.get("content", ""),
                           tool_calls=step.get("tool_calls", []),
                           provider="fake", model="fake")


def _orchestrator(config, script, tool_result):
    from jarvis.core.orchestrator import Orchestrator
    from jarvis.tools.base import ToolResult
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    orchestrator = Orchestrator(config, registry=registry)
    orchestrator.initialize(ingest_knowledge=False, resume_session=False)
    orchestrator.brain = ScriptedBrain(orchestrator.brain, script)
    calls_run = []

    async def fake_execute_many(calls, **_):
        calls_run.extend(calls)
        return [ToolResult(ok=tool_result[0], content=tool_result[1],
                           error="" if tool_result[0] else tool_result[1], tool=c["name"])
                for c in calls]

    orchestrator.registry.execute_many = fake_execute_many
    return orchestrator, calls_run


def _run(orchestrator, text):
    async def go():
        return [event async for event in orchestrator.process(text, stream=False)]

    return asyncio.run(go())


def test_action_request_is_nudged_and_then_runs_the_tool(config):
    # "delete" is an action but is not routed directly, so the model is asked first
    orchestrator, calls_run = _orchestrator(config, [
        {"content": "Done, I deleted the old backup."},                  # claims, no tool
        {"tool_calls": [{"id": "1", "name": "delete_path",
                         "args": {"path": "old-backup"}}]},               # now acts
        {"content": "Deleted the old backup."},
    ], (True, "deleted old-backup"))
    orchestrator.harness.approver = lambda request: True  # operator says yes

    events = _run(orchestrator, "delete the old backup folder")
    assert any(e.type == "notice" and "asking the model to act" in e.text for e in events)
    assert [c["name"] for c in calls_run] == ["delete_path"]
    assert orchestrator.last_result.answer == "Deleted the old backup."
    orchestrator.shutdown()


def test_claimed_action_without_any_tool_is_flagged_not_believed(config):
    orchestrator, calls_run = _orchestrator(config, [
        {"content": "I played the song and it is playing."},
        {"content": "I played the song and it is playing."},              # still no tool
    ], (True, ""))

    # not a play request (those fall back to play_song, tested below), so no forced call
    events = _run(orchestrator, "send the report to bob")
    assert calls_run == []
    assert orchestrator.last_result.answer.startswith("I did not run a tool")
    assert any(e.type == "error" and "nothing was done" in e.text for e in events)
    orchestrator.shutdown()


def test_failed_tool_is_reported_as_failed(config):
    orchestrator, _ = _orchestrator(config, [
        {"tool_calls": [{"id": "1", "name": "media_play", "args": {"query": "x"}}]},
        {"content": "I played it."},
    ], (False, "no display"))

    _run(orchestrator, "play x")
    assert "did not succeed" in orchestrator.last_result.answer
    orchestrator.shutdown()


def test_small_talk_is_answered_without_tools(config):
    orchestrator, _ = _orchestrator(config, [{"content": "Hello, Harsh."}], (True, ""))
    events = _run(orchestrator, "hii")
    assert orchestrator.last_result.answer == "Hello, Harsh."
    tool_notice = [e for e in events if e.type == "notice" and "tools bound" in e.text]
    assert tool_notice and tool_notice[0].text.startswith("🔧 tools bound: 0/")
    orchestrator.shutdown()


def test_question_without_action_is_not_flagged(config):
    orchestrator, _ = _orchestrator(config, [{"content": "Paris is the capital of France."}],
                                    (True, ""))
    _run(orchestrator, "what is the capital of France?")
    assert orchestrator.last_result.answer == "Paris is the capital of France."
    orchestrator.shutdown()


def test_playbook_heading_is_not_duplicated(config, orchestrator_prompt=None):
    from jarvis.core.system_prompt import build_universal_prompt

    text = build_universal_prompt(config=config, tools=[])
    assert text.count("## PLAYBOOKS") == 1


def test_loop_that_never_answers_says_what_happened(config):
    tool_step = {"tool_calls": [{"id": "1", "name": "calculate", "args": {"expression": "1+1"}}]}
    orchestrator, calls_run = _orchestrator(config, [tool_step] * 20, (True, "2"))
    _run(orchestrator, "keep checking the numbers")
    answer = orchestrator.last_result.answer
    assert answer.startswith("I could not finish this"), answer
    assert "calculate" in answer
    orchestrator.shutdown()


def test_play_request_without_a_tool_call_runs_play_song(config):
    orchestrator, calls_run = _orchestrator(config, [
        {"content": "Sure, I will play it now."},      # no tool
        {"content": "Playing pasuri for you."},         # still no tool, after the nudge
    ], (True, "opened"))

    events = _run(orchestrator, "play pasuri on youtube")
    assert [c["name"] for c in calls_run] == ["play_song"]
    assert calls_run[0]["args"] == {"query": "pasuri"}
    assert any(e.type == "notice" and "routed to play_song" in e.text for e in events)
    assert orchestrator.brain.asked == 0          # routed directly, no model step
    orchestrator.shutdown()
