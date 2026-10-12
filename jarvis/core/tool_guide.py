"""When and how to use each core tool, written for a small model.

The system prompt includes the lines for the tools bound to the current turn, so
the model sees "WHEN" and "HOW" next to the tool name, not just a schema.  Keep
each entry to one short line per field.  Tools without an entry fall back to the
first line of their own description.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

__all__ = ["TOOL_GUIDE", "guide_lines"]

#: tool name -> (WHEN to use it, HOW to call it)
TOOL_GUIDE: Dict[str, Tuple[str, str]] = {
    "play_song": (
        "operator asks to play / listen to / bajao / chalao a song or video",
        "query = the song or artist name only; reports the video it opened",
    ),
    "media_play": (
        "playback must be verified (player state, autoplay) after a song is opened",
        "query or url; use play_song first, this one only when verification matters",
    ),
    "media_history": (
        "operator asks what they played before, or for a random earlier song",
        "limit=20; pick an entry yourself, then play it with play_song",
    ),
    "jina_read_url": (
        "you need the text of a known web page (search results, articles, docs)",
        "url = full https URL; returns markdown with links",
    ),
    "read_url": (
        "you need the text of one page; falls back to other readers if Jina fails",
        "url = full https URL; max_chars to limit size",
    ),
    "search_web": (
        "you need current facts or sources you do not already know",
        "query = a short, specific search; then read_url the best result",
    ),
    "browse_open": (
        "operator asks to open, visit or show a website in Chrome",
        "url = full URL; new_tab=true to keep the current page",
    ),
    "browser_status": (
        "before opening Chrome, to reuse an open window",
        "no arguments",
    ),
    "page_read": (
        "you must read what is on the open Chrome page",
        "max_chars to limit the text",
    ),
    "page_find": (
        "you need to check that some text or element is on the open page",
        "text = the words to find",
    ),
    "page_click": (
        "you must press a button or link on the open page",
        "target = the [index] or visible label from the page snapshot",
    ),
    "page_type": (
        "a form field on the open page must receive text",
        "target = the field label; text = what to type; press_enter only when asked to submit",
    ),
    "page_press": (
        "a key must be pressed on the open page (Enter, Escape, k)",
        "key = the key name",
    ),
    "read_file": (
        "the operator names a file and you need its contents",
        "path = the full file path; max_chars to limit",
    ),
    "write_file": (
        "the operator asks to save or write text into a file",
        "path + content; never overwrite a file the operator did not name",
    ),
    "list_directory": (
        "the operator asks what is inside a folder",
        "directory = the folder path; max_depth=1 for a quick look",
    ),
    "find_files_by_name": (
        "the operator asks where a file is",
        "name_pattern = the file name, wildcards allowed",
    ),
    "remember": (
        "the operator says remember, or states a preference, deadline or fact to keep",
        "fact = one plain sentence",
    ),
    "recall": (
        "the operator asks about something they told you earlier",
        "query = the topic in a few words",
    ),
    "calculate": (
        "any arithmetic or maths expression",
        "expression = the maths only, e.g. (18/100)*2450",
    ),
    "percentage": (
        "'X is what percent of Y' or 'what is P% of Y' style questions",
        "part and whole as numbers",
    ),
    "get_current_datetime": (
        "the operator asks for the current date or time",
        "timezone_name = e.g. Asia/Kolkata (optional)",
    ),
    "convert_timezone": (
        "the operator asks to convert a time between timezones",
        "moment (ISO time), to_timezone, optional from_timezone",
    ),
    "run_python": (
        "a calculation, data check or script must actually run",
        "code = a complete snippet; read stdout and errors, then fix and rerun",
    ),
    "system_info": (
        "the operator asks about this machine's CPU, RAM, disk or OS",
        "no arguments",
    ),
    "clipboard_get": (
        "the operator refers to what they copied",
        "no arguments",
    ),
    "obsidian_study_plan": (
        "the operator asks for a revision or exam plan",
        "exam_date (YYYY-MM-DD) is required; write_daily=true saves it",
    ),
    "draft_email": (
        "the operator asks to write an email",
        "to, subject, body; this only drafts, it never sends",
    ),
    "speak": (
        "the operator asks you to read something out loud",
        "text = the words to say",
    ),
    "list_available_tools": (
        "you are not sure which tool exists for a task",
        "no arguments; then call the tool it names",
    ),
}


def guide_lines(names: Iterable[str], descriptions: Dict[str, str] | None = None,
                limit: int = 12) -> List[str]:
    """Guide lines for the bound tool names, at most ``limit`` of them."""
    descriptions = descriptions or {}
    lines: List[str] = []
    for name in sorted(set(names)):
        if len(lines) >= limit:
            break
        entry = TOOL_GUIDE.get(name)
        if entry is None:
            first = (descriptions.get(name) or "").strip().split("\n")[0][:110]
            if first:
                lines.append(f"- {name}: {first}")
            continue
        when, how = entry
        lines.append(f"- {name}: WHEN {when}. HOW {how}.")
    return lines
