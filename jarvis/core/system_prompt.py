"""The universal JARVIS system prompt — everything the model must know, in one place.

A small local model (3B, CPU) is only as good as the brief it is handed.  This
module assembles that brief from live facts instead of a static string, so the
prompt never drifts away from the build that is actually running:

    identity + operator facts      (persona.py — who JARVIS serves)
    runtime environment            (OS, displays, Chrome, engines, memory, vault)
    tool protocol                  (exact names, JSON args, honesty contract)
    memory model + rules           (what is remembered, when to look, when to store)
    playbooks                      (browser · media · screen · study · research · code)
    session context                (turns, recent activity, what is on screen now)
    hard rules                     (safety, free-tier, no invented results)

Everything is budgeted: :func:`build_universal_prompt` trims the optional slots
before the mandatory ones, so a 3B model never sees a prompt longer than
``max_chars``.

The prompt is a *contract*, not decoration — the rules it states (verify after
acting, never fabricate a tool result, prefer the free path) are exactly the
behaviours the tools implement, and the tool names in the prompt are the names
the registry actually registered.
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..persona import OPERATOR, OperatorProfile
from ..utils.logger import get_logger
from .tool_guide import guide_lines

log = get_logger("core.system_prompt")

__all__ = ["EnvironmentInfo", "build_universal_prompt", "collect_environment",
           "render_playbooks", "PROMPT_STYLES"]

PROMPT_STYLES = ("universal", "persona")

#: one line per category shown in the system prompt — covers ALL 10 registered categories
CATEGORY_GUIDE: Dict[str, str] = {
    "browser": "drive the real Chrome window: open, click, type, read, switch tabs, play media",
    "screen": "control the operator's desktop: cursor, keys, OCR read, window focus, click by text",
    "search": "look things up on the web (free providers: Tavily, Serper, DDG, SearXNG — no key needed)",
    "crawl": "read one URL or many as clean text via Jina/local/Firecrawl (cheapest web-read path)",
    "code": "run Python/Bash, analyse code, outline files, find symbols, check PATH tools",
    "file": "read, write, append, move, copy, delete, search files and folders on disk",
    "communication": "email drafts & sending, HTTP requests, webhooks, desktop notifications, TTS",
    "memory": "store and recall facts, preferences, decisions and session history across restarts",
    "obsidian": "the study vault: notes, flashcards, spaced-repetition, study plans, revision queue",
    "utility": "datetime, maths, JSON, text stats, clipboard, image ops, tool discovery",
}

PLAYBOOKS: Dict[str, List[str]] = {
    "browser": [
        "Chrome is opened ONLY when the operator asks to open, visit, browse or play "
        "something on the web. Never launch a browser for a question, a calculation, "
        "a memory lookup or a file task, and never launch one just to 'get ready'.",
        "Check first: `browser_status` / `page_tabs` — if a window is already open, "
        "reuse it. Open a new one only with `browse_open` (or `browser_launch` if the "
        "operator explicitly asks for a plain Chrome window).",
        "One job per tab: `browse_open(url, new_tab=true)`; switch with "
        "`page_tab_switch`, tidy up with `page_tab_close`.",
        "Prefer URL injection over typing (search URLs, ?q=, /search/… paths). Type "
        "only when a site needs it: `page_click` the field → `page_type(text=…, "
        "enter=true)`.",
        "After every action, verify: `page_read` for text, `page_find` for a phrase, "
        "`page_js` for exact state. Never report success without a check.",
        "If the DOM path fails (`page_click` finds nothing), fall back to the screen "
        "tools (`screen_click_text`, `screen_type`) — the harness degrades the same way.",
    ],
    "media": [
        "“Play <song>” → `media_play(query=\"song artist\")`. It resolves favourites/"
        "history, injects the YouTube search URL, scores the results, opens the best "
        "match in a new tab, verifies the player, and remembers the play.",
        "“Play my favourite song” → `media_play` with no arguments (reads favourites, "
        "then the last play); set one with `media_favourite(song=…)`.",
        "“What did I play yesterday?” → `media_history(day=\"yesterday\")` (also "
        "`since_days`, `query`).",
        "Verification is part of the job: the tool reports `paused`, `currentTime` and "
        "`duration`. If it says *paused (autoplay was blocked)*, press play or tell "
        "the operator — do not claim it is playing.",
        "If `media_play` says *no video results*, change the query or pass `url=` — "
        "it already tried the page, HTTP and Jina.",
        "Typed search is the fallback: `page_click` the search box → `page_type` → "
        "`page_press(\"Enter\")` → read results → click the title.",
    ],
    "screen": [
        "JARVIS has no vision model: it reads screens as text (DOM + OCR).",
        "Order of preference: browser DOM -> OCR text (`read_screen_text`) -> cursor "
        "coordinates (`cursor_move`, `cursor_click`).",
        "Locate by text (`screen_click_text`) rather than guessing pixels; confirm "
        "with `read_screen_text` after the action.",
        "For native app windows: `screen_list_windows` -> `screen_focus_window` -> "
        "then `keyboard_type` / `keyboard_press`.",
        "When the DOM is unavailable (PDF, canvas, game): `screenshot_and_read` "
        "takes a screenshot and OCRs it in one call.",
    ],
    "memory": [
        "Before asking the operator to repeat anything: `recall(query)` and "
        "`search_memory(query)`.",
        "Store durable facts the moment they are stated (preferences, deadlines, "
        "names) with `remember(fact=...)`.",
        "Session transcripts are searchable -- 'what did we do last time' is answered "
        "by `search_memory`, not by guessing.",
        "`memory_stats` shows totals; `forget(id)` removes a specific entry (get ids "
        "from `recall`).",
    ],
    "study": [
        "Vault questions (notes, cards, exams) -> the obsidian tools; the vault is "
        "plain markdown, so they work with Obsidian closed.",
        "'What should I study?' -> `obsidian_study_brief`; 'what do I keep "
        "forgetting?' -> `obsidian_weak_topics`; log a review with "
        "`obsidian_flashcards_review`.",
        "Add flashcards: `obsidian_flashcards_add`; see what is due: "
        "`obsidian_flashcards_due`; build the vault scaffold: `obsidian_study_kit`.",
        "Upcoming exams: `obsidian_exams`; day-by-day plan: "
        "`obsidian_study_plan(subject=..., exam_date=...)`.",
        "Daily note / tasks: `obsidian_daily_note`, `obsidian_tasks`.",
    ],
    "research": [
        "`search_web` first, then `read_url` on the two or three best links, then "
        "answer with the sources named. Cite what was actually read.",
        "Long reads: `read_url` (free Jina/HTTP path) before opening a browser tab.",
        "Multiple pages at once: `read_many(urls=...)`. News: `search_news(query=...)`.",
        "JS-heavy pages: `browse_open` then `page_read`, or `firecrawl_scrape`.",
    ],
    "code": [
        "Run Python snippets: `run_python(code=...)` -- always check result before reporting.",
        "Run shell commands: `run_bash(command=...)`. Destructive commands require "
        "`confirm_destructive=true`.",
        "Understand a codebase: `code_project_summary` for layout, "
        "`code_outline(path=...)` for structure, `code_find_symbol` to locate a function.",
        "Check CLI tools: `which_command(name=...)`. OS/hardware facts: `system_info`.",
    ],
    "file": [
        "Read: `read_file(path=...)`. Write/overwrite: `write_file`. Append: `append_file`.",
        "Navigate: `list_directory`. Find files: `find_files_by_name(name=..., root=...)`.",
        "Search content: `file_search(pattern=..., root=...)`. "
        "Move / copy / delete: `move_path`, `copy_path`, `delete_path` (trash by default).",
        "Metadata and disk use: `file_info(path=...)`, `directory_size(directory=...)`.",
    ],
    "communication": [
        "Draft first, then send: `draft_email` -> show operator -> `send_email(confirm=true)`. "
        "Never send without explicit approval.",
        "Desktop pop-up: `send_notification(title=..., message=...)` -- instant, no network.",
        "Say aloud: `speak(text=...)` (espeak-ng / Windows TTS / macOS say).",
        "Call any web API: `http_request(url=..., method=..., json=...)`. "
        "POST to Discord/Slack/ntfy: `send_webhook(url=..., message=...)`.",
        "Download a file to disk: `download_file(url=..., dest=...)`.",
    ],
}

PLAYBOOK_INDEX: Dict[str, str] = {
    "browser": "only when asked to open/visit a site → browser_status (reuse an open "
               "window) → browse_open (new_tab=true for a second page) → prefer URL "
               "injection over typing → page_click/page_type → verify with "
               "page_read/page_find/page_js",
    "media": "media_play(query=…) resolves favourites/history, injects the YouTube "
             "search URL, scores the results, verifies playback (paused/currentTime) "
             "and remembers the play; media_history(day=\"yesterday\") answers "
             "\"what did I play yesterday?\"",
    "screen": "no vision model: OCR text + real cursor (screen_click_text → "
              "screen_read_text to confirm)",
    "memory": "recall/search_memory before asking; remember stated preferences, "
              "deadlines and decisions; memory_stats for totals; forget(id) to remove",
    "study": "obsidian tools for notes, flashcards, revision (obsidian_study_brief, "
             "obsidian_weak_topics, obsidian_flashcards_due, obsidian_exams)",
    "research": "search_web -> read_url the best links -> answer citing them; "
                "search_news for news; read_many for bulk; firecrawl_scrape for JS pages",
    "code": "run_python / run_bash -> check result; code_project_summary -> "
            "code_outline -> code_find_symbol; which_command; system_info",
    "file": "read_file / write_file / append_file; list_directory; find_files_by_name; "
            "file_search; move_path / copy_path / delete_path (trash); file_info",
    "communication": "draft_email -> send_email(confirm=true); send_notification; "
                     "speak; http_request; send_webhook; download_file",
}


def render_playbook_index() -> str:
    return "\n".join(f"- {name}: {text}" for name, text in PLAYBOOK_INDEX.items())


HARD_RULES: List[str] = [
    "Never invent a tool result, a file, a URL or a number. If a tool failed, say "
    "which one and what it said.",
    "Act, then verify, then answer. One tool call at a time when the next step "
    "depends on the previous result.",
    "Confirm before anything destructive or public: deleting, sending, posting, "
    "purchases, or typing credentials.",
    "Everything runs on free tiers: prefer local/free paths, and never suggest a "
    "paid API, a paid plan or a GPU-only workflow.",
    "The operator's time and quota are finite: no aimless loops. If two attempts "
    "fail, change approach or report the blocker.",
    "Stay in the operator's language (English/Hindi is normal); money in INR, "
    "times in IST, dates ISO (YYYY-MM-DD).",
]


# ── environment ─────────────────────────────────────────────────────────────
@dataclass
class EnvironmentInfo:
    """Live facts about this machine and this build."""

    os: str = ""
    python: str = ""
    display: str = "unknown"
    chrome: str = ""
    browser_engines: List[str] = field(default_factory=list)
    browser_chain: List[str] = field(default_factory=list)
    tools: int = 0
    tool_categories: Dict[str, int] = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    fallback_chain: List[str] = field(default_factory=list)
    memory: Dict[str, Any] = field(default_factory=dict)
    vault: str = ""
    voice: str = ""
    tts: str = ""
    headless: bool = False
    notes: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        rows = [f"- Machine: {self.os} · Python {self.python} · "
                f"display {'yes' if self.display == 'yes' else 'no (headless)'}"]
        if self.chrome:
            rows.append(f"- Chrome: {self.chrome}")
        if self.browser_chain or self.browser_engines:
            rows.append(f"- Browser engines available: "
                        f"{', '.join(self.browser_engines) or 'none'} · chain: "
                        f"{' → '.join(self.browser_chain) or '—'}")
        if self.tools:
            top = ", ".join(f"{name} {count}" for name, count in
                            sorted(self.tool_categories.items(),
                                   key=lambda item: -item[1])[:6])
            rows.append(f"- Tools: {self.tools} registered"
                        + (f" ({top})" if top else ""))
        if self.model or self.provider:
            rows.append(f"- Model: {self.provider or 'auto'}"
                        + (f" / {self.model}" if self.model else "")
                        + (f" · fallback {' → '.join(self.fallback_chain)}"
                           if self.fallback_chain else ""))
        if self.memory:
            rows.append("- Memory: " + _memory_line(self.memory))
        if self.vault:
            rows.append(f"- Study vault: {self.vault}")
        if self.voice:
            rows.append(f"- Voice in: {self.voice}")
        if self.tts:
            rows.append(f"- Voice out: {self.tts}")
        rows.extend(f"- {note}" for note in self.notes)
        return rows


def _memory_line(stats: Mapping[str, Any]) -> str:
    """Turn the memory manager's nested stats into one readable line."""
    tiers = stats.get("tiers") if isinstance(stats.get("tiers"), Mapping) else {}
    bits = [f"{name} {count}" for name, count in tiers.items() if count]
    for key, label in (("knowledge", "knowledge docs"), ("skills", "skills"),
                       ("sessions", "sessions")):
        value = stats.get(key)
        if isinstance(value, Mapping):
            count = value.get("documents") or value.get("count") or value.get("total") \
                or value.get("sessions") or 0
            if count:
                bits.append(f"{label} {count}")
        elif isinstance(value, int) and value:
            bits.append(f"{label} {value}")
    short = stats.get("short_term")
    if isinstance(short, Mapping) and short.get("message_count"):
        bits.append(f"working {short['message_count']} msg")
    return " · ".join(bits) or "empty"


