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
import json
import os
import sys
from pathlib import Path
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
        raise SystemExit(1) from exc

    extra_context = ""
    if context_file:
        try:
            with open(context_file, encoding="utf-8", errors="replace") as handle:
                extra_context = handle.read()[:6000]
        except Exception as exc:
            say(f"could not read {context_file}: {exc}", "yellow")

    status = orchestrator.status()
    say(f"tools {status['tools']['tools']} · providers "
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



def _obsidian_errors(func):
    """Turn vault problems into a readable line instead of a traceback."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from ..tools.obsidian.vault import ObsidianError

        try:
            return func(*args, **kwargs)
        except (ObsidianError, ValueError) as exc:
            say(f"⚠ {exc}", "red")
            raise click.exceptions.Exit(1) from exc
    return wrapper

# ── obsidian (notes · flashcards · study planning) ──────────────────────────
@main.group(invoke_without_command=True)
@click.option("--vault", default="", help="Vault folder (overrides OBSIDIAN_VAULT).")
@click.pass_context
def obsidian(ctx: click.Context, vault: str) -> None:
    """Obsidian vault: notes, flashcards, revision queue, study plans."""
    ctx.ensure_object(dict)
    ctx.obj["vault"] = vault
    if ctx.invoked_subcommand is None:
        _obsidian_status(vault)


def _obsidian_vault(vault: str = ""):
    from ..tools.obsidian.study import study_report
    from ..tools.obsidian.vault import resolve_vault

    cfg = load_config()
    return cfg, resolve_vault(cfg, explicit=vault)


@_obsidian_errors
def _obsidian_status(vault: str = "") -> None:
    from ..tools.obsidian.study import study_report

    cfg, path = _obsidian_vault(vault)
    say(study_report(path, cfg))


@obsidian.command("status")
@click.pass_context
@_obsidian_errors
def obsidian_status(ctx: click.Context) -> None:
    """Vault, notes, due cards, exams."""
    _obsidian_status(ctx.obj.get("vault", "") if ctx.obj else "")


@obsidian.command("search")
@click.argument("query")
@click.option("--limit", type=int, default=20, help="Maximum matches.")
@click.option("--folder", default="", help="Restrict to a folder.")
@click.pass_context
@_obsidian_errors
def obsidian_search(ctx: click.Context, query: str, limit: int, folder: str) -> None:
    """Search every note in the vault."""
    from ..tools.obsidian.vault import search_vault

    _cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    hits = search_vault(path, query, limit=limit, folder=folder)
    if not hits:
        say(f"No notes matched '{query}'.", "yellow")
        return
    for hit in hits:
        say(f"  {hit.note}:{hit.line} — {truncate(' '.join(hit.text.split()), 120)}")


@obsidian.command("notes")
@click.option("--folder", default="", help="Restrict to a folder.")
@click.option("--tag", default="", help="Only notes with this tag.")
@click.option("--limit", type=int, default=30)
@click.pass_context
@_obsidian_errors
def obsidian_notes(ctx: click.Context, folder: str, tag: str, limit: int) -> None:
    """List notes (newest first)."""
    from ..tools.obsidian.vault import load_notes

    _cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    notes = load_notes(path, folder=folder)
    if tag:
        notes = [note for note in notes if tag.lstrip("#") in note.tags]
    notes.sort(key=lambda note: -note.mtime)
    for note in notes[:limit]:
        say(f"  {note.rel:<52} {note.metadata_line()}")


@obsidian.command("read")
@click.argument("note")
@click.option("--max-chars", type=int, default=4000)
@click.pass_context
@_obsidian_errors
def obsidian_read(ctx: click.Context, note: str, max_chars: int) -> None:
    """Print one note."""
    from ..tools.obsidian.vault import read_note

    _cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    loaded = read_note(path, note)
    say(f"{loaded.title} ({loaded.rel}) · {loaded.metadata_line()}\n")
    say(truncate(loaded.body, max_chars))


@obsidian.command("daily")
@click.option("--text", default="", help="Text to append to the daily note.")
@click.option("--day", default="", help="Date YYYY-MM-DD (default today).")
@click.option("--section", default="Study log", help="Heading to append under.")
@click.pass_context
@_obsidian_errors
def obsidian_daily(ctx: click.Context, text: str, day: str, section: str) -> None:
    """Create or open today's daily note."""
    from datetime import date

    from ..tools.obsidian.vault import append_under_heading, daily_note

    cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    when = date.fromisoformat(day) if day else date.today()
    note = daily_note(path, day=when, config=cfg)
    if text:
        note = append_under_heading(path, note.rel, section, text)
    say(f"✅ {note.rel} ({note.word_count} words)")


@obsidian.command("tasks")
@click.option("--all", "include_done", is_flag=True, help="Include finished tasks.")
@click.option("--due-before", default="", help="Only tasks due on/before this date.")
@click.option("--complete", default="", help="Mark done: \"note.md:line\".")
@click.pass_context
@_obsidian_errors
def obsidian_tasks(ctx: click.Context, include_done: bool, due_before: str,
                   complete: str) -> None:
    """List or complete checkbox tasks."""
    from ..tools.obsidian.vault import all_tasks, toggle_task

    _cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    if complete:
        reference, _, line = complete.rpartition(":")
        say(str(toggle_task(path, reference, int(line), done=True)))
        return
    tasks = all_tasks(path, include_done=include_done, due_before=due_before)
    if not tasks:
        say("No open tasks.")
        return
    for task in tasks:
        due = f" (due {task.due})" if task.due else ""
        say(f"  [ ] {truncate(task.text, 90)}{due}  {task.note}:{task.line}")


@obsidian.command("cards")
@click.argument("action", type=click.Choice(["due", "stats", "add", "from-note", "review",
                                             "forecast"]))
@click.argument("args", nargs=-1)
@click.option("--note", default="", help="Target note for add/review.")
@click.option("--grade", default="good", help="again|hard|good|easy")
@click.option("--limit", type=int, default=12)
@click.pass_context
@_obsidian_errors
def obsidian_cards(ctx: click.Context, action: str, args: tuple, note: str, grade: str,
                   limit: int) -> None:
    """Flashcards: due · stats · add "Q :: A" · from-note <note> · forecast · review."""
    from datetime import date

    from ..tools.obsidian.flashcards import (SrsStore, collect_cards, due_cards,
                                             generate_cards_from_note, review_forecast,
                                             schedule)
    from ..tools.obsidian.vault import append_under_heading, read_note, write_note

    cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    store = SrsStore()
    if action == "stats":
        stats = store.stats()
        say(f"tracked {stats['tracked']} · due {stats['due']} · mature {stats['mature']} · "
            f"learning {stats['learning']} · reviews {stats['reviews']} · streak {stats['streak']}d")
        say(f"schedule file: {stats['path']}")
        return
    cards = collect_cards(path, store=store)
    if action == "forecast":
        say(review_forecast(cards))
        return
    if action == "due":
        due = due_cards(cards, limit=limit)
        if not due:
            say("Nothing due — you are ahead. 🎉")
            return
        for card in due:
            say(f"  Q: {card.question}")
            say(f"  A: {card.answer}   [{card.note} · "
                f"{'new' if card.is_new else card.due}]", "green")
        return
    if action == "add":
        pairs = [value for value in args if "::" in value]
        if not pairs:
            say("Give cards as \"Question :: Answer\" arguments.", "red")
            return
        block = "\n".join(f"- {pair}" for pair in pairs)
        if note:
            append_under_heading(path, note, "Flashcards", block)
        else:
            write_note(path, f"Cards {date.today().isoformat()}.md",
                       f"## Flashcards\n\n{block}\n", folder="03 Flashcards",
                       frontmatter={"type": "flashcards", "tags": ["flashcard"]})
        say(f"✅ saved {len(pairs)} card(s)")
        return
    if action == "from-note":
        if not note:
            say("Use --note <note> to choose the source.", "red")
            return
        source = read_note(path, note)
        generated = generate_cards_from_note(source.body)
        if not generated:
            say("Nothing card-worthy found in that note.", "yellow")
            return
        block = "\n".join(f"- {card.question} :: {card.answer}" for card in generated)
        append_under_heading(path, source.rel, "Flashcards", block)
        say(f"✅ {len(generated)} card(s) added to {source.rel}")
        return
    if action == "review":
        question = " ".join(args).strip()
        if not note:
            say("Use --note <note> (and the question text) to review a card.", "red")
            return
        source = read_note(path, note)
        from ..tools.obsidian.flashcards import cards_from_text

        match = next((card for card in cards_from_text(source.rel, source.body)
                      if not question or question.lower() in card.question.lower()), None)
        if match is None:
            say("No matching card.", "red")
            return
        store.get(match)
        schedule(match, grade)
        store.update(match, grade)
        say(f"✅ [{grade}] next review {match.due} (interval {match.interval:.0f}d)")


@obsidian.command("plan")
@click.option("--exam", "exam_date", required=True, help="Exam date YYYY-MM-DD.")
@click.option("--subjects", default="", help="Comma separated subjects.")
@click.option("--topics-per-day", type=int, default=2)
@click.option("--session-minutes", type=int, default=45)
@click.option("--no-daily", is_flag=True, help="Write only the plan note.")
@click.pass_context
@_obsidian_errors
def obsidian_plan(ctx: click.Context, exam_date: str, subjects: str, topics_per_day: int,
                  session_minutes: int, no_daily: bool) -> None:
    """Build a day-by-day revision plan before an exam."""
    from ..tools.obsidian.study import build_study_plan

    cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    chosen = [s.strip() for s in subjects.split(",") if s.strip()]
    plan = build_study_plan(path, exam_date=exam_date, subjects=chosen,
                            topics_per_day=topics_per_day,
                            session_minutes=session_minutes, write_daily=not no_daily,
                            config=cfg)
    say(f"✅ plan for {plan['exam_date']}: {plan['days']} days, {plan['topics']} topics")
    say(f"   note: {plan['plan_note']}")
    for day in sorted(plan["schedule"])[:7]:
        topics = ", ".join(f"{s}: {t}" for s, t in plan["schedule"][day])
        say(f"   {day}: {truncate(topics, 100)}")


@obsidian.command("queue")
@click.option("--limit", type=int, default=12)
@click.pass_context
@_obsidian_errors
def obsidian_queue(ctx: click.Context, limit: int) -> None:
    """What to revise right now: cards + overdue tasks + #revision notes."""
    from ..tools.obsidian.study import revision_queue

    _cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    queue = revision_queue(path, limit=limit)
    say("**Cards**")
    for card in queue["due_cards"] or []:
        say(f"  Q: {card.question}\n  A: {card.answer}", "green")
    if not queue["due_cards"]:
        say("  none due")
    say("**Tasks**")
    for task in queue["tasks"] or []:
        say(f"  [ ] {truncate(task.text, 80)}  {task.note}:{task.line}")
    if not queue["tasks"]:
        say("  none")
    counts = queue["counts"]
    say(f"_streak {counts['streak']}d · {counts['reviews']} reviews · "
        f"{counts['tracked']} cards tracked_")


@obsidian.command("plugins")
@click.option("--install", is_flag=True, help="Install/enable the JARVIS plugin.")
@click.option("--install-prebuilt", "install_prebuilt_spec", default="",
              help='Install prebuilt community plugins: "all" or ids/names, comma separated.')
@click.option("--list-prebuilt", is_flag=True,
              help="Show the prebuilt catalogue with install status.")
@click.option("--update", is_flag=True,
              help="Reinstall the newest release of every prebuilt plugin already installed.")
@click.option("--offline", is_flag=True,
              help="Use only bundles vendored in obsidian-plugin/prebuilt/.")
@click.option("--tag", default="", help="Pin a release tag (default: latest release).")
@click.option("--checked", is_flag=True,
              help="Also ask GitHub for the newest release of each prebuilt plugin.")
@click.pass_context
@_obsidian_errors
def obsidian_plugins_cmd(ctx: click.Context, install: bool, install_prebuilt_spec: str,
                         list_prebuilt: bool, update: bool, offline: bool, tag: str,
                         checked: bool) -> None:
    """List vault plugins; install the JARVIS plugin or prebuilt community plugins."""
    from ..tools.obsidian.prebuilt import (describe_prebuilt, install_prebuilt,
                                           prebuilt_status, resolve_selection)
    from ..tools.obsidian.study import (install_obsidian_plugin, installed_plugins,
                                        jarvis_plugin_status)

    _cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    plugins = installed_plugins(path)
    say(f"**{len(plugins)} plugin(s) installed:**")
    for plugin in plugins:
        state = "enabled" if plugin.enabled else "disabled"
        say(f"  {plugin.name} ({plugin.id}) {plugin.version} — {state}")
    if not plugins:
        say("  none yet")

    if install:
        result = install_obsidian_plugin(path)
        say(f"✅ JARVIS plugin installed → {result['plugin_dir']}")
        say(f"   {result['restart_hint']}")
    status = jarvis_plugin_status(path)
    say(f"JARVIS plugin: {'installed' if status['installed'] else 'not installed'} "
        f"({'enabled' if status['enabled'] else 'disabled'})")

    if install_prebuilt_spec or update:
        selection, unknown = resolve_selection(install_prebuilt_spec or "all")
        if update and not install_prebuilt_spec:
            from ..tools.obsidian.prebuilt import enabled_prebuilt_ids
            have = set(enabled_prebuilt_ids(path))
            selection = [plugin for plugin in selection if plugin.id in have]
            if not selection:
                say("\nNothing to update — no prebuilt plugin is installed yet.")
        for name in unknown:
            say(f"⚠ unknown plugin: {name}")
        if selection:
            say(f"\nInstalling {len(selection)} prebuilt plugin(s)…")
            result = install_prebuilt(path, selection, enable=True, tag=tag, offline=offline)
            for item in result["installed"]:
                detail = f"{item['source']}" + (f" {item['tag']}" if item["tag"] else "")
                flag = "enabled" if item["enabled"] else "installed (enable in Obsidian)"
                say(f"  ✅ {item['name']} v{item['version']} — {flag} · {detail}")
                if item.get("warning"):
                    say(f"     ↳ {item['warning']}")
            for item in result["failed"]:
                say(f"  ⚠ {item['name']}: {item['error']}")
            if result["installed"]:
                say(f"↳ {result['restart_hint']}")
        return

    say("")
    say(describe_prebuilt(prebuilt_status(path, check_releases=checked)))
    say("\n_install: `python -m jarvis obsidian plugins --install-prebuilt all` "
        "(add --offline to use vendored bundles only)_")


@obsidian.command("kit")
@click.option("--subjects", default="", help="Comma separated subjects.")
@click.option("--force", is_flag=True, help="Overwrite existing templates.")
@click.pass_context
@_obsidian_errors
def obsidian_kit(ctx: click.Context, subjects: str, force: bool) -> None:
    """Create the student vault structure (folders, templates, dashboard)."""
    from ..tools.obsidian.study import DEFAULT_SUBJECTS, create_study_kit

    _cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    chosen = [s.strip() for s in subjects.split(",") if s.strip()] or DEFAULT_SUBJECTS
    result = create_study_kit(path, subjects=chosen, overwrite_templates=force)
    say(f"✅ study kit in {result['vault']} — {len(result['created'])} item(s) created")
    for item in result["created"][:20]:
        say(f"   {item}")


@obsidian.command("exams")
@click.option("--days", type=int, default=120, help="Look ahead N days.")
@click.pass_context
@_obsidian_errors
def obsidian_exams_cmd(ctx: click.Context, days: int) -> None:
    """Upcoming exams and days left."""
    from ..tools.obsidian.study import exam_countdown

    cfg, path = _obsidian_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    exams = exam_countdown(path, cfg, within_days=days)
    if not exams:
        say("No exam notes found (add exam_date: YYYY-MM-DD to a note in 04 Exams).",
            "yellow")
        return
    for exam in exams:
        say(f"  {exam['exam']} — {exam['days_left']} day(s) left ({exam['date']})")


# ── run one tool directly ──────────────────────────────────────────────────
@main.command(name="call")
@click.argument("tool_name")
@click.argument("args", default="{}", required=False)
@click.option("--timeout", default=0.0, type=float, help="Seconds before it is called off.")
def call(tool_name: str, args: str, timeout: float) -> None:
    """Run one tool directly — the fastest way to check a real click or tab.

    \b
    Examples:
      python -m jarvis call media_history '{"day":"yesterday"}'
      python -m jarvis call page_type '{"selector":"input#search","text":"hello"}'
      python -m jarvis call page_tab_switch '{"target":"results"}'
    """
    from ..tools.registry import ToolRegistry

    try:
        payload = json.loads(args or "{}")
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"arguments must be JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.BadParameter("arguments must be a JSON object")

    cfg = load_config()
    registry = ToolRegistry()
    registry.load_all_default_tools(cfg, verbose=False)
    result = asyncio.run(registry.execute(tool_name, payload,
                                          timeout=timeout or None))
    if result.ok:
        say(f"✅ {tool_name} · {result.elapsed_ms} ms")
        md(result.content or "(no output)")
        if result.artifacts:
            say("artifacts: " + ", ".join(str(a) for a in result.artifacts))
    else:
        say(f"❌ {tool_name} failed")
        md(result.error or "unknown error")
        raise SystemExit(1)



# ── the system prompt ───────────────────────────────────────────────────────
@main.command()
@click.option("--tools", default="", help="Comma separated tool names to advertise as bound.")
@click.option("--memory", default="", help='Recalled-memory block to include, e.g. "favourite song: …".')
@click.option("--knowledge", default="", help="Knowledge-base block to include.")
@click.option("--session", default="", help="Session context block to include.")
@click.option("--budget", default=0, type=int, help="Character ceiling (0 = config value).")
@click.option("--style", default="", help="universal | persona (default: config).")
@click.option("--out", default="", help="Write it to a file instead of printing.")
def prompt(tools: str, memory: str, knowledge: str, session: str, budget: int,
           style: str, out: str) -> None:
    """Show the exact system prompt the model receives (and why it matters)."""
    from ..core.brain import Brain
    from ..core.system_prompt import build_universal_prompt, collect_environment
    from ..tools.registry import ToolRegistry

    cfg = load_config()
    if style:
        cfg = dict(cfg)
        cfg["brain"] = dict(cfg.get("brain") or {})
        cfg["brain"]["system_prompt"] = style
    registry = ToolRegistry()
    registry.load_all_default_tools(cfg, verbose=False)
    brain = Brain(cfg)
    chosen = [name.strip() for name in tools.split(",") if name.strip()]
    bound = [registry.get(name) for name in chosen if registry.get(name)] or \
        registry.get_all_tools()[:9]
    brain.bind_tools(bound)
    brain.attach_registry(registry)
    brain.set_session_context(session)
    limit = budget or int((cfg.get("brain") or {}).get("system_prompt_budget", 12000))

    if brain.prompt_style() == "persona":
        text = brain.system_prompt(memory=memory, knowledge=knowledge)
    else:
        text = build_universal_prompt(
            environment=collect_environment(cfg, registry=registry),
            config=cfg, tools=bound, memories=memory, knowledge=knowledge,
            session=session, max_chars=limit)
    if out:
        Path(out).expanduser().write_text(text, encoding="utf-8")
        say(f"✅ system prompt ({len(text)} chars) → {out}")
        return
    say(f"# system prompt · style={brain.prompt_style()} · {len(text)} chars "
        f"(budget {limit}) · bound tools: "
        f"{', '.join(t.name for t in bound) if len(bound) <= 6 else f'{len(bound)}'}")
    md(text)


# ── study coach (retention analytics + scheduled brief) ─────────────────────
@main.group(invoke_without_command=True)
@click.option("--vault", default="", help="Vault folder (overrides OBSIDIAN_VAULT).")
@click.pass_context
def coach(ctx: click.Context, vault: str) -> None:
    """Study coach: daily brief, weak-topic analytics, weekly reviews."""
    ctx.ensure_object(dict)
    ctx.obj["vault"] = vault
    if ctx.invoked_subcommand is None:
        _coach_status(ctx.obj.get("vault", ""))


def _coach_vault(vault: str = ""):
    from ..tools.obsidian.vault import resolve_vault

    cfg = load_config()
    return cfg, resolve_vault(cfg, explicit=vault)


@_obsidian_errors
def _coach_status(vault: str = "") -> None:
    from ..tools.obsidian.coach import coach_status

    cfg, path = _coach_vault(vault)
    md(coach_status(path, cfg))


@coach.command("brief")
@click.option("--write", is_flag=True, help="Save the brief into today's daily note.")
@click.option("--day", default="", help="Target day (YYYY-MM-DD), default today.")
@click.option("--limit", default=0, type=int, help="Items per section.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
@_obsidian_errors
def coach_brief(ctx: click.Context, write: bool, day: str, limit: int, as_json: bool) -> None:
    """What to study right now: due cards, tasks, weak topics, exams."""
    import json as _json
    from datetime import date

    from ..tools.obsidian.coach import build_brief, render_brief, write_brief

    cfg, path = _coach_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    target = date.fromisoformat(day) if day else None
    brief = build_brief(path, cfg, day=target, limit=limit or None)
    if write:
        written = write_brief(path, cfg, brief=brief, day=target)
        say(f"✅ brief → {written['note']}")
    if as_json:
        say(_json.dumps(brief, indent=2, default=str))
    else:
        md(render_brief(brief))


@coach.command("weak")
@click.option("--limit", default=5, type=int, help="How many topics to list.")
@click.option("--days", default=30, type=int, help="Retention window in days.")
@click.pass_context
@_obsidian_errors
def coach_weak(ctx: click.Context, limit: int, days: int) -> None:
    """Subjects and notes the student keeps forgetting."""
    from ..tools.obsidian.coach import coach_status, weak_topics

    cfg, path = _coach_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    rows = weak_topics(path, cfg, limit=limit, days=days)
    if not rows:
        say("Nothing reviewed yet — no weak spots to report.")
        return
    for topic in rows:
        if topic["reviews"]:
            say(f"  {topic['subject']} · {topic['topic']} — {topic['lapses']} lapse(s) in "
                f"{topic['reviews']} review(s) ({topic['retention']}% retention), "
                f"{topic['due']} due")
        else:
            say(f"  {topic['subject']} · {topic['topic']} — {topic['new']} unreviewed, "
                f"{topic['due']} due")
    say("")
    md(coach_status(path, cfg))


@coach.command("weekly")
@click.option("--day", default="", help="Any day of the week (YYYY-MM-DD).")
@click.pass_context
@_obsidian_errors
def coach_weekly(ctx: click.Context, day: str) -> None:
    """Write the weekly review note (retention table + next week's focus)."""
    from datetime import date

    from ..tools.obsidian.coach import weekly_review

    cfg, path = _coach_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    target = date.fromisoformat(day) if day else None
    result = weekly_review(path, cfg, day=target)
    say(f"✅ weekly review → {result['note']} ({result['week'][0]} → {result['week'][1]})")


@coach.command("watch")
@click.option("--at", "times", default="", help='Times like "19:30" or "07:00,19:30".')
@click.option("--notify/--no-notify", default=None, help="Desktop notification.")
@click.option("--speak/--no-speak", default=None, help="Read the headline aloud.")
@click.option("--weekly/--no-weekly", default=None, help="Sunday review note.")
@click.option("--once", is_flag=True, help="Run one cycle immediately, then exit.")
@click.option("--iterations", default=0, type=int, help="Stop after N cycles (testing).")
@click.pass_context
@_obsidian_errors
def coach_watch(ctx: click.Context, times: str, notify: bool, speak: bool, weekly: bool,
                once: bool, iterations: int) -> None:
    """Sit in the background and write the brief at the scheduled times."""
    from ..tools.obsidian.coach import parse_times, run_once, watch

    cfg, path = _coach_vault(ctx.obj.get("vault", "") if ctx.obj else "")
    settings = cfg.get("obsidian", {}).get("coach", {}) if isinstance(cfg, dict) else {}
    notify = bool(settings.get("notify", True)) if notify is None else notify
    speak = bool(settings.get("speak", False)) if speak is None else speak
    weekly = bool(settings.get("weekly_review", True)) if weekly is None else weekly
    schedule = parse_times(times or settings.get("times") or ["19:30"])

    if once:
        run = run_once(path, cfg, write=True, notify=notify, speak=speak, weekly=weekly)
        say(f"✅ {run.line()}")
        return
    say(f"🕒 Study coach watching — brief at {', '.join(schedule)} "
        f"(notify={notify}, speak={speak}, weekly={weekly}). Ctrl-C to stop.")
    try:
        runs = watch(path, cfg, times=schedule, notify=notify, speak=speak,
                     weekly=weekly, iterations=iterations or None,
                     on_run=lambda run: say(f"  {run.line()}"))
    except KeyboardInterrupt:
        say("\n👋 Coach stopped.")
        return
    if iterations:
        say(f"ran {len(runs)} cycle(s)")


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
