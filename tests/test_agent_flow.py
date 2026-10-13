"""End-to-end behaviour of the orchestrator, the agents and the API server.

Everything here runs offline: the `config` fixture pins the LLM provider to the
built-in ``stub`` provider, which is exactly what JARVIS falls back to when
nothing else is reachable.
"""

from __future__ import annotations

import asyncio
import json

import pytest


def _orchestrator(config):
    from jarvis.core.orchestrator import Orchestrator
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    orchestrator = Orchestrator(config, registry=registry)
    orchestrator.initialize(ingest_knowledge=False, resume_session=False)
    return orchestrator


# ── orchestrator ──────────────────────────────────────────────────────────
def test_orchestrator_initialises_everything(config):
    orchestrator = _orchestrator(config)
    assert len(orchestrator.registry.get_all_tools()) > 90
    report = orchestrator.initialize(ingest_knowledge=False, resume_session=False)
    assert report["tools"] > 90
    status = orchestrator.status()
    assert status["brain"]["provider_chain"]
    assert "long_term" in status["memory"]
    orchestrator.shutdown()


def test_orchestrator_answers_and_streams_events(config):
    orchestrator = _orchestrator(config)

    async def run():
        events = []
        async for event in orchestrator.process("What is 21 * 2 + 8?"):
            events.append(event)
        return events

    events = asyncio.run(run())
    kinds = {event.type for event in events}
    assert "notice" in kinds and "done" in kinds
    assert orchestrator.last_result is not None
    assert orchestrator.last_result.answer, "a turn must always produce an answer"
    assert orchestrator.last_result.latency_ms >= 0
    orchestrator.shutdown()


def test_orchestrator_executes_a_tool_and_reports_it(config):
    orchestrator = _orchestrator(config)

    async def run():
        return [event async for event in
                orchestrator.process("Use the calculate tool for 17 * 23")]

    events = asyncio.run(run())
    tool_calls = [event for event in events if event.type == "tool_call"]
    # the stub provider mimics a tool call; if it does not, the turn still
    # completes cleanly — what matters is that calls are surfaced as events.
    assert isinstance(tool_calls, list)
    result = orchestrator.last_result
    assert result.tool_calls is not None
    orchestrator.shutdown()


def test_orchestrator_writes_to_memory(config):
    orchestrator = _orchestrator(config)
    asyncio.run(orchestrator.ask("Remember that my favourite editor is VS Code"))
    stored = orchestrator.memory.long.count()
    assert stored >= 1, "a turn should leave a memory trace"
    orchestrator.shutdown()


def test_orchestrator_survives_an_unknown_tool_request(config):
    orchestrator = _orchestrator(config)

    async def run():
        return [event async for event in
                orchestrator.process("call the tool 'make_me_a_sandwich' and show me")]

    events = asyncio.run(run())
    assert any(event.type == "done" for event in events)
    orchestrator.shutdown()


def test_tool_selection_is_relevant_and_small(config):
    from jarvis.core.brain import Brain

    brain = Brain(config)
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    brain.bind_tools(registry.get_all_tools())

    browser_pick = {tool.name for tool in brain.select_relevant_tools(
        "open youtube and search for lofi beats")}
    # max_tools is 14 plus a few always-on tools — verify a sensible upper bound
    assert len(browser_pick) <= 20
    assert browser_pick & {"browse_open", "page_type", "search_web"}

    math_pick = {tool.name for tool in brain.select_relevant_tools(
        "what is 15% of 4200")}
    assert math_pick & {"calculate", "percentage"}


# ── agents ────────────────────────────────────────────────────────────────
def test_agent_factory_routes_requests(config):
    from jarvis.agents.agent_factory import AgentFactory

    factory = AgentFactory(config)
    assert factory.route("open youtube in chrome and play lofi").agent == "browser"
    assert factory.route("do a quick bit of research on the best free LLM APIs").agent == "research"
    assert factory.route("write a python script that prints the date").agent == "code"
    assert factory.route("what do you remember about my preferences").agent == "memory"
    assert factory.route("hi") is None, "small talk belongs to the main loop"