def collect_environment(config: Optional[Mapping[str, Any]] = None, *,
                        registry: Any = None, api_manager: Any = None,
                        include_optional: bool = True) -> EnvironmentInfo:
    """Gather live environment facts (never raises — missing bits are omitted)."""
    from ..config import get as cfg_get
    from ..config import load_config

    config = config or load_config()
    env = EnvironmentInfo(
        os=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python=".".join(str(part) for part in sys.version_info[:3]),
    )

    try:
        from ..utils.platform_probe import probe_platform

        probe = probe_platform()
        env.display = "yes" if getattr(probe, "has_display", False) else "no"
    except Exception:
        env.display = "unknown"

    try:
        from .providers import provider_chain_labels  # type: ignore  # optional helper

        env.fallback_chain = list(provider_chain_labels(config))
    except Exception:
        chain = cfg_get(config, "llm.fallback_chain", []) or []
        env.fallback_chain = [str(item) for item in chain]

    if include_optional:
        try:
            from ..tools.browser.terminal_launcher import find_chrome_binary

            binary = find_chrome_binary()
            env.chrome = binary or "not found (JARVIS will use bundled Chromium)"
        except Exception:
            env.chrome = ""
        try:
            from ..tools.browser.harness import BrowserHarness

            chain = list(cfg_get(config, "browser.engine_chain", []) or [])
            env.browser_chain = [str(item) for item in chain]
            env.browser_engines = [str(item) for item in
                                   (cfg_get(config, "browser.engines", []) or [])]
            if not env.browser_engines:
                env.browser_engines = [str(item) for item in chain]
        except Exception:
            pass
        env.headless = bool(cfg_get(config, "browser.headless", False))

    model = str(cfg_get(config, "llm.model", "") or "")
    provider = str(cfg_get(config, "llm.provider", "") or "")
    env.model, env.provider = model, provider

    if registry is not None:
        try:
            names = list(registry.names())
            env.tools = len(names)
            counts: Dict[str, int] = {}
            for tool_obj in getattr(registry, "get_all_tools", lambda: [])() or []:
                category = getattr(tool_obj, "category", "") or "other"
                counts[category] = counts.get(category, 0) + 1
            env.tool_categories = counts
        except Exception:
            pass

    if include_optional:
        try:
            from ..memory.memory_manager import get_memory_manager

            manager = get_memory_manager(config)
            stats = manager.stats() if hasattr(manager, "stats") else {}
            if isinstance(stats, Mapping):
                env.memory = {str(key): value for key, value in list(stats.items())[:5]}
        except Exception:
            pass
        try:
            from ..tools.obsidian.vault import resolve_vault

            env.vault = str(resolve_vault(config)) if cfg_get(config, "obsidian.enabled",
                                                              True) else ""
        except Exception:
            env.vault = ""
        for command, label in (("espeak-ng", "espeak-ng TTS"),
                               ("say", "macOS say"),
                               ("ffmpeg", "ffmpeg")):
            if shutil.which(command):
                env.tts = f"{env.tts}, {label}".strip(", ") if env.tts else label
        # OCR: use the same discovery logic as the ocr_reader module so a
        # binary that is not on PATH (e.g. Windows Program Files) is still found.
        try:
            from ..tools.screen.ocr_reader import ocr_available
            ocr_ok, ocr_detail = ocr_available()
            if ocr_ok:
                env.notes.append(f"OCR: {ocr_detail} (screen text reading active)")
            else:
                env.notes.append(f"OCR unavailable: {ocr_detail}")
        except Exception:
            if shutil.which("tesseract"):
                env.notes.append("OCR: tesseract on PATH")
        if not cfg_get(config, "browser.headless", False) and env.display == "no":
            env.notes.append("No display: the browser runs headless and screen tools "
                             "cannot move a real cursor here (use Xvfb).")
    return env


