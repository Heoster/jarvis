"""Tool contract, registry behaviour and the offline-testable tool families."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── tool contract ─────────────────────────────────────────────────────────
def test_tool_decorator_infers_schema():
    from jarvis.tools.base import ToolResult, tool

    @tool(category="utility", name="demo_tool", timeout=5)
    def demo(query: str, limit: int = 5, exact: bool = False) -> str:
        """Do a demo thing.

        Args:
            query: what to look for
            limit: how many results
            exact: require an exact match
        """
        return f"{query}:{limit}:{exact}"

    assert demo.name == "demo_tool"
    assert demo.category == "utility"
    schema = demo.schema()["function"]          # OpenAI function-tool shape
    assert schema["name"] == "demo_tool"
    properties = schema["parameters"]["properties"]
    assert set(properties) == {"query", "limit", "exact"}
    assert properties["limit"]["default"] == 5
    assert schema["parameters"]["required"] == ["query"]
    assert "what to look for" in properties["query"]["description"]

    result = demo.invoke({"query": "jarvis", "limit": 2})
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.content == "jarvis:2:False"
    assert result.elapsed_ms >= 0


def test_tool_reports_exceptions_instead_of_raising():
    from jarvis.tools.base import tool

    @tool(category="utility")
    def broken(value: int) -> str:
        """Always fails.

        Args:
            value: ignored
        """
        raise ValueError("expected failure")

    result = broken.invoke({"value": 1})
    assert result.ok is False
    assert "expected failure" in (result.error or "")


def test_tool_coerces_string_arguments():
    from jarvis.tools.base import tool

    @tool(category="utility")
    def add(a: int, b: int = 1) -> str:
        """Adds two numbers.

        Args:
            a: first
            b: second
        """
        return str(a + b)

    # small models often send numbers as strings — the tool layer must cope
    assert add.invoke({"a": "2", "b": "3"}).content == "5"
    assert add.invoke({"a": 2}).content == "3"
    # unknown keys are dropped instead of blowing up the call
    assert add.invoke({"a": 2, "bogus": "x"}).ok


# ── registry ──────────────────────────────────────────────────────────────
def test_registry_loads_every_package(registry):
    stats = registry.stats()
    assert stats["tools"] >= 90, "all tool packages must load"
    categories = registry.categories()
    for expected in ("browser", "screen", "memory", "search", "crawl", "code",
                     "file", "utility", "communication"):
        assert expected in categories, f"missing category {expected}"


def test_registry_execute_and_errors(registry):
    result = registry.execute_sync("calculate", {"expression": "6*7"})
    assert result.ok and "42" in result.content

    missing = registry.execute_sync("no_such_tool", {})
    assert missing.ok is False
    assert "unknown" in (missing.error or "").lower()


def test_registry_execute_many_runs_in_parallel(registry):
    import asyncio
    import time

    calls = [{"name": "calculate", "args": {"expression": f"{i}+{i}"}}
             for i in range(6)]
    started = time.perf_counter()
    results = asyncio.run(registry.execute_many(calls, parallel=True))
    elapsed = time.perf_counter() - started
    assert len(results) == 6
    assert all(result.ok for result in results)
    assert elapsed < 3.0


def test_registry_reports_missing_capabilities_clearly(registry):
    """Capability reports must tell the truth about this machine."""
    result = registry.execute_sync("take_desktop_screenshot", {})
    if result.ok:
        # On a machine with a real display the screenshot succeeds — that is
        # the capability being "clearly reported" (a non-empty path/content).
        assert (result.content or "").strip(), "successful screenshot must return a path"
    else:
        message = f"{result.content} {result.error}".lower()
        assert "display" in message or "install" in message or "backend" in message


# ── filesystem ────────────────────────────────────────────────────────────
def test_filesystem_roundtrip(config, home, registry):
    target = home / "notes.txt"
    write = registry.execute_sync("write_file", {"path": str(target),
                                                 "content": "hello jarvis"})
    assert write.ok, write.error
    assert target.read_text() == "hello jarvis"

    read = registry.execute_sync("read_file", {"path": str(target)})
    assert read.ok and "hello jarvis" in read.content

    append = registry.execute_sync("append_file", {"path": str(target),
                                                   "content": "\nsecond line"})
    assert append.ok
    assert target.read_text().count("\n") == 1

    listing = registry.execute_sync("list_directory", {"directory": str(home)})
    assert listing.ok and "notes.txt" in listing.content

    # a wrong parameter name must be reported, not silently ignored
    wrong = registry.execute_sync("list_directory", {"path": str(home)})
    assert not wrong.ok and "valid arguments" in wrong.error


def test_delete_goes_to_trash_not_oblivion(config, home, registry, monkeypatch):
    monkeypatch.setenv("JARVIS_TRASH_DIR", str(home / "data" / "trash"))
    target = home / "throwaway.txt"
    target.write_text("bye")
    result = registry.execute_sync("delete_path", {"path": str(target)})
    assert result.ok, result.error
    assert not target.exists()
    trash = home / "data" / "trash"
    assert trash.exists(), "deletes must be recoverable"
    assert any("throwaway" in str(path) for path in trash.rglob("*"))


def test_file_search_finds_content(config, home, registry):
    (home / "a.py").write_text("def hello():\n    return 'needle_jelly'\n")
    (home / "b.txt").write_text("nothing here")
    result = registry.execute_sync("file_search", {"pattern": "needle_jelly",
                                                   "root": str(home)})
    assert result.ok and "a.py" in result.content


# ── code ──────────────────────────────────────────────────────────────────
def test_run_python_executes_and_captures_output(config, registry):
    result = registry.execute_sync("run_python",
                                   {"code": "print(6*7)", "timeout": 15})
    assert result.ok, result.error
    assert "42" in result.content


def test_run_python_refuses_syntax_errors_before_running(config, registry):
    result = registry.execute_sync("run_python",
                                   {"code": "print(1", "timeout": 15})
    assert not result.ok
    assert "syntax" in (result.error or "").lower()


def test_run_python_times_out_cleanly(config, registry):
    result = registry.execute_sync("run_python",
                                   {"code": "import time\nwhile True: time.sleep(0.2)",
                                    "timeout": 2})
    assert not result.ok
    assert "time" in (result.error or "").lower()


def test_destructive_bash_needs_confirmation(config, registry):
    # `rm -rf` of a real path is on the blocked/destructive list: it must not run
    result = registry.execute_sync("run_bash",
                                   {"command": "rm -rf /tmp/definitely-not-real"})
    assert not result.ok, "a destructive command must not run unconfirmed"
    message = f"{result.error} {result.content}".lower()
    assert "confirm" in message or "blocked" in message

    # a *confirmable* destructive command reports needs-confirmation, not blocked
    confirmable = registry.execute_sync("run_bash", {"command": "rm -rf ./build"})
    assert not confirmable.ok
    assert "confirm" in f"{confirmable.error} {confirmable.content}".lower()

    # ... and an explicit confirmation lets it through
    confirmed = registry.execute_sync("run_bash",
                                      {"command": "rm -rf /tmp/jarvis-not-real",
                                       "confirm_destructive": True})
    combined = f"{confirmed.error}{confirmed.content}".lower()
    assert (confirmed.ok
            or "no such file" in combined
            or "not recognized" in combined   # Windows: 'rm' is not a cmd
            or "cannot find" in combined)


def test_safe_bash_runs(config, registry):
    result = registry.execute_sync("run_bash", {"command": "echo jarvis-ok"})
    assert result.ok and "jarvis-ok" in result.content


# ── utility ───────────────────────────────────────────────────────────────
def test_calculator_is_exact_and_safe(config, registry):
    result = registry.execute_sync("calculate", {"expression": "0.1 + 0.2",
                                                 "exact": True})
    assert result.ok and "0.3" in result.content

    danger = registry.execute_sync("calculate",
                                   {"expression": "__import__('os').system('ls')"})
    assert not danger.ok, "the calculator must never execute code"


def test_datetime_uses_ist_by_default(config, registry):
    result = registry.execute_sync("get_current_datetime", {})
    assert result.ok
    assert "IST" in result.content or "Kolkata" in result.content or "+05:30" in result.content


def test_json_tools(config, registry):
    payload = json.dumps({"user": {"name": "Heoster"}, "items": [1, 2, 3]})
    parsed = registry.execute_sync("json_parse", {"text": payload})
    assert parsed.ok and "Heoster" in parsed.content
    queried = registry.execute_sync("json_query", {"text": payload,
                                                   "path": "items.1"})
    assert queried.ok and "2" in queried.content


def test_text_stats(config, registry):
    result = registry.execute_sync("text_stats", {"text": "one two three four"})
    assert result.ok and "words" in result.content


# ── search & crawl (offline: providers must fail loudly, not hang) ────────
def test_search_reports_missing_providers_without_hanging(config, registry, monkeypatch):
    """With no keys, no SearXNG and no network, search must explain itself."""
    monkeypatch.setattr("jarvis.config.get_config", lambda: config, raising=False)
    result = registry.execute_sync("search_web", {"query": "anything"})
    message = f"{result.content} {result.error}".lower()
    assert "provider" in message or "no search" in message


def test_search_quota_is_enforced(config, monkeypatch):
    from jarvis.tools.search import base
    from jarvis.tools.search.base import SearchQuotaExceeded, _quota_check

    class _State:
        used, limit, unit, period = 1000, 1000, "request", "month"

    class _Never:
        def allow(self, provider: str, cost: int = 1, **kwargs) -> bool:
            return False

        def state(self, provider: str):
            return _State()

    monkeypatch.setattr(base, "get_quota_manager", lambda: _Never())
    with pytest.raises(SearchQuotaExceeded):
        _quota_check("tavily", 1)


def test_read_url_reports_failure_cleanly(config):
    """No network → the crawl chain raises a clear error, not a hang."""
    from jarvis.tools.crawl.reader import read_url_text
    from jarvis.tools.search.base import SearchError

    try:
        outcome = read_url_text("https://example.com")
    except SearchError as exc:
        assert "example.com" in str(exc) or "could not" in str(exc).lower()
    else:
        assert outcome["provider"] == "" or not outcome["text"]
        assert isinstance(outcome["attempts"], list)


def test_crawl_report_lists_providers(config, registry):
    result = registry.execute_sync("crawl_tools_report")
    assert result.ok
    report = result.content.lower()
    assert "jina" in report and "firecrawl" in report


# ── memory tools ──────────────────────────────────────────────────────────
def test_memory_tools_store_and_recall(config, registry):
    stored = registry.execute_sync("remember", {"fact": "Heoster is building JARVIS",
                                                "importance": 0.9})
    assert stored.ok, stored.error
    recalled = registry.execute_sync("recall", {"query": "what is Heoster building"})
    assert recalled.ok and "JARVIS" in recalled.content
    stats = registry.execute_sync("memory_stats", {})
    assert stats.ok and "long-term" in stats.content


def test_tool_discovery_tools(config, registry):
    listed = registry.execute_sync("list_available_tools", {"category": "screen"})
    assert listed.ok and "cursor_click" in listed.content
    help_text = registry.execute_sync("tool_help", {"tool_name": "cursor_click"})
    assert help_text.ok
    assert "Arguments" in help_text.content and "required" in help_text.content


# ── honest failure contract (focus areas) ─────────────────────────────────
def test_screen_tools_never_report_a_hollow_success(registry):
    """A screen tool either does the job or says why it could not.

    This deliberately does not assume the machine is headless: a Windows runner
    has a real 1024×768 session, so ``list_screen_monitors`` genuinely works
    there. What must never happen is ``ok=True`` with nothing behind it, or
    ``ok=False`` with an empty reason — that is the bug this contract exists to
    catch, on whichever box the suite runs.
    """
    for name, args in [("take_desktop_screenshot", {}),
                       ("list_screen_monitors", {}),
                       ("read_screen_text", {}),
                       ("find_on_screen", {"text": "Save"}),
                       ("screen_click_text", {"text": "Save"}),
                       ("cursor_move", {"x": 5, "y": 5}),
                       ("cursor_click", {"x": 5, "y": 5}),
                       ("keyboard_type", {"text": "hi"}),
                       ("keyboard_press", {"key": "enter"}),
                       ("screen_scroll", {}),
                       ("screen_list_windows", {}),
                       ("screen_focus_window", {"title": "nothing-here"}),
                       ("screen_active_window", {}),
                       ("launch_application", {"name": "definitely-not-installed"})]:
        result = registry.execute_sync(name, args)
        if result.ok:
            assert (result.content or "").strip(), \
                f"{name} claimed success but returned nothing"
        else:
            assert (result.error or "").strip(), f"{name} failed without a reason"


def test_headless_browser_tools_report_failure_not_success(registry):
    for name, args in [("browse_open", {"url": "example.com"}),
                       ("page_read", {}), ("page_click", {"target": "1"}),
                       ("page_type", {"target": "Search", "text": "x"}),
                       ("page_press", {"key": "Enter"}), ("page_scroll", {}),
                       ("page_back", {}), ("page_forward", {}),
                       ("page_tabs", {}), ("page_screenshot", {}),
                       ("browser_launch", {}), ("screenshot_page", {})]:
        result = registry.execute_sync(name, args)
        assert result.ok is False, f"{name} claimed success: {result.content[:80]}"
        reason = (result.error or "").lower()
        assert any(word in reason for word in
                   ("playwright", "selenium", "chrome", "display", "no browser")), \
            f"{name} gave an unhelpful reason: {result.error[:120]}"


def test_failure_helper_is_the_standard_refusal_shape():
    from jarvis.tools.base import failure, tool

    @tool(category="utility")
    def never_works() -> str:
        """Always fails (test fixture)."""
        return failure("cannot do that here")

    result = never_works.invoke({})
    assert result.ok is False and "cannot do that here" in result.error


def test_search_and_crawl_fail_loudly_offline(registry):
    """With no network and no keys, search/crawl must not look successful."""
    search = registry.execute_sync("search_web", {"query": "anything at all"})
    assert search.ok is False and search.error
    news = registry.execute_sync("search_news", {"query": "anything at all"})
    assert news.ok is False and news.error
    read = registry.execute_sync("read_url", {"url": "https://example.com"})
    assert read.ok is False and read.error