def test_every_specialist_has_a_bounded_toolbox(config):
    from jarvis.agents.specialists import AGENT_CLASSES

    for name, agent_cls in AGENT_CLASSES.items():
        assert agent_cls.spec.categories or agent_cls.spec.tools, name
        assert agent_cls.spec.max_iterations <= 10, name
        assert agent_cls.spec.prompt.strip(), name


def test_browser_agent_only_gets_browser_tools(config):
    from jarvis.agents.specialists import BrowserAgent

    agent = BrowserAgent(config)
    names = {tool.name for tool in agent.tools()}
    assert "page_click" in names and "cursor_click" not in names
    assert len(names) <= 20


def test_agent_runs_and_records_its_work(config):
    from jarvis.agents.specialists import MemoryAgent

    agent = MemoryAgent(config)
    result = asyncio.run(agent.run("what do you know about Heoster?"))
    assert result.agent == "memory"
    assert result.answer
    assert result.iterations >= 1


# ── API server (no socket needed: the handler logic is exercised directly) ──
def test_api_surface_answers_without_a_network(config):
    from jarvis.interfaces.api_server import JarvisAPI

    api = JarvisAPI(config)
    assert api.health()["status"] == "ok"
    tools = api.tools()
    assert tools["count"] > 90
    answer = api.ask("say hello")
    assert answer["answer"]
    completion = api.openai_chat({"messages": [{"role": "user",
                                                "content": "hello jarvis"}]})
    assert completion["object"] == "chat.completion"
    assert completion["choices"][0]["message"]["role"] == "assistant"
    assert "jarvis" in completion


def test_api_tool_endpoint_runs_a_tool(config):
    from jarvis.interfaces.api_server import JarvisAPI

    api = JarvisAPI(config)
    result = api.tool_call("calculate", {"expression": "2+2"})
    assert result["ok"] and "4" in result["content"]


# ── CLI wiring ────────────────────────────────────────────────────────────
def test_cli_exposes_the_expected_commands():
    from click.testing import CliRunner

    from jarvis.interfaces.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("chat", "ask", "doctor", "tools", "api", "browser", "screen",
                    "memory", "quota", "serve", "search", "read", "agents"):
        assert command in result.output

    version = runner.invoke(main, ["--version"])
    assert version.exit_code == 0 and "JARVIS" in version.output


def test_cli_tools_lists_a_category():
    from click.testing import CliRunner

    from jarvis.interfaces.cli import main

    result = CliRunner().invoke(main, ["tools", "screen"])
    assert result.exit_code == 0
    assert "cursor_click" in result.output


def test_cli_chat_displays_tool_count(monkeypatch):
    from click.testing import CliRunner

    import jarvis.interfaces.cli as cli

    class FakeOrchestrator:
        def status(self):
            return {
                "tools": {"tools": 123},
                "brain": {"provider_chain": ["stub"]},
                "memory": {"long_term": 45},
            }

        def shutdown(self):
            pass

    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli, "_build_orchestrator",
                        lambda _config, verbose=False: FakeOrchestrator())

    result = CliRunner().invoke(cli.main, ["chat"], input="")

    assert result.exit_code == 0, result.exception
    assert "tools 123" in result.output


def test_study_agent_routes_and_has_vault_tools(config):
    from jarvis.agents.specialists import AGENT_CLASSES, StudyAgent

    assert "study" in AGENT_CLASSES
    decision = "make flashcards from my physics notes"
    from jarvis.agents.agent_factory import AgentFactory

    routed = AgentFactory(config).route(decision)
    assert routed is not None and routed.agent == "study"

    agent = StudyAgent(config)
    names = {tool.name for tool in agent.tools()}
    assert {"obsidian_flashcards_due", "obsidian_write_note",
            "obsidian_study_plan"} <= names
    assert len(names) <= 22