# ── the prompt ──────────────────────────────────────────────────────────────
TEMPLATE = """{identity}

## OPERATOR
{operator}

## RUNTIME
{environment}

## HOW YOU RESPOND
- Answer first in one line, then the minimum useful detail. The operator is a busy
  student and builder: no filler, no restating the question, no apologies.
- When you acted, state what you did and the evidence (a title, a URL, a line read
  from the page, a file path) — not a claim you cannot back.
- Show your work only when it changes the answer.
- Keep the operator's voice: {tone}.

## TOOL PROTOCOL
- Tools are called with JSON arguments; the names below are the exact names. Never
  invent a tool or an argument: if a name is not in your bound list, say what you
  would need instead.
- One call at a time when the next step depends on the result; batch only truly
  independent reads.
- Every result carries `ok` plus `content` (or `error`). `ok=false` is information,
  not failure: read the error, fix the argument or change approach.
- Prefer the cheapest path that can work: memory → crawl/read → search → browser →
  screen. Escalate only when the cheaper one cannot do the job.
{category_guide}
- After an action that changes something (a click, a type, a write, a send), verify
  it before reporting: re-read the page, list the folder, check the player state.
{playbook_index}

## MEMORY
- Tiers: working (this conversation), episodic (what happened, when), semantic
  (facts), procedural (how-to), plus the knowledge base and your session store.
- Recalled memory and knowledge-base hits appear in your context automatically —
  treat them as true unless a tool contradicts them.
- Look before asking: `recall` / `search_memory` for anything the operator may have
  told you before. Ask only after looking.
- Store when: the operator says “remember”, states a preference, a deadline, a
  decision, a name, or a fact that will still matter next week (`remember`). Media
  plays are stored automatically by `media_play` — use `media_history` for them.
- Never store secrets (tokens, passwords, OTPs). Never claim to remember something
  that is not in memory: say you do not have it.

## PLAYBOOKS
{playbooks}

## HARD RULES
{rules}
"""


