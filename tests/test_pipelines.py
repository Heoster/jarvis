"""Pipelines: strategies, fall-through, placeholders, checks — and the search route.

Tools are fakes, so these run offline and never open a window or press a key.
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote_plus

import pytest

from jarvis.tools.base import ToolResult
from jarvis.tools.pipelines import Pipeline, PipelineRunner, Step, Strategy, load_pipelines


class FakeTool:
    def __init__(self, name, script):
        self.name = name
        self.script = list(script)   # list of (ok, content) returned per call
        self.calls = []

    async def ainvoke(self, args=None, **_):
        self.calls.append(dict(args or {}))
        ok, content = self.script.pop(0) if self.script else (True, "")
        return ToolResult(ok=ok, content=content if ok else "", error="" if ok else content,
                          tool=self.name)


def _runner(tools, strategies, query_inputs=("query",)):
    pipeline = Pipeline(name="t", description="", inputs=list(query_inputs),
                        strategies=strategies)
    return PipelineRunner({"t": pipeline}, lambda name: tools.get(name))


def run(runner, inputs):
    return asyncio.run(runner.run("t", inputs))


def test_first_strategy_success_stops_the_pipeline():
    keys = FakeTool("keyboard_type", [(True, "typed")])
    other = FakeTool("page_type", [(True, "typed")])
    runner = _runner({"keyboard_type": keys, "page_type": other}, [
        Strategy("keys", [Step("keyboard_type", {"text": "{query}"})]),
        Strategy("page", [Step("page_type", {"text": "{query}"})]),
    ])
    result = run(runner, {"query": "heoster"})
    assert result.ok and result.strategy == "keys"
    assert other.calls == []


def test_failed_strategy_falls_through_and_is_recorded():
    keys = FakeTool("keyboard_type", [(False, "no window has focus")])
    page = FakeTool("page_type", [(True, "typed")])
    runner = _runner({"keyboard_type": keys, "page_type": page}, [
        Strategy("keys", [Step("keyboard_type", {"text": "{query}"})]),
        Strategy("page", [Step("page_type", {"text": "{query}"})]),
    ])
    result = run(runner, {"query": "heoster"})
    assert result.ok and result.strategy == "page"
    assert result.attempts[0] == {"strategy": "keys", "ok": False, "failed_step": 1,
                                  "reason": "keyboard_type: no window has focus"}


def test_placeholders_are_filled_including_url_encoding():
    typed = FakeTool("keyboard_type", [(True, "ok")])
    runner = _runner({"keyboard_type": typed}, [
        Strategy("s", [Step("keyboard_type", {"text": "https://x.test/?q={query_url}"})])])
    run(runner, {"query": "best laptops, 2026"})
    assert typed.calls[0]["text"] == "https://x.test/?q=" + quote_plus("best laptops, 2026")


def test_expect_query_fails_when_the_query_is_not_on_screen():
    screen = FakeTool("read_screen_text", [(True, "Desktop  Recycle Bin  Start")])
    runner = _runner({"read_screen_text": screen}, [
        Strategy("s", [Step("read_screen_text", {}, expect="query")])])
    result = run(runner, {"query": "heoster"})
    assert not result.ok
    assert "did not show the expected text" in result.attempts[0]["reason"]


def test_expect_query_passes_when_a_query_word_is_visible():
    screen = FakeTool("read_screen_text", [(True, "Results for HEOSTER on the web")])
    runner = _runner({"read_screen_text": screen}, [
        Strategy("s", [Step("read_screen_text", {}, expect="query")])])
    assert run(runner, {"query": "heoster"}).ok


def test_missing_tool_is_a_failed_step_not_a_crash():
    runner = _runner({}, [Strategy("s", [Step("keyboard_type", {"text": "x"})])])
    result = run(runner, {"query": "x"})
    assert not result.ok and "not available" in result.attempts[0]["reason"]


def test_all_strategies_failing_says_so():
    runner = _runner({"a": FakeTool("a", [(False, "boom"), (False, "boom again")])}, [
        Strategy("one", [Step("a", {})]), Strategy("two", [Step("a", {})])])
    result = run(runner, {"query": "x"})
    assert not result.ok and "all 2 strategies" in result.output
    assert len(result.attempts) == 2


def test_unknown_pipeline_is_reported():
    result = asyncio.run(PipelineRunner({}, lambda n: None).run("nope", {}))
    assert not result.ok and "no pipeline named" in result.output


def test_shipped_web_search_pipeline_has_three_strategies_in_order(config):
    pipelines = load_pipelines()
    names = [s.name for s in pipelines["web_search"].strategies]
    assert names == ["desktop_keyboard", "browser_typing", "search_api"]


def test_every_pipeline_step_uses_a_real_tool_with_real_arguments(config):
    from jarvis.config import load_config
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(load_config())
    problems = []
    for pipeline in load_pipelines().values():
        for strategy in pipeline.strategies:
            for step in strategy.steps:
                tool = registry.get(step.tool)
                if tool is None:
                    problems.append(f"{pipeline.name}/{strategy.name}: no tool {step.tool}")
                    continue
                schema = tool.schema() if callable(getattr(tool, "schema", None)) else tool.schema
                function = schema.get("function", schema)
                accepted = set((function.get("parameters") or {}).get("properties", {}))
                unknown = set(step.args) - accepted
                if unknown:
                    problems.append(f"{step.tool} has no argument {sorted(unknown)}")
    assert not problems, "\n".join(problems)


def test_run_pipeline_tool_is_registered(config):
    from jarvis.config import load_config
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(load_config())
    assert "run_pipeline" in registry.names()


# ── the search route in the orchestrator ──────────────────────────────────
def test_search_request_is_routed_to_the_web_search_pipeline(config):
    from test_tool_calling import _orchestrator, _run

    orchestrator, calls_run = _orchestrator(config, [{"content": "should not be used"}],
                                            (True, "**Search results for heoster** 1. ..."))
    events = _run(orchestrator, "search heoster")
    assert [c["name"] for c in calls_run] == ["run_pipeline"]
    assert calls_run[0]["args"] == {"name": "web_search", "query": "heoster"}
    assert orchestrator.brain.asked == 0          # no model step: the result is the answer
    assert orchestrator.last_result.answer.startswith("**Search results for heoster**")
    assert any(e.type == "notice" and "routed to run_pipeline" in e.text for e in events)
    orchestrator.shutdown()


def test_routed_failure_is_reported_as_an_error_not_a_guess(config):
    from test_tool_calling import _orchestrator, _run

    orchestrator, _ = _orchestrator(config, [{"content": "I searched it."}],
                                    (False, "pipeline web_search failed."))
    _run(orchestrator, "google zz quux")
    assert orchestrator.last_result.answer.startswith("Error:")
    assert "I searched" not in orchestrator.last_result.answer
    orchestrator.shutdown()


@pytest.mark.parametrize("text", ["search my notes for exams", "what is the capital of France"])
def test_non_web_requests_are_not_routed_to_search(text):
    from jarvis.core.orchestrator import direct_route

    assert direct_route(text) is None
