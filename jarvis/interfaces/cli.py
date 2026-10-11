"""JARVIS command line interface.

    python -m jarvis                     interactive chat
    python -m jarvis ask "..."            one-shot question
    python -m jarvis doctor               environment + capability report
    python -m jarvis tools [category]     list tools
    python -m jarvis api --verify         provider health / fallback check
    python -m jarvis browser --verify     Chrome harness + engine fallback test
    python -m jarvis screen               desktop capability report
    python -m jarvis memory               smart memory report
    python -m jarvis quota                free-tier usage
    python -m jarvis serve                local OpenAI-compatible API
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

import click

from ..config import config_source, load_config
from ..utils.logger import get_logger, setup_logging
from ..utils.sanitizer import truncate
from ..version import APP_NAME, __version__, banner

log = get_logger("cli")

try:  # pragma: no cover - presentation only
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    HAS_RICH = True
except Exception:  # pragma: no cover
    console = None
    HAS_RICH = False


def say(text: str = "", style: str = "") -> None:
    if HAS_RICH and style:
        console.print(str(text), style=style)
    else:
        print(text)


def md(text: str) -> None:
    if HAS_RICH:
        console.print(Markdown(text or ""))
    else:
        print(text)


def panel(title: str, body: str) -> None:
    if HAS_RICH:
        console.print(Panel(str(body), title=title, border_style="cyan"))
    else:
        print(f"\n=== {title} ===\n{body}\n")


def _tool_text(name: str, orchestrator) -> str:
    """Run a tool through the registry and return its text (CLI helpers)."""
    result = orchestrator.registry.execute_sync(name)
    return result.content if result.ok else f"❌ {result.error}"


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


# ── root group ────────────────────────────────────────────────────────────
@click.group(invoke_without_command=True, context_settings=CONTEXT_SETTINGS)
@click.option("--version", "show_version", is_flag=True, help="Show version and exit.")
@click.pass_context
def main(ctx: click.Context, show_version: bool) -> None:
    """JARVIS — the personal AI operator built for Heoster."""
    ctx.ensure_object(dict)
    if show_version:
        click.echo(f"{APP_NAME} {__version__}")
        return
    if ctx.invoked_subcommand is None:
        say(banner(), "bold cyan")
        ctx.invoke(chat)


def _build_orchestrator(cfg, *, verbose: bool = False):
    from ..core.orchestrator import Orchestrator

    orchestrator = Orchestrator(cfg)
    orchestrator.initialize(verbose=verbose)
    return orchestrator


def _stream_turn(orchestrator, text: str) -> str:
    """Run one turn, printing events live.  Returns the final answer."""

    async def runner() -> str:
        answer = ""
        async for event in orchestrator.process(text):
            if event.type == "thinking":
                if os.environ.get("JARVIS_VERBOSE"):
                    say(event.text, "dim")
            elif event.type == "text":
                answer = event.text
            elif event.type == "notice":
                say(event.text, "dim cyan")
            elif event.type == "tool_call":
                say("  " + event.text, "yellow")
            elif event.type == "tool_result":
                say("  " + truncate(event.text.replace("\n", " "), 300),
                    "green" if event.data.get("ok") else "red")
            elif event.type == "error":
                say("  " + event.text, "bold red")
        return answer

    print()
    answer = asyncio.run(runner())
    print()
    md(answer or "(no answer)")
    print()
    return answer


@main.command()
@click.option("--context", "context_file", default="", help="Extra context file.")
@click.option("--verbose", is_flag=True, help="Verbose tool logging.")
def chat(context_file: str, verbose: bool) -> None:
    """Interactive conversation with JARVIS (default command)."""
    cfg = load_config()
    setup_logging(cfg)
    say(f"{APP_NAME} {__version__} · config: {config_source()} · /help for commands", "dim")
    try:
        orchestrator = _build_orchestrator(cfg, verbose=verbose)
    except Exception as exc:
        say(f"❌ startup failed: {exc}", "bold red")
        raise SystemExit(1)

    extra_context = ""
    if context_file:
        try:
            with open(context_file, encoding="utf-8", errors="replace") as handle:
                extra_context = handle.read()[:6000]
        except Exception as exc:
            say(f"could not read {context_file}: {exc}", "yellow")

    status = orchestrator.status()
    say(f"tools {status['tools']['total']} · providers "
        f"{' → '.join(status['brain']['provider_chain'])} · memory "
        f"{status['memory'].get('long_term', 0)} records", "dim")
    say('Try: "open youtube and search for lofi beats" or /help.', "dim")

    while True:
        try:
            text = (console.input("\n[bold cyan]Heoster ›[/] ") if HAS_RICH
                    else input("\nHeoster › "))
        except (EOFError, KeyboardInterrupt):
            say("\nGoodbye, Heoster.", "cyan")
            break
        text = text.strip()
        if not text:
            continue
        if text.startswith("/"):
            if _slash_command(text, orchestrator) == "exit":
                break
            continue
        if extra_context:
            text = f"{text}\n\n[attached context: {context_file}]\n{extra_context}"
        try:
            _stream_turn(orchestrator, text)
        except KeyboardInterrupt:
            say("\n(interrupted)", "yellow")
        except Exception as exc:
            say(f"❌ {exc}", "bold red")

    try:
        orchestrator.shutdown()
    except Exception:
        pass


@main.command()
@click.argument("question", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True, help="Print a JSON result.")
def ask(question: tuple, as_json: bool) -> None:
    """Ask one question and exit."""
    cfg = load_config()
    setup_logging(cfg)
    orchestrator = _build_orchestrator(cfg)
    answer = _stream_turn(orchestrator, " ".join(question))
    if as_json:
        import json

        result = orchestrator.last_result
        click.echo(json.dumps({
            "answer": answer,
            "provider": getattr(result, "provider", ""),
            "model": getattr(result, "model", ""),
            "iterations": getattr(result, "iterations", 0),
            "tool_calls": getattr(result, "tool_calls", []),
            "latency_ms": getattr(result, "latency_ms", 0),
        }, indent=2, ensure_ascii=False))
    orchestrator.shutdown()


@main.command()
def doctor() -> None:
    """Check this machine and report exactly what works (no model needed)."""
    from ..utils.platform_probe import optional_deps_report, probe_platform

    cfg = load_config()
    setup_logging(cfg)
    sections: List[tuple] = []
    sections.append(("HOST", probe_platform().summary()))

    try:
        from ..tools import build_registry

        registry = build_registry(cfg)
        sections.append(("TOOLS", registry.summary()))
    except Exception as exc:
        sections.append(("TOOLS", f"❌ registry failed: {exc}"))

    try:
        from ..tools.screen.actions import desktop_report

        sections.append(("SCREEN (focus area)", desktop_report()))
    except Exception as exc:
        sections.append(("SCREEN (focus area)", f"unavailable: {exc}"))

    try:
        from ..tools.browser.harness import get_harness

        sections.append(("BROWSER (focus area)", get_harness(cfg).verify()))
    except Exception as exc:
        sections.append(("BROWSER (focus area)", f"unavailable: {exc}"))

    try:
        from ..memory.memory_manager import get_memory_manager

        sections.append(("MEMORY (focus area)", get_memory_manager(cfg).report()))
    except Exception as exc:
        sections.append(("MEMORY (focus area)", f"unavailable: {exc}"))

    try:
        from ..core.api_manager import get_api_manager

        sections.append(("API MANAGER", get_api_manager(cfg).report()))
    except Exception as exc:
        sections.append(("API MANAGER", f"unavailable: {exc}"))

    try:
        from ..tools import build_registry

        result = build_registry(cfg, verbose=False).execute_sync("crawl_tools_report")
        sections.append(("CRAWL", result.content if result.ok else str(result.error)))
    except Exception as exc:
        sections.append(("CRAWL", f"unavailable: {exc}"))

    sections.append(("OPTIONAL PACKAGES", optional_deps_report()))
    body = "\n\n".join(f"── {title} " + "─" * max(4, 44 - len(title)) + f"\n{text}"
                       for title, text in sections)
    panel("JARVIS doctor — capability report", body)


@main.command()
@click.argument("category", required=False)
def tools(category: Optional[str]) -> None:
    """List tools, optionally filtered by category."""
    cfg = load_config()
    from ..tools import build_registry

    registry = build_registry(cfg)
    entries = registry.get_by_category(category) if category else registry.get_all_tools()
    if not entries:
        say(f"No tools in '{category}'. Categories: {', '.join(registry.categories())}",
            "yellow")
        return
    say(f"\n{len(entries)} tool(s):\n", "bold cyan")
    for tool in entries:
        first_line = (tool.description or "").split("\n")[0]
        say(f"  {tool.name:<26} [{getattr(tool, 'category', '?') :<13}] "
            f"{truncate(first_line, 70)}")


@main.command()
@click.option("--verify", is_flag=True, help="Live-probe every provider (verifies fallback).")
def api(verify: bool) -> None:
    """Provider chain, health, latency and free-tier quota."""
    cfg = load_config()
    from ..core.api_manager import get_api_manager

    say(get_api_manager(cfg).report(verify=verify))


@main.command()
@click.option("--launch", is_flag=True, help="Launch Chrome from the terminal now.")
@click.option("--url", default="", help="URL to open on launch.")
@click.option("--headless", is_flag=True, help="Headless Chrome (default: visible).")
@click.option("--verify", is_flag=True, help="Verify the engine fallback chain.")
@click.option("--deep", is_flag=True, help="With --verify: actually start each engine.")
def browser(launch: bool, url: str, headless: bool, verify: bool, deep: bool) -> None:
    """Chrome harness: engines, terminal launch, fallback verification."""
    cfg = load_config()
    if headless:
        cfg.setdefault("browser", {})["headless"] = True
    from ..tools.browser.harness import get_harness

    harness = get_harness(cfg)
    if launch:
        outcome = harness.launch(url=url or None)
        say(("✅ " if outcome.get("ok") else "❌ ") + str(outcome.get("detail")))
    if verify:
        say(harness.verify(deep=deep))
        return
    say(harness.report())


@main.command()
@click.option("--screenshot", is_flag=True, help="Capture the desktop now.")
@click.option("--read", "read_text", is_flag=True, help="OCR the current screen.")
@click.option("--click-text", default="", help="Find text on screen and click it.")
@click.option("--windows", is_flag=True, help="List open windows.")
def screen(screenshot: bool, read_text: bool, click_text: str, windows: bool) -> None:
    """Screen control: capabilities, screenshots, OCR, click-by-text."""
    from ..config import load_config as _load

    cfg = _load()
    from ..tools import build_registry

    registry = build_registry(cfg)

    def run(name: str, args: Optional[Dict[str, Any]] = None) -> None:
        result = registry.execute_sync(name, args or {})
        say(result.content if result.ok else f"❌ {result.error}")

    if click_text:
        run("screen_click_text", {"text": click_text})
        return
    if screenshot:
        run("take_desktop_screenshot")
    if read_text:
        run("read_screen_text")
    if windows:
        run("screen_list_windows")
    if not any((screenshot, read_text, windows)):
        run("desktop_report") if "desktop_report" in registry else None
        from ..tools.screen.actions import desktop_report

        say(desktop_report())


@main.command()
@click.option("--consolidate", is_flag=True, help="Run consolidation and pruning now.")
@click.option("--search", "query", default="", help="Search every memory tier.")
def memory(consolidate: bool, query: str) -> None:
    """Inspect the smart memory manager."""
    cfg = load_config()
    from ..memory.memory_manager import get_memory_manager

    manager = get_memory_manager(cfg)
    if consolidate:
        say(str(manager.consolidate(force=True)))
        return
    if query:
        hits = manager.search(query, limit=10)
        if not hits:
            say(f"Nothing found for '{query}'.", "yellow")
        for hit in hits:
            score = hit.get("score")
            score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "  —  "
            say(f"  {hit['where']:<16} {score_text}  {truncate(hit['content'], 110)}")
        return
    say(manager.report())


@main.command()
@click.option("--reset", "reset_provider", default="", help="Reset one provider's window.")
def quota(reset_provider: str) -> None:
    """Free-tier quota usage per provider."""
    cfg = load_config()
    from ..utils.quota import get_quota_manager

    manager = get_quota_manager(cfg)
    if reset_provider:
        manager.reset(reset_provider)
        say(f"♻ reset quota window for {reset_provider}")
    say(manager.summary())


@main.command()
@click.option("--host", default="", help="Bind address (default from config).")
@click.option("--port", type=int, default=0, help="Port (default from config).")
def serve(host: str, port: int) -> None:
    """Run the local API server (OpenAI-compatible)."""
    cfg = load_config()
    setup_logging(cfg)
    from .api_server import run_server

    run_server(cfg, host=host or None, port=port or None)


@main.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--max", "max_results", type=int, default=5)
def search(query: tuple, max_results: int) -> None:
    """Search the web through the provider fallback chain."""
    cfg = load_config()
    from ..tools import build_registry

    registry = build_registry(cfg, verbose=False)
    result = registry.execute_sync("search_web",
                                   {"query": " ".join(query),
                                    "max_results": max_results})
    say(result.content if result.ok else f"❌ {result.error}")


@main.command()
@click.argument("url")
@click.option("--max-chars", type=int, default=6000)
def read(url: str, max_chars: int) -> None:
    """Read a URL through the crawl fallback chain."""
    cfg = load_config()
    from ..tools import build_registry

    registry = build_registry(cfg, verbose=False)
    result = registry.execute_sync("read_url", {"url": url, "max_chars": max_chars})
    say(result.content if result.ok else f"❌ {result.error}")


@main.command()
def agents() -> None:
    """List the specialist sub-agents."""
    cfg = load_config()
    from ..agents.agent_factory import AgentFactory

    say(AgentFactory(cfg).report())


@main.command()
@click.argument("agent")
@click.argument("task", nargs=-1, required=True)
def delegate(agent: str, task: tuple) -> None:
    """Hand a task to one specialist: `jarvis delegate browser "open youtube"`."""
    cfg = load_config()
    setup_logging(cfg)
    from ..agents.agent_factory import AgentFactory

    factory = AgentFactory(cfg)
    result = factory.dispatch_sync(" ".join(task), agent=agent)
    if result is None:
        say("no such agent", "red")
        raise SystemExit(1)
    say(result.summary(), "dim")
    md(result.answer)


@main.command()
def profile() -> None:
    """Show what JARVIS knows about Heoster."""
    from ..persona import OperatorProfile

    say(OperatorProfile().report())


# ── slash commands inside chat ────────────────────────────────────────────
def _slash_command(text: str, orchestrator) -> str:
    """Handle a /command.  Returns "exit" to leave the chat."""
    command, _, argument = text[1:].partition(" ")
    command = command.lower().strip()
    argument = argument.strip()
    registry = orchestrator.registry

    if command in ("exit", "quit", "q"):
        return "exit"
    if command in ("help", "h", "?"):
        say(HELP_TEXT)
    elif command == "tools":
        entries = registry.get_by_category(argument) if argument else registry.get_all_tools()
        for tool in entries:
            say(f"  {tool.name:<26} [{getattr(tool, 'category', '?')}]", "cyan")
        say(f"{len(entries)} tool(s)", "dim")
    elif command == "status":
        say(orchestrator.report())
    elif command == "memory":
        if argument == "consolidate":
            say(str(orchestrator.memory.consolidate(force=True)))
        elif argument:
            for hit in orchestrator.memory.search(argument, limit=8):
                say(f"  {hit['where']:<16} {truncate(hit['content'], 100)}")
        else:
            say(orchestrator.memory.report())
    elif command == "quota":
        from ..utils.quota import get_quota_manager

        say(get_quota_manager(orchestrator.config).summary())
    elif command == "api":
        say(orchestrator.brain.api.report(verify=argument == "verify"))
    elif command == "browser":
        from ..tools.browser.harness import get_harness

        say(get_harness(orchestrator.config).report())
    elif command == "screen":
        from ..tools.screen.actions import desktop_report

        say(desktop_report())
    elif command == "agents":
        from ..agents.agent_factory import AgentFactory

        say(AgentFactory(orchestrator.config).report())
    elif command == "agent":
        from ..agents.agent_factory import AgentFactory

        if not argument:
            say("usage: /agent <name> <task>", "yellow")
        else:
            name, _, task = argument.partition(" ")
            result = AgentFactory(orchestrator.config).dispatch_sync(task, agent=name)
            say(result.answer if result else "unknown agent")
    elif command == "profile":
        from ..persona import OperatorProfile

        say(OperatorProfile().report())
    elif command == "skills":
        say(str(orchestrator.memory.skills.stats()))
    elif command == "model":
        if argument:
            orchestrator.brain.api.llm_cfg["provider"] = argument  # type: ignore[index]
            say(f"provider pinned to {argument}")
        else:
            say(f"active provider: {orchestrator.brain.api.active_provider()}")
    elif command == "clear":
        orchestrator.memory.short.clear()
        say("🧹 conversation cleared (long-term memory kept)")
    elif command == "compress":
        say(str(orchestrator.memory.consolidate(force=True)))
    elif command in ("report", "debug"):
        say(orchestrator.report())
    elif command == "time":
        result = registry.execute_sync("get_current_datetime", {})
        say(result.content if result.ok else result.error)
    else:
        say(f"unknown command '/{command}' — /help for the list", "yellow")
    return ""


HELP_TEXT = """\
[bold cyan]JARVIS commands[/]

  /help                this list                     /exit    quit
  /status              brain + memory + tool status  /report  full debug report
  /tools [category]    list tools                    /agents  specialist sub-agents
  /agent <name> <task> run one specialist            /model [provider]  show/pin
  /memory [query]      inspect/search memory         /memory consolidate  tidy now
  /profile             what JARVIS knows about Heoster
  /skills              learned procedures             /compress  force consolidation
  /api [verify]        provider chain + health       /quota   free-tier usage
  /browser             browser harness status        /screen  screen capabilities
  /clear               clear the thread              /time    current date/time (IST)

Talk normally to use JARVIS. It remembers what matters between sessions.
"""


if __name__ == "__main__":  # pragma: no cover
    main()