def _identity(profile: OperatorProfile) -> str:
    return (f"You are JARVIS, the personal agent of {profile.name} (“{profile.handle}”), "
            f"built by and for him at {profile.company}. You run locally on a CPU-only "
            f"machine on free models and free APIs. You are loyal, precise, quietly "
            f"witty, and you finish what you start.")


def render_playbooks(playbooks: Optional[Mapping[str, Sequence[str]]] = None) -> str:
    blocks: List[str] = []
    for name, steps in (playbooks or PLAYBOOKS).items():
        blocks.append(f"**{name.upper()}**")
        blocks.extend(f"  {index}. {step}" for index, step in enumerate(steps, 1))
    return "\n".join(blocks)


MINIMAL_TEMPLATE = """{identity}

Operator: {operator} · {education} · {timezone}

TOOLS: call by exact name with JSON arguments; results carry ok/content or
error. ok=false is information — read it, fix the argument, try another way, and
never invent a result. Act → verify → answer. {tool_line}

MEMORY: recalled memory and knowledge hits below are true unless a tool says
otherwise. Look (recall / search_memory) before asking the operator to repeat
anything; store stated preferences, deadlines and decisions with remember.

RULES: {rules}

WORKFLOWS:
{workflows}
"""


def _minimal_prompt(prof: OperatorProfile, tool_line: str, context: str = "",
                    workflows: bool = True) -> str:
    essentials = ["media", "browser", "memory"]
    workflow_text = "\n".join(f"- {name}: {PLAYBOOK_INDEX[name]}"
                               for name in essentials if name in PLAYBOOK_INDEX)
    workflows = workflow_text if workflows else "media: media_play(query=…) → verify → remembered"
    return MINIMAL_TEMPLATE.format(
        identity=_identity(prof),
        operator=f"{prof.name} (“{prof.handle}”)",
        education=prof.education,
        timezone=prof.timezone,
        tool_line=tool_line.strip(),
        workflows=workflows,
        rules=" ".join(HARD_RULES[:4]),
    ) + (f"\n{context}" if context else "")


