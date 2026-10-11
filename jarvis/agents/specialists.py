"""The specialist sub-agents: browser, research, code, file, screen.

Each has a narrow toolbox and a prompt tuned for its job.  On a CPU-only box that
matters: a browser agent with 8 tools makes far fewer mistakes than the full
100-tool loop, and it costs a fraction of the context.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .base_agent import AgentSpec, BaseAgent

__all__ = ["BrowserAgent", "ResearchAgent", "CodeAgent", "FileAgent",
           "ScreenAgent", "MemoryAgent", "AGENT_CLASSES", "get_agent"]


class BrowserAgent(BaseAgent):
    """Drives the real Chrome window: navigate, read, click, type."""

    spec = AgentSpec(
        name="browser",
        description="Websites and web apps: open pages, log in, click, extract.",
        categories=("browser", "utility"),
        tools=("browse_open", "page_read", "page_click", "page_type", "page_press",
               "page_scroll", "page_back", "page_forward", "page_tabs",
               "page_screenshot", "page_js", "page_find", "page_screenshot",
               "browser_status", "browser_engines", "browser_launch",
               "read_url", "search_web", "get_current_datetime"),
        max_iterations=8, max_tool_calls=14,
        prompt=(
            "You are JARVIS's BROWSER agent. You operate a real Chrome window.\n"
            "Rules:\n"
            "1. To read a page's content, prefer `read_url` (free, fast). Use the\n"
            "   browser only when you must interact (click, type, log in, scroll).\n"
            "2. After `browse_open` you get a numbered element list — click by index:\n"
            "   `page_click(target=\"12\")`.\n"
            "3. If a page did not change after clicking, re-read it with `page_read`\n"
            "   before trying again. Never repeat the same click blindly.\n"
            "4. Type into search boxes with `page_type`, then wait and re-read.\n"
            "5. Report the exact URLs and the facts you extracted. No filler."),
    )


class ResearchAgent(BaseAgent):
    """Multi-source research: search wide, read the best pages, synthesise."""

    spec = AgentSpec(
        name="research",
        description="Answer questions from multiple sources with citations.",
        categories=("search", "crawl", "memory", "utility"),
        tools=("search_web", "parallel_search", "search_news", "read_url",
               "read_many", "jina_read_url", "local_read_url", "search_memory",
               "remember", "calculate", "get_current_datetime"),
        max_iterations=7, max_tool_calls=12,
        prompt=(
            "You are JARVIS's RESEARCH agent.\n"
            "Method: (1) search, (2) read the 2–4 most promising URLs, (3) compare,\n"
            "(4) answer with inline source URLs.\n"
            "Rules:\n"
            "- Use `parallel_search` when the question benefits from several angles.\n"
            "- `read_url` is the cheapest way to get page content; `read_many` reads\n"
            "  several pages in one step.\n"
            "- Separate what sources say from your own inference.\n"
            "- If sources disagree, say so explicitly.\n"
            "- Finish with a 2–4 sentence answer plus a bullet list of sources."),
    )


class CodeAgent(BaseAgent):
    """Writes, runs and debugs code; inspects repositories."""

    spec = AgentSpec(
        name="code",
        description="Write/run/debug code, analyse projects, use the shell.",
        categories=("code", "file", "utility"),
        tools=("run_python", "analyze_python", "run_bash", "code_outline",
               "code_find_symbol", "code_project_summary", "read_file",
               "write_file", "file_search", "list_directory", "calculate",
               "which_command", "system_info"),
        max_iterations=8, max_tool_calls=16,
        prompt=(
            "You are JARVIS's CODE agent.\n"
            "Rules:\n"
            "- Always RUN the code you write (`run_python`) before claiming it works.\n"
            "- Read the actual error text; fix the cause, not the symptom.\n"
            "- Before editing an existing file, read it (use `code_outline` for big\n"
            "  files instead of reading everything).\n"
            "- Destructive shell commands need confirmation — prefer safe variants.\n"
            "- Report the final code and the observed output."),
    )


class FileAgent(BaseAgent):
    """Filesystem work: organise, search, convert, summarise documents."""

    spec = AgentSpec(
        name="file",
        description="Files and folders: find, read, move, write, summarise.",
        categories=("file", "utility", "crawl"),
        tools=("list_directory", "directory_size", "read_file", "write_file",
               "append_file", "delete_path", "move_path", "copy_path", "file_info",
               "file_search", "find_files_by_name", "read_url", "text_stats"),
        max_iterations=6, max_tool_calls=12,
        prompt=(
            "You are JARVIS's FILE agent.\n"
            "Rules:\n"
            "- Look before you leap: list the directory before assuming paths.\n"
            "- Reads are safe; writes are logged; deletes go to the trash.\n"
            "- Never claim a file operation worked without the tool confirming it.\n"
            "- Keep large reads partial (use max_chars / file_search)."),
    )


class ScreenAgent(BaseAgent):
    """Desktop automation with the real cursor and OCR (no vision model)."""

    spec = AgentSpec(
        name="screen",
        description="Control apps on screen with the mouse/keyboard and read it via OCR.",
        categories=("screen", "utility"),
        tools=("launch_application", "screen_list_windows", "screen_focus_window",
               "screen_active_window", "screen_window_action", "take_desktop_screenshot",
               "read_screen_text", "find_on_screen", "screen_click_text",
               "cursor_move", "cursor_click", "keyboard_type", "keyboard_press",
               "screen_cursor_position", "screen_scroll", "list_screen_monitors"),
        max_iterations=8, max_tool_calls=16,
        prompt=(
            "You are JARVIS's SCREEN agent. You have no vision model — you read the\n"
            "screen as TEXT via OCR and act with the real mouse/keyboard.\n"
            "Method:\n"
            "1. `screen_list_windows` / `screen_focus_window` to get to the right app.\n"
            "2. `read_screen_text` to see what is on screen.\n"
            "3. `screen_click_text(\"label\")` to click a visible label, or\n"
            "   `cursor_click(x, y)` when you have coordinates.\n"
            "4. After each action, read the screen again to verify it worked.\n"
            "Rules: always focus a window before typing; never guess coordinates\n"
            "without reading the screen first; report exactly what changed."),
    )


class MemoryAgent(BaseAgent):
    """Curates memory: consolidates, searches and summarises what JARVIS knows."""

    spec = AgentSpec(
        name="memory",
        description="Search/curate long-term memory, profile and knowledge base.",
        categories=("memory", "utility"),
        tools=("recall", "search_memory", "remember", "forget", "memory_stats",
               "read_file", "list_directory"),
        max_iterations=5, max_tool_calls=10,
        prompt=(
            "You are JARVIS's MEMORY agent.\n"
            "Answer questions about what JARVIS knows (about Heoster, past sessions,\n"
            "past tasks) by searching memory, then summarise clearly.\n"
            "Never invent a memory — quote what `recall`/`search_memory` returned."),
    )


AGENT_CLASSES = {
    "browser": BrowserAgent,
    "research": ResearchAgent,
    "code": CodeAgent,
    "file": FileAgent,
    "screen": ScreenAgent,
    "memory": MemoryAgent,
}


def get_agent(name: str, config: Mapping[str, Any], **kwargs: Any) -> BaseAgent:
    """Instantiate one specialist by name."""
    agent_cls = AGENT_CLASSES.get(str(name).lower())
    if agent_cls is None:
        raise KeyError(f"unknown agent '{name}'. Available: {', '.join(AGENT_CLASSES)}")
    return agent_cls(config, **kwargs)
