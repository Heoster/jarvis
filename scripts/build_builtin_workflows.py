"""Write skills/builtin_workflows.json — the 20 ready-made JARVIS workflows.

Run:  python scripts/build_builtin_workflows.py

Each workflow is a skill: a trigger (the words an operator would use), the
tool steps to run in order, and a note per step.  The skill library turns a
matching skill into a plan hint in the system prompt, so the model follows
the recipe instead of improvising.  Placeholders such as {{query}} are filled
by the model from the request.  Edit the JSON by hand or re-run this script.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "skills" / "builtin_workflows.json"


def step(tool: str, _note: str = "", **args):
    """One workflow step.  ``_note`` is the human hint; ``**args`` are the tool arguments."""
    return {"tool": tool, "args": args, "note": _note}


WORKFLOWS = [
    {
        "name": "open_youtube_and_play",
        "description": "Play a song: search YouTube, read the results through Jina, open the first trusted video.",
        "trigger": "play open youtube yt song video music bajao chalao sunao listen",
        "tags": ["media", "browser"],
        "steps": [
            step("play_song", "One call does the whole pipeline: builds the YouTube search URL, "
                 "reads it with Jina Reader, takes the first trusted watch link and opens it. "
                 "Pass only the song name as query.", query="{{song}}", new_tab=True),
            step("", "Report the title and URL that play_song opened. Do not say it is playing "
                 "unless media_play has confirmed the player state."),
        ],
    },
    {
        "name": "play_random_song",
        "description": "Play a random song: pick one from the play history, or a popular search.",
        "trigger": "play random song any music shuffle yt youtube surprise me",
        "tags": ["media", "browser"],
        "steps": [
            step("media_history", "Get recent plays. Pick one title at random. If the list is "
                 "empty, use the query \"popular songs\" in the next step.", limit=20),
            step("play_song", "Play the picked title (or the popular-songs query).",
                 query="{{random_pick_or_popular_songs}}", new_tab=True),
        ],
    },
    {
        "name": "open_website",
        "description": "Open a website in Chrome, reusing the open window when possible.",
        "trigger": "open website go to visit site url link browser chrome kholo",
        "tags": ["browser"],
        "steps": [
            step("browser_status", "Reuse an open Chrome window if one exists."),
            step("browse_open", "Open the address in a new tab and read the page.",
                 url="{{url}}", new_tab=True, read_page=True),
        ],
    },
    {
        "name": "search_web_and_answer",
        "description": "Search the web, read the best result and answer with sources.",
        "trigger": "search find look up latest news research who what best compare",
        "tags": ["search", "research"],
        "steps": [
            step("search_web", "Search the question.", query="{{question}}", max_results=5),
            step("read_url", "Read the most relevant result in full.",
                 url="{{best_result_url}}", max_chars=6000),
            step("", "Answer in a few lines and list the source URLs you used."),
        ],
    },
    {
        "name": "read_page_and_summarise",
        "description": "Read one article or page and summarise it.",
        "trigger": "read article summarise summarize url link page text tl;dr",
        "tags": ["crawl", "research"],
        "steps": [
            step("read_url", "Fetch the page as clean text.", url="{{url}}", max_chars=8000),
            step("", "Summarise in 5 bullet points and quote the key figures exactly."),
        ],
    },
    {
        "name": "fill_web_form",
        "description": "Open a site, fill a form field by field and stop before submitting.",
        "trigger": "fill form login sign up register website browser type enter",
        "tags": ["browser", "forms"],
        "steps": [
            step("browse_open", "Open the form page.", url="{{url}}", new_tab=True,
                 read_page=True),
            step("page_find", "Confirm the field is on the page.", text="{{field_label}}"),
            step("page_type", "Type into the field. Never type passwords or OTPs you were "
                 "not given in this request.", target="{{field_label}}", text="{{value}}"),
            step("", "Show the operator what is filled. Submit ONLY after they confirm."),
        ],
    },
    {
        "name": "screenshot_and_describe_page",
        "description": "Take a screenshot of the open page and describe what is on it.",
        "trigger": "screenshot screen capture page look see what is on website",
        "tags": ["browser", "vision"],
        "steps": [
            step("browse_open", "Open the page if it is not already open.", url="{{url}}",
                 new_tab=True, read_page=True),
            step("screenshot_and_read", "Capture and read the visible content."),
        ],
    },
    {
        "name": "take_note_to_file",
        "description": "Write a note to a text or markdown file.",
        "trigger": "note write save file text document notes jot down",
        "tags": ["file"],
        "steps": [
            step("write_file", "Write the note. Use a path inside the operator's folder.",
                 path="{{path}}", content="{{content}}"),
        ],
    },
    {
        "name": "summarise_folder",
        "description": "List a folder, read the most relevant files and summarise them.",
        "trigger": "folder files directory summarise summarize documents list contents",
        "tags": ["file"],
        "steps": [
            step("list_directory", "See what is in the folder.", directory="{{folder}}",
                 max_depth=1),
            step("read_file", "Read the up-to-three files that match the request.",
                 path="{{file}}", max_chars=4000),
            step("", "Summarise each file in one line, then the common theme."),
        ],
    },
    {
        "name": "find_file_by_name",
        "description": "Locate a file on disk by its name.",
        "trigger": "find file locate where search document pdf path",
        "tags": ["file"],
        "steps": [
            step("find_files_by_name", "Search by name pattern.", name_pattern="{{name}}",
                 root="{{root}}", files_only=True),
        ],
    },
    {
        "name": "remember_fact",
        "description": "Store a fact the operator wants kept, then check it was saved.",
        "trigger": "remember note this fact save store my preference deadline",
        "tags": ["memory"],
        "steps": [
            step("remember", "Store the fact in plain words.", fact="{{fact}}", importance=0.8),
            step("recall", "Read it back to confirm it was stored.", query="{{fact}}", k=3),
        ],
    },
    {
        "name": "recall_past_information",
        "description": "Look up something the operator told JARVIS or played earlier.",
        "trigger": "recall what did i tell you remember when last earlier history",
        "tags": ["memory"],
        "steps": [
            step("recall", "Search long-term memory for the topic.", query="{{topic}}", k=5),
            step("media_history", "If the question is about music or video, check plays too.",
                 query="{{topic}}", limit=10),
        ],
    },
    {
        "name": "time_in_timezone",
        "description": "Give the current time in a timezone, or convert a time between zones.",
        "trigger": "time date timezone clock now what time convert zone",
        "tags": ["utility"],
        "steps": [
            step("get_current_datetime", "Current time in the requested zone.",
                 timezone_name="{{timezone}}"),
            step("convert_timezone", "Only for a conversion request.", moment="{{moment}}",
                 to_timezone="{{to_timezone}}", from_timezone="{{from_timezone}}"),
        ],
    },
    {
        "name": "calculate_and_percentage",
        "description": "Do arithmetic exactly and work out percentages.",
        "trigger": "percent percentage calculate calculation maths math sum divide",
        "tags": ["utility"],
        "steps": [
            step("calculate", "Exact arithmetic on the expression.", expression="{{expression}}",
                 exact=True),
            step("percentage", "For 'X is what % of Y' questions.", part="{{part}}",
                 whole="{{whole}}"),
        ],
    },
    {
        "name": "flashcard_revision_session",
        "description": "Pull the due Obsidian flashcards and quiz the operator, one card at a time.",
        "trigger": "flashcards revise revision study review cards quiz",
        "tags": ["study"],
        "steps": [
            step("obsidian_flashcards_due", "Get the cards due today.", limit=10),
            step("obsidian_flashcards_review", "Ask one card's question; after the answer, "
                 "record the grade.", note="{{note}}", question="{{question}}",
                 grade="{{grade}}"),
        ],
    },
    {
        "name": "make_exam_study_plan",
        "description": "Build a day-by-day revision plan up to an exam date and save it to the daily note.",
        "trigger": "study plan exam timetable schedule revision plan prepare test",
        "tags": ["study"],
        "steps": [
            step("obsidian_study_plan", "Build the plan from the exam date.",
                 exam_date="{{exam_date}}", session_minutes=60, write_daily=True),
        ],
    },
    {
        "name": "run_code_and_fix",
        "description": "Run a Python snippet, read any error, fix it and run again (max 3 tries).",
        "trigger": "run code python script debug error fix test traceback",
        "tags": ["code"],
        "steps": [
            step("run_python", "Run the code and read stdout and stderr.", code="{{code}}"),
            step("", "If it failed, change only the broken line and run again. Stop after 3 tries "
                 "and report the last error."),
        ],
    },
    {
        "name": "clipboard_word_count",
        "description": "Read the clipboard and report its length, words and reading time.",
        "trigger": "clipboard copy paste words count text stats characters",
        "tags": ["utility"],
        "steps": [
            step("clipboard_get", "Read the clipboard text."),
            step("text_stats", "Count characters, words and lines.", text="{{clipboard_text}}"),
        ],
    },
    {
        "name": "check_system_status",
        "description": "Report the machine's CPU, memory, disk and OS details.",
        "trigger": "system info machine ram cpu disk status specs computer",
        "tags": ["system"],
        "steps": [
            step("system_info", "Read the system details."),
        ],
    },
    {
        "name": "draft_email_for_review",
        "description": "Draft an email and show it. Never send without the operator's yes.",
        "trigger": "email mail draft write message to send compose",
        "tags": ["communication"],
        "steps": [
            step("draft_email", "Create the draft only.", to="{{to}}", subject="{{subject}}",
                 body="{{body}}"),
            step("", "Show the draft and ask: send it? Send only after a clear yes."),
        ],
    },
]


def build() -> Path:
    for workflow in WORKFLOWS:
        workflow.setdefault("source", "builtin")
        workflow.setdefault("uses", 0)
        workflow.setdefault("successes", 0)
        workflow.setdefault("failures", 0)
    names = [w["name"] for w in WORKFLOWS]
    assert len(names) == len(set(names)), "duplicate workflow name"
    assert 10 <= len(WORKFLOWS) <= 20, "the set is meant to hold 10–20 workflows"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(WORKFLOWS, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return OUT


if __name__ == "__main__":
    print(f"wrote {len(WORKFLOWS)} workflows to {build()}")