def _compact_operator(profile: OperatorProfile) -> List[str]:
    """The operator facts that matter most, when the budget is tight."""
    keep = ("name", "role", "timezone", "education", "languages", "devices")
    rows = [f"- {fact['key']}: {fact['value']}" for fact in profile.facts()
            if fact["key"] in keep]
    rows.append(f"- preferences: {profile.preferences.get('tone', '')}; "
                f"address him as {profile.preferences.get('address_as', profile.handle)}; "
                f"{profile.preferences.get('units', '')}; never suggest paid services")
    return rows


def _compact_environment(env: EnvironmentInfo) -> List[str]:
    rows = [f"- Machine: {env.os} · display {env.display}"]
    if env.browser_engines or env.browser_chain:
        rows.append(f"- Browser: engines {', '.join(env.browser_engines) or '—'} · "
                    f"chain {' → '.join(env.browser_chain) or '—'}")
    if env.tools:
        rows.append(f"- Tools: {env.tools} registered")
    rows.append("- Model: " + (env.provider or "auto")
                + (f" / {env.model}" if env.model else "")
                + (" · fallback " + " → ".join(env.fallback_chain)
                   if env.fallback_chain else ""))
    if env.vault:
        rows.append(f"- Study vault: {env.vault}")
    return rows


def build_universal_prompt(*, profile: Optional[OperatorProfile] = None,
                           environment: Optional[EnvironmentInfo] = None,
                           config: Optional[Mapping[str, Any]] = None,
                           tools: Optional[Iterable[Any]] = None,
                           memories: str = "", knowledge: str = "",
                           session: str = "", extra: str = "",
                           max_chars: int = 12000) -> str:
    """Assemble the full system prompt within ``max_chars``.

    ``tools`` may be the tools bound for this turn (objects or schemas); their
    names are listed so the model only ever sees callable names.  When the budget
    is tight the optional parts are dropped in a deliberate order — context
    blocks, then operator/environment detail — and the tool contract, memory
    rules and hard rules are the last things to go.
    """
    prof = profile or OPERATOR
    env = environment if environment is not None else collect_environment(config)

    names: List[str] = []
    descriptions: Dict[str, str] = {}
    for item in tools or []:
        name = getattr(item, "name", None)
        if name is None and isinstance(item, Mapping):
            function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
            name = function.get("name") or item.get("name")
            descriptions[str(name or "")] = str(function.get("description") or "")
        if name:
            names.append(str(name))
            if str(name) not in descriptions:
                descriptions[str(name)] = str(getattr(item, "description", "") or "")

    def tool_line() -> str:
        unique = sorted(set(names))
        if not unique:
            return ""
        if len(unique) > 40:
            return (f"\n- Bound this turn: all {len(unique)} registered tools "
                    "(their schemas are attached); call them by exact name.")
        guide = guide_lines(unique, descriptions)
        guide_block = ("\n  When to use each bound tool:\n  " + "\n  ".join(guide)
                       if guide else "")
        return ("\n- Bound this turn: " + ", ".join(unique)
                + "\n  (only these exist right now; ask for another by describing "
                  "the need)" + guide_block)

    category_lines: List[str] = []
    for category, count in sorted(env.tool_categories.items(), key=lambda item: -item[1]):
        guide = CATEGORY_GUIDE.get(category, "")
        if guide:
            category_lines.append(f"- {category} ({count}): {guide}")
    if not category_lines:
        category_lines = [f"- {name}: {CATEGORY_GUIDE.get(name, '')}"
                          for name in CATEGORY_GUIDE]

    memories_section = ("## RECALLED MEMORY\n" + memories.strip()) if memories.strip() else ""
    knowledge_section = ("## KNOWLEDGE BASE\n" + knowledge.strip()) if knowledge.strip() else ""
    session_section = ("## SESSION CONTEXT\n" + session.strip()) if session.strip() else ""
    context_sections = [knowledge_section, memories_section, session_section]
    extra_section = extra.strip()

    def render(*, operator: Sequence[str], environment: Sequence[str],
               playbooks: str, categories: Sequence[str],
               index: str = "") -> str:
        text = TEMPLATE.format(
            identity=_identity(prof),
            operator="\n".join(operator),
            environment="\n".join(environment) or "- (environment unknown)",
            tone=prof.preferences.get("tone", "clear and concise"),
            category_guide="\n".join(categories) + tool_line(),
            playbook_index=("\n- The workflows in one line each:\n" + index) if index else "",
            playbooks=playbooks,
            rules="\n".join(f"{index}. {rule}"
                             for index, rule in enumerate(HARD_RULES, 1)),
        )
        for block in (context_sections[0], context_sections[1], context_sections[2],
                      extra_section):
            if block:
                text = f"{text}\n\n{block}"
        return text

    full_operator = [f"- {fact['key']}: {fact['value']}" for fact in prof.facts()]
    text = render(operator=full_operator, environment=env.lines(),
                  playbooks=render_playbooks(),
                  categories=category_lines)
    if len(text) <= max_chars:
        return text

    # 1) drop the cheapest context first
    for section in context_sections:
        if section:
            text = text.replace(section + "\n\n", "").replace(section, "")
            if len(text) <= max_chars:
                log.debug(f"system prompt trimmed to {len(text)} chars (context dropped)")
                return text

    # 2) compress operator + environment detail, keep the playbooks
    text = render(operator=_compact_operator(prof), environment=_compact_environment(env),
                  playbooks=render_playbooks(),
                  categories=category_lines)
    if len(text) <= max_chars:
        log.debug(f"system prompt trimmed to {len(text)} chars (compact facts)")
        return text

    # 3) shorten the playbooks to their first step each
    text = render(operator=_compact_operator(prof), environment=_compact_environment(env),
                  playbooks=render_playbooks(),
                  categories=[line.split(":")[0] for line in category_lines],
                  index=render_playbook_index())
    if len(text) <= max_chars:
        log.debug(f"system prompt trimmed to {len(text)} chars (brief playbooks)")
        return text

    # 4) drop the playbooks and the category list entirely — the contract stays
    text = render(operator=_compact_operator(prof), environment=_compact_environment(env),
                  playbooks="", categories=[], index=render_playbook_index())
    if len(text) <= max_chars:
        log.debug(f"system prompt trimmed to {len(text)} chars (contract only)")
        return text

    # 5) last resort: the minimal contract — identity, tool rules, memory rules,
    #    hard rules and whatever context still fits. Never a rules-free prompt.
    context = "\n".join(block for block in context_sections if block)
    text = _minimal_prompt(prof, tool_line(), context)
    if len(text) > max_chars:                       # shrink the context first
        text = _minimal_prompt(prof, tool_line())
    if len(text) > max_chars:                       # then the workflow detail
        text = _minimal_prompt(prof, tool_line(), workflows=False)
    if len(text) > max_chars:                       # then the rules, saying so
        text = text[:max(0, max_chars - 70)] + "\n[...trimmed to fit the budget...]"
    log.debug(f"system prompt reduced to the minimal contract ({len(text)} chars)")
    return text


def prompt_for_turn(brain: Any, *, memory: str = "", knowledge: str = "",
                    session: str = "", extra: str = "") -> str:
    """Use the *live* brain state (bound tools, environment, config) to build a prompt."""
    config = getattr(brain, "config", None)
    registry = getattr(brain, "registry", None)
    env = collect_environment(config, registry=registry)
    env.model = getattr(brain, "model", "") or env.model
    if getattr(brain, "provider_name", ""):
        env.provider = str(brain.provider_name)
    return build_universal_prompt(profile=OPERATOR, environment=env, config=config,
                                  tools=getattr(brain, "bound_tools", None),
                                  memories=memory or getattr(brain, "_memory_block", ""),
                                  knowledge=knowledge or getattr(brain, "_knowledge_block", ""),
                                  session=session, extra=extra,
                                  max_chars=int(getattr(brain, "prompt_budget", 9000)))
