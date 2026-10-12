# JARVIS

[![CI](https://github.com/heostertech-cyber/jarvis/actions/workflows/ci.yml/badge.svg)](https://github.com/heostertech-cyber/jarvis/actions/workflows/ci.yml)
[![CD · release](https://github.com/heostertech-cyber/jarvis/actions/workflows/cd-release.yml/badge.svg)](https://github.com/heostertech-cyber/jarvis/actions/workflows/cd-release.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://github.com/heostertech-cyber/jarvis)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](pyproject.toml)

**Just A Rather Very Intelligent System** — a free-tier, single-user AI operator built for **Heoster** (Harsh Saini · CODEEX-AI).

JARVIS is a *harness*, not a chatbot wrapper. It holds a persistent session, keeps a smart memory of you, drives a real Chrome window (by DOM **and** by moving the actual cursor), controls the desktop through OCR rather than a vision model, chains six LLM providers with automatic verified failover, and exposes everything through a CLI and an OpenAI-compatible local API.

```
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
        JARVIS v1.0.0 · built for Heoster (CODEEX-AI) · free-tier only
```

---

## Table of contents

- [What it is](#what-it-is)
- [The three focus areas](#the-three-focus-areas)
  - [1 · Screen control (no vision model)](#1--screen-control-no-vision-model)
  - [2 · Browser control (real Chrome, real cursor)](#2--browser-control-real-chrome-real-cursor)
  - [3 · Smart memory](#3--smart-memory)
- [API manager: verified automatic fallback](#api-manager-verified-automatic-fallback)
- [Install](#install)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [Local API server](#local-api-server)
- [Obsidian & student mode](#obsidian--student-mode)
- [The system prompt (what the model is actually told)](#the-system-prompt-what-the-model-is-actually-told)
- [Play a song (URL injection, verification, memory)](#play-a-song-url-injection-verification-memory)
- [Tools (123)](#tools-123)
- [Agents](#agents)
- [Architecture](#architecture)
- [Free-tier limits](#free-tier-limits)
- [Configuration](#configuration)
- [Troubleshooting / doctor](#troubleshooting--doctor)
- [Tests](#tests)
- [CI / CD on GitHub](#ci--cd-on-github)
- [Project layout](#project-layout)

---

## What it is

| Property | Value |
| --- | --- |
| Operator | **Heoster** — Harsh Saini, founder of CODEEX-AI, Khatauli (Muzaffarnagar), India |
| Cost policy | **Free tiers only.** No paid API, no metered service, no credit card |
| Vision model | **None, by design.** Screen understanding is OCR + DOM, never a VLM |
| LLM chain | ollama → groq → cerebras → openrouter → gemini → hf → stub (auto-failover, verified) |
| Browser chain | playwright → selenium → cdp (terminal-launched Chrome) → screen control |
| Tools | **123**, in 10 categories, all schema-validated |
| Memory | short-term window + episodic / semantic / procedural long-term tiers, hybrid recall |
| Study | Obsidian vault tools with spaced repetition **and** a JARVIS plugin for Obsidian |
| Interfaces | CLI (`python -m jarvis`), interactive chat, OpenAI-compatible HTTP server |

Everything works **offline-first**: with no API keys and no local model, JARVIS still browses, clicks, types, reads the screen, searches its memory, manages files, and answers arithmetic — it just says so honestly instead of hallucinating.

---

## The three focus areas

### 1 · Screen control (no vision model)

JARVIS never sends a screenshot to a model. Screen understanding is a two-step, fully local pipeline:

```
screenshot  ──►  tesseract OCR (local binary)  ──►  words + confidence + boxes
                                                        │
                            fuzzy match ≥ 0.8 ──────────┘
                                     │
                          scale  ──►  real cursor click
```

- **Input backends** (auto-detected, first that works wins):
  `pyautogui` → `xdotool` → `ydotool` → PowerShell (Windows) → AppleScript (macOS) → `NullBackend` (reports *why* instead of pretending).
- **Real cursor movement**: Bezier-curved paths with eased timing (`min(120, distance × 0.18)` bow, `t²(3−2t)` easing, ±0.8 px jitter, ~16 ms steps) — moves look human, not teleported.
- **Click by text**: `screen_click_text("Sign in")` → OCR → match → scaled coordinates → cursor click, and it reports the exact pixel it used.
- **Windows**: list, focus, maximise/minimise/close via `wmctrl`/`xdotool`/PowerShell/osascript.
- **Capability honesty**: on a headless box every screen tool returns `ok=false` with the exact install command (`Xvfb`, `xdotool`, `tesseract-ocr`) — it never silently fails.

Key tools: `take_desktop_screenshot`, `read_screen_text`, `find_on_screen`, `screen_click_text`, `screen_fill_field`, `cursor_move`, `cursor_click`, `keyboard_type`, `keyboard_press`, `screen_scroll`, `screen_list_windows`, `screen_focus_window`, `launch_application`, `run_in_terminal_window`.

### 2 · Browser control (real Chrome, real cursor)

The user's requirement was explicit: **launch Chrome from the terminal, then drive it by screen control with cursor movement, typing and browser commands — in real time, harness-style.** JARVIS implements that literally, with a hybrid that uses the DOM for *targeting* and the OS cursor for *acting*.

```
BrowserHarness
 ├─ playwright   full DOM + JS + headless or headed
 ├─ selenium     full DOM, classic WebDriver
 ├─ cdp          talks to a Chrome YOU launched from the terminal (port 9222)
 └─ screen       OCR + cursor only (works on any window, even one you opened)
```

- **Terminal launch first**: `scripts/chrome-harness.sh` starts Chrome with `--remote-debugging-port=9222`, a real window, and your profile. JARVIS's `browser_launch` tool does the same from Python and remembers the process.
- **Hybrid clicking**: `browse_open` renders the page as a numbered menu — `[7] <a> "Lofi Girl"` — the model picks an index, JARVIS resolves it to a CSS selector, asks the engine for the element's viewport box, converts that box to **screen coordinates** (window position + toolbar height + `devicePixelRatio`), and then moves the real cursor and clicks. If the box is unavailable it falls back to a DOM click, and if no engine is alive it falls back to OCR + coordinate search.
- **Verification built in**: `verify(deep=True)` starts each engine in turn and reports *exactly* what is missing (`pip install playwright && playwright install chromium`), so "the browser is broken" is never a mystery.
- **Detection resistance**: because the click physically happens at OS level on a real, visible window, sites that fingerprint synthetic events see a human.

Key tools: `browse_open`, `page_read`, `page_click`, `page_type`, `page_press`, `page_scroll`, `page_back`, `page_forward`, `page_tabs`, `page_screenshot`, `page_js`, `page_find`, `browser_launch`, `browser_engines`, `browser_status`, `screenshot_page`, `screenshot_and_read`.

### 3 · Smart memory

A fixed sliding window is the *floor*, not the design. JARVIS keeps four tiers and decides, per turn, what deserves to persist:

| Tier | Holds | Lifetime |
| --- | --- | --- |
| **Working** | the live conversation (24 messages) + rolling summary of evicted turns | session |
| **Episodic** | *what happened* — turns, tool runs, agent results | decayed, promoted when reused |
| **Semantic** | *what is true* — facts about Heoster, preferences, project state | long, deduped |
| **Procedural** | *how to do it* — learned tool sequences (skills) | strongest, reused |

- **Importance gate** — every candidate is scored (explicit "remember" 0.9, corrections 0.85, preferences 0.75, tool failures 0.6, chatter 0.05). Below the threshold (`memory.importance_threshold`, default 0.35) it is **dropped**, so the store does not fill with "hmm" and "ok".
- **Near-duplicate merge** — a new fact within cosine 0.90 of an existing one bumps the old record instead of adding a twin.
- **Decay & promotion** — unused low-importance records fade (45-day half-life) and are pruned; records that get recalled are promoted (episodic → semantic) and their importance rises.
- **Hybrid recall** — 0.65 × vector similarity (hashing TF-IDF locally, `nomic-embed-text` when ollama is up) + 0.35 × BM25 keyword match, with a token budget so recall can never blow the context window.
- **Consolidation** — every 25 turns (and on `/memory consolidate`) JARVIS promotes, decays, prunes and summarizes.
- **Skills** — successful browser/screen traces are stored as reusable procedures and offered back as hints (`skills.hints_for("open youtube and play lofi")`).
- **Heoster profile** — seeded from `jarvis/persona.py`, extended automatically: preferences, corrections, personal details and tool preferences are extracted from what you say, and injected into every system prompt.

Storage is a single SQLite file (`memory_store/vectors.sqlite3`, WAL mode, float32 vectors + an inverted index for BM25). No server, no Docker, no RAM footprint. ChromaDB is optional if you want it.

---

## API manager: verified automatic fallback

`jarvis/core/api_manager.py` is the reason JARVIS keeps answering when a free tier dies.

- **Chain**: `ollama → groq → cerebras → openrouter → gemini → hf → stub`, reorderable, always ending in a provider that cannot fail.
- **Health checks**: each provider has a cheap `probe()` (model list / ping). Results are cached for 90 s with latency recorded.
- **Backoff**: a provider that errors is marked `degraded`/`down` with exponential backoff (2 s → 120 s) instead of being hammered.
- **Quota integration**: every call passes the free-tier quota manager (rpm / rpd / monthly credits per provider) *before* the request, so JARVIS never burns a free tier it needs later.
- **Verified fallback** — this is the part that is easy to get wrong and JARVIS gets right:
  1. the turn's failed provider is recorded,
  2. the next provider is *actually called* (not assumed healthy),
  3. the answer records `provider`, `fallback_from` and `degraded: true`,
  4. a background **auto-heal** thread re-probes failed providers every 120 s and returns them to the chain once they recover,
  5. `jarvis api --verify` forces a live probe of every provider and prints latency + status, and `recent_fallbacks()` exposes the audit trail.
- **Honest last resort**: the `stub` provider never pretends to reason. It performs the *obvious* tool call (arithmetic, time, URL, search), returns raw results, and tells Heoster how to get a real model — including the exact `ollama pull` command.

```
$ python -m jarvis api --verify
  provider     status        latency   note
  ollama       down          -         connection refused (start `ollama serve`)
  groq         unconfigured  -         no API key (set GROQ_API_KEY)
  ...
  stub         healthy       0 ms      unlimited
  fallback chain : ollama → groq → cerebras → openrouter → gemini → hf → stub
  would answer   : stub
```

---

## Install

Requires **Python 3.10+** and ~200 MB of disk. Works on Windows, Linux and macOS; a 2-core / 4 GB machine is enough.

### One command

```bash
git clone https://github.com/Heoster/jarvis.git
cd jarvis
python jarvis-setup.py            # that's it — same command on every platform
```

The launcher picks the native installer for your machine (`scripts/jarvis-setup.ps1` on Windows, `scripts/jarvis-setup.sh` on Linux and macOS) and passes your flags through. Either installer also runs on its own:

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\jarvis-setup.ps1
```

```bash
# Linux / macOS
bash scripts/jarvis-setup.sh
```

Ten steps, in order: interpreter check → virtualenv → core install (`pip install -e .`, so `jarvis` lands on PATH) → focus-area extras (screen, browser, reading) → Playwright's Chromium → OS-level tools (Tesseract OCR, and on Linux `xdotool` / `wmctrl` / `scrot`) → optional local model → `.env` → **verification** → `jarvis doctor`.

The verification step is what makes it "installed" rather than "hopeful": it runs the test suite, the same `ruff` gate CI enforces, and a wheel build, so a broken install tells you so immediately.

Nothing is fatal except the core install. Anything optional that fails becomes a warning, and the run ends with a list of what to look at.

| Flag | Effect |
| --- | --- |
| `--python PATH` | interpreter to build the venv from |
| `--skip-browser` | skip the Chromium download (~150 MB) |
| `--skip-tests` | skip the verification stage |
| `--skip-system` | never touch the OS package manager; report only |
| `--model` | also `ollama pull hermes3:3b` (~2 GB) |
| `--web` | also the FastAPI + Gradio web UI extras |
| `--no-dev` | skip pytest / ruff / build |
| `--yes` | don't prompt before installing OS packages |

On Windows the equivalents are `-Python`, `-SkipBrowser`, `-SkipTests`, `-SkipSystem`, `-Model`, `-Web`, `-NoDev`, `-Yes`.

### Or manually

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
pip install playwright selenium websockets mss Pillow pytesseract trafilatura beautifulsoup4
playwright install chromium
```

Then check the machine and doctor the install:

```bash
python -m jarvis doctor          # hardware, screen, browser, memory, API, crawl
```

Nothing above is mandatory beyond the core four (`pyyaml`, `httpx`, `rich`, `click`). Every optional piece degrades to a clear message instead of a crash.

Headless server? Give it a virtual display:

```bash
bash scripts/start-xvfb.sh          # Xvfb :99 + xdotool + tesseract hints
```

---

## Quick start

```bash
# interactive chat (resumes your last session, remembers everything)
python -m jarvis

# one-shot question
python -m jarvis ask "What is 21 * 2 + 8?"

# launch Chrome from the terminal, then drive it by DOM + cursor
bash scripts/chrome-harness.sh              # port 9222, real window, your profile
python -m jarvis ask "open youtube, search for lofi beats and click the first video"

# screen control with no vision model
python -m jarvis screen --read              # OCR the current screen
python -m jarvis ask "click the button that says Sign in"

# what do you know about me?
python -m jarvis profile

# Obsidian: study kit, daily note, revision queue, exam plan
export OBSIDIAN_VAULT="$HOME/Obsidian"
python -m jarvis obsidian kit --subjects "Physics,Chemistry,Maths"
python -m jarvis obsidian cards due          # spaced-repetition queue
python -m jarvis obsidian plan --exam 2027-03-01
python -m jarvis obsidian plugins --install  # the JARVIS Obsidian plugin

# local OpenAI-compatible API
python -m jarvis serve --port 8077
```

Inside `jarvis chat` you also get slash commands:

| Command | Effect |
| --- | --- |
| `/help` | full slash-command list |
| `/status` `/health` | brain, memory, tools, quota |
| `/tools [category]` | what JARVIS can do right now |
| `/memory [stats\|consolidate\|search q]` | inspect or maintain memory |
| `/profile` | everything learned about Heoster |
| `/skills` | learned procedures |
| `/agents` `/agent <name> <task>` | list / delegate to specialists |
| `/model` `/api` `/quota` | provider chain, health, free-tier usage |
| `/browser` `/screen` | focus-area status |
| `/compress` | summarise the context now |
| `/report` `/time` `/clear` `/exit` | misc |

---

## CLI reference

```
python -m jarvis [--version] COMMAND [ARGS]

chat        Interactive conversation (default)
ask         Ask one question and exit
doctor      Machine + capability report, with exact fix commands
tools       List tools, optionally filtered by category
api         Provider chain, health, latency, free-tier quota (--verify for live probes)
browser     Chrome harness: engines, terminal launch, fallback verification
screen      Screen control: capabilities, screenshots, OCR, click-by-text
memory      Inspect the smart memory manager (stats/consolidate/search)
obsidian    Vault notes, flashcards, revision queue, exams, study plans, plugins
coach       Study coach: daily brief, weak-topic analytics, weekly reviews
quota       Free-tier usage per provider (--reset PROVIDER)
serve       Run the local API server (OpenAI-compatible)
search      Web search through the provider fallback chain
read        Read a URL through the crawl fallback chain
agents      List specialist sub-agents
delegate    Hand a task to one specialist
profile     Show what JARVIS knows about Heoster
```

---

## Local API server

OpenAI-compatible, stdlib-only by default (FastAPI + uvicorn if installed):

```bash
python -m jarvis serve --host 127.0.0.1 --port 8077
# or: uvicorn-style via JARVIS_API_HOST / JARVIS_API_PORT / JARVIS_API_TOKEN
```

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | liveness |
| `GET /status` | brain, memory, tools, quota |
| `GET /tools` | all 123 tools with categories |
| `POST /ask` | `{"message": "..."}` → answer + provider + memory trace |
| `POST /tool` | `{"name": "calculate", "args": {...}}` → raw tool result |
| `POST /read` | `{"url": "..."}` → crawl chain shortcut |
| `POST /search` | `{"query": "..."}` → search chain shortcut |
| `POST /v1/chat/completions` | drop-in OpenAI shape, adds a `jarvis` metadata block |

```bash
curl -s localhost:8077/ask -H 'content-type: application/json' \
     -d '{"message":"What is 21 * 2 + 8?"}'
```

Set `JARVIS_API_TOKEN` to require `Authorization: Bearer <token>`. The server binds to `127.0.0.1` by default — bind `0.0.0.0` only on a trusted network.

---

## Obsidian & student mode

JARVIS treats an **Obsidian vault as first-class memory for studying**: a vault is
just a folder of markdown files, so every tool works with Obsidian closed, offline,
and without any plugin. Point JARVIS at your vault and it can read, write, search,
plan and drill it.

```bash
export OBSIDIAN_VAULT="$HOME/Obsidian"        # or obsidian.vault_path in config.yaml
python -m jarvis obsidian kit                 # scaffold the student vault
python -m jarvis obsidian status              # vault, due cards, exams, streak
```

### The study loop

| Step | Command | What happens |
| --- | --- | --- |
| **Capture** | `obsidian_write_note`, `obsidian_daily_note` | lecture notes, daily log, doubts |
| **Distil** | `obsidian_flashcards_add(from_note="Newton's Laws")` | definitions → `Term :: meaning`, key terms → cloze |
| **Revise** | `obsidian_revision_queue` | due cards + overdue tasks + `#revision` notes |
| **Grade** | `obsidian_flashcards_review(grade="good")` | SM-2 reschedules (again/hard/good/easy), streak tracked |
| **Plan** | `obsidian_study_plan(exam_date="2027-03-01")` | chapters spread over the days left, targets written into each daily note |
| **Track** | `obsidian_exams`, `obsidian_study_report` | days left, per-subject progress, open tasks |
| **Coach** | `jarvis coach watch --at 19:30` | pushes the brief to you instead of waiting to be asked |
| **Diagnose** | `obsidian_weak_topics`, `jarvis coach weak` | which topics you actually keep forgetting |

### The study coach — it asks you, not the other way round

Every review is logged, so JARVIS can measure what is actually sticking:

```bash
python -m jarvis coach                 # next brief time, retention, weakest topics
python -m jarvis coach weak --limit 5  # "Physics · Optics — 4 lapses in 4 reviews (0%)"
python -m jarvis coach brief           # what to do right now (markdown / --json)
python -m jarvis coach brief --write   # …and drop it into today's daily note
python -m jarvis coach weekly          # retention table + next week's focus note
python -m jarvis coach watch --at 19:30            # sit in the background and push it
python -m jarvis coach watch --at 07:00,19:30 --speak   # …and read the headline aloud
```

The brief is written under `### JARVIS brief` in the daily note — refreshed in
place, never duplicated — and lands next to the planner's *Target for today*
block. It contains: cards due (⚠ marks ones you have lapsed on), overdue tasks,
the weakest subject/topic with its retention %, exam countdowns, streaks, and a
one-line headline (`5 card(s) due — weakest: Physics / Optics (0% retention)`).
`watch` sleeps until each scheduled time (notify desktop · speak aloud · write a
weekly review on Sundays) and keeps a running log.

**Weakness** is not vibes: it combines lapses, low ease, how overdue a card is,
cards stuck in `learning`, and topics that have gone quiet for
`obsidian.coach.stale_days`. All of it comes from files already in the vault.

### Card formats (Obsidian-Spaced-Repetition compatible)

```markdown
- What is the SI unit of pressure? :: pascal (Pa)
- Photosynthesis happens in the {{c1::chloroplast}}.

#flashcard The three laws of motion are stated by?
> Newton
```

Code fences are ignored, duplicates are merged, and the schedule lives in a single
JSON file (`data/obsidian_srs.json`) — reviews are never lost, and the community
*Spaced Repetition* plugin understands the same syntax if you prefer its UI.

### What the study kit builds

`00 Inbox` · `01 Subjects/<subject>` with index notes · `02 Daily Notes` ·
`03 Flashcards` · `04 Exams` · `05 Revision` · `99 Templates` (lecture note,
formula sheet, exam plan, daily note, weekly review) · `JARVIS Home.md` dashboard.
Nothing existing is overwritten, and re-running it creates nothing twice.

### Prebuilt community plugins, installed for you

The free plugins a student actually wants — **Dataview, Spaced Repetition, Tasks,
Calendar, Templater, Git, Excalidraw, Kanban, Admonition, Syntax Highlight** —
can be dropped straight into the vault. JARVIS fetches the same release files
Obsidian's community store would install (`main.js`, `manifest.json`,
`styles.css`), so there is **no npm, no bundler and no build step**:

```bash
python -m jarvis obsidian plugins --list-prebuilt            # the catalogue + state
python -m jarvis obsidian plugins --install-prebuilt all     # download + enable
python -m jarvis obsidian plugins --install-prebuilt "dataview, tasks, spaced"
python -m jarvis obsidian plugins --update                   # newest releases
python -m jarvis obsidian plugins --list-prebuilt --checked  # what has an update
python -m jarvis obsidian plugins --install-prebuilt all --offline   # no network
```

Installs are not a black box: when a release publishes a sha256 digest it must
match before anything is written, and every plugin folder gets a
`.jarvis-provenance.json` (repo, tag, asset URLs, per-file hashes, timestamp). A
download that fails falls back to a vendored bundle when one exists.

**Offline installs.** Run `python scripts/fetch_prebuilt_plugins.py` once on a
machine with internet and every bundle is vendored under
`obsidian-plugin/prebuilt/<plugin-id>/` with `SHA256SUMS` and `ATTRIBUTION.md`;
after that `--offline` works with no network at all (each project keeps its own
licence — see the attribution file). The chat model can do the same thing by
calling the `obsidian_plugins` tool with `action="install-prebuilt"`.

### The JARVIS plugin for Obsidian

`obsidian-plugin/jarvis/` is a real community plugin — plain CommonJS, no bundler,
no npm, no build step — that talks to your local JARVIS server over `requestUrl`:

```bash
python -m jarvis obsidian plugins --install    # copies + enables it in the vault
```

Commands (Ctrl/Cmd+P): **Ask JARVIS** · **Explain the current note** ·
**Summarize the current note** · **Explain the selected text** ·
**Make flashcards from the current note** · **Show today's revision queue** ·
**Research this topic** · **Save selection to memory** · **Test the connection**.
The ribbon robot opens a chat panel where every answer can be inserted into the
note or turned into cards. A status-bar dot shows whether the server is reachable.

Nothing is sent anywhere except `127.0.0.1` (plus whichever LLM provider you
configured) — the plugin has no account, no telemetry, no cloud.

## The system prompt (what the model is actually told)

Every turn, `Brain.system_prompt()` builds one universal prompt from live facts
(`jarvis/core/system_prompt.py`). Sections, in priority order:

1. **identity + operator** — who JARVIS is, who Heoster is, language/timezone.
2. **RUNTIME** — OS, Python, provider chain, bound model, tool counts, search/browser chains.
3. **HOW YOU RESPOND** — short, direct, tools for facts, no invented results.
4. **TOOL PROTOCOL** — call by exact name with JSON args, `ok=false` is information,
   one call at a time when the next step depends on the previous result, act → verify → answer.
5. **MEMORY** — recalled memory/knowledge below is true; look before asking; store
   preferences, deadlines and decisions.
6. **WORKFLOWS** / **PLAYBOOKS** — the exact recipes, e.g. how to play a song or drive a browser.
7. **HARD RULES** — never invent a tool result/file/URL/number; confirm before anything
   destructive or public; free tiers only.

Context blocks (RECALLED MEMORY · KNOWLEDGE BASE · SESSION CONTEXT) are appended when
they fit. The whole thing is budgeted: as the ceiling drops it drops knowledge, then
memories, then session context, then workflow detail — and always still contains the
tool protocol, memory rules, hard rules and a one-line workflow index. See it verbatim:

```bash
python -m jarvis prompt                                  # the real prompt, live facts
python -m jarvis prompt --budget 3000 --memory "favourite song: Kesariya"
python -m jarvis prompt --out prompt.txt                 # save it
```

`brain.system_prompt: universal|persona` in `config.yaml` switches it (the older
hand-written persona prompt is still available); `brain.system_prompt_budget` sets the
character ceiling (default 12000).

## Play a song (URL injection, verification, memory)

```bash
python -m jarvis tool media_play '{"query":"kesariya arijit singh"}'
python -m jarvis tool media_play '{}'                    # replay the last song
python -m jarvis tool media_play '{}' --json             # with query "my favourite song"
python -m jarvis tool media_history '{"day":"yesterday"}'
python -m jarvis tool media_favourite '{"song":"Kesariya – Arijit Singh"}'
python -m jarvis tool page_tabs '{}'                     # or page_tab_switch / page_tab_close
```

The flow, exactly: launch Chrome → **new tab** → inject the YouTube search URL (no
typing into the site) → score the results by keyword overlap (official/lyrics up,
reactions/remixes/short clips down) → open the best match in its own tab → **verify**
whether it is playing, paused (autoplay blocked) or ended → press `k`, then
`video.play()` if still paused → confirm again → record the play with the date,
channel and query in `data/media_history.json` (and mirror it to long-term memory).


## Tools (123)

| Category | Count | Highlights |
| --- | --- | --- |
| browser | 17 | `browse_open`, `page_click/type/press/scroll/tabs/js/find`, `browser_launch`, `browser_status`, `page_screenshot` |
| screen | 19 | `take_desktop_screenshot`, `read_screen_text`, `screen_click_text`, `screen_fill_field`, `cursor_move/click`, `keyboard_type/press`, windows, `launch_application` |
| utility | 16 | `calculate`, `percentage`, `get_current_datetime`, `convert_timezone`, `json_parse`, `clipboard_get/set`, `image_info/resize/convert`, `list_available_tools`, `tool_help` + 5 memory tools |
| file | 11 | `read_file`, `write_file`, `append_file`, `delete_path` (→ trash), `move_path`, `copy_path`, `file_info`, `list_directory`, `file_search`, `find_files_by_name`, `directory_size` |
| code | 8 | `run_python`, `analyze_python`, `run_bash` (destructive-command guard), `code_outline`, `code_find_symbol`, `code_project_summary`, `which_command`, `system_info` |
| communication | 8 | `draft_email`, `send_email`, `email_status`, `http_request`, `send_webhook`, `download_file`, `send_notification`, `speak` |
| crawl | 8 | `read_url` (chain), `read_many`, `crawl_tools_report`, `jina_read_url`, `local_read_url`, `firecrawl_scrape/crawl/map` |
| search | 8 | `search_web` (chain), `parallel_search`, `search_news`, `search_providers`, `tavily_search`, `tavily_answer`, `serper_search`, `searxng_search` |
| memory | 5 | `remember`, `recall`, `search_memory`, `forget`, `memory_stats` |
| obsidian | 18 | `obsidian_search/read/write`, `obsidian_daily_note`, `obsidian_tasks`, `obsidian_flashcards_add/due/review`, `obsidian_revision_queue`, `obsidian_study_brief`, `obsidian_weak_topics`, `obsidian_study_kit`, `obsidian_study_plan`, `obsidian_exams`, `obsidian_plugins` |

Every tool has a validated JSON schema (`python -m jarvis tools spec calculate` shows it), a timeout, and returns `ToolResult(ok, content, data, error, elapsed_ms, artifacts)`. Blocked and destructive operations return `ok=false` with the reason — never a silent success.

---

## Agents

Seven specialists share the same brain, memory and registry, but only see the tools they need:

| Agent | Iterations | Calls | Toolbox |
| --- | --- | --- | --- |
| `browser` | 8 | 14 | browser + crawl |
| `research` | 7 | 12 | search + crawl + memory |
| `code` | 8 | 16 | code + file |
| `file` | 6 | 12 | file |
| `screen` | 8 | 16 | screen + utility |
| `memory` | 5 | 10 | memory + utility |
| `study` | 6 | 14 | obsidian + memory + search/crawl |

```bash
python -m jarvis agents
python -m jarvis delegate browser "open codeex-ai.vercel.app and read the hero section"
python -m jarvis ask "research the best free LLM APIs and save the findings"   # auto-routed
```

Routing is keyword-scored with structural signals (a URL + "open" → browser; "write/fix/run" + code → code; "my preference" → memory); below the confidence threshold the main orchestrator handles the turn itself.

---

## Architecture

```
                       ┌──────────────────────────┐
   CLI / API ─────────► │  Orchestrator            │  guards: 12 iterations,
                       │  perceive→plan→act→learn │  24 calls, 300 s/turn
                       └────┬───────────┬─────────┘
                            │           │
              ┌─────────────▼──┐   ┌────▼─────────────┐
              │  Brain         │   │  Agents (7)      │
              │  prompt+pick   │   │  browser/research│
              └────┬───────────┘   └────┬─────────────┘
                   │                    │
        ┌──────────▼─────────┐   ┌──────▼────────────┐
        │ API Manager        │   │  Tool Registry    │
        │ health·quota·retry │   │  123 tools / 10   │
        │ verified failover  │   │  categories       │
        └──────────┬─────────┘   └──────┬────────────┘
                   │                    │
   ollama→groq→cerebras→openrouter→   browser·screen·search·crawl·
   gemini→hf→stub                     code·file·utility·comms·memory·obsidian
                   │                    │
              ┌────▼────────────────────▼─────┐
              │  Smart Memory Manager         │
              │  working/episodic/semantic/   │
              │  procedural · hybrid recall   │
              └───────────────────────────────┘
```

- **Context manager** keeps the prompt inside a 14 000-token budget: it merges system blocks, shrinks tool results, compacts old turns into a running summary, and guarantees the newest user turn always survives.
- **Tool selection** scores every tool against the request (category + name + docstring overlap with regex boosts for maths, dates and URLs), so a small local model gets ≤ 9 relevant tools instead of all 123.
- **Everything is inspectable**: `jarvis status`, `jarvis doctor`, `TurnResult.context_line`, `memory.budget_report()`.

---

## Free-tier limits

JARVIS tracks every provider's free allowance and refuses to exceed it (`data/quota.db`):

| Provider | Free allowance (tracked) | Notes |
| --- | --- | --- |
| ollama | unlimited | local models — the primary free brain |
| groq | 30 rpm / 14 400 rpd | very fast free tier, no card |
| cerebras | 30 rpm / 14 400 rpd | free tier |
| openrouter | 20 rpm / 50 rpd | free models (`:free` suffix) |
| gemini | 15 rpm / 1 500 rpd | free tier |
| huggingface | 10 rpm / 1 000 rpd | inference API |
| tavily | 1 000 searches/month | search |
| serper | 2 500 one-time credits | search |
| firecrawl | 500 one-time credits | crawl (last resort) |
| jina reader | ~20 rpm keyless | crawl (first choice) |

`jarvis quota` shows live usage; `jarvis quota --reset groq` clears a counter (e.g. after the daily reset).

---

## Configuration

`config.yaml` is the single source of truth; everything can also come from the environment with the `JARVIS__SECTION__KEY` pattern, and secrets are read from `.env` / real env vars (see `.env.example`).

```yaml
llm:
  provider: auto                 # auto = verified fallback chain
  fallback_chain: [ollama, groq, cerebras, openrouter, gemini, hf, stub]
  model: hermes3:3b              # small enough for a 4 GB laptop
  cost_policy: free              # refuse anything that could bill money

browser:
  engine_order: [playwright, selenium, cdp, screen]
  hybrid:  {enabled: true, move_duration_ms: 260, click_settle_ms: 350}

screen:
  vision_model: false            # by design — OCR + DOM only
  ocr: {min_confidence: 45, fuzzy_threshold: 0.8}

memory:
  working_window: 24
  importance_threshold: 0.35
  consolidation_interval: 25
  recall_budget: 3500
  long_term: {embedding_provider: auto, dedupe_threshold: 0.9}
```

Useful environment variables: `JARVIS_CONFIG` (alternate config file), `JARVIS_TRASH_DIR`, `JARVIS_CHROME_PATH`, `JARVIS_PROFILE`, `JARVIS_API_HOST/PORT/TOKEN`, plus the provider keys (`GROQ_API_KEY`, `CEREBRAS_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `HUGGINGFACE_API_KEY`, `TAVILY_API_KEY`, `SERPER_API_KEY`, `FIRECRAWL_API_KEY`, `JINA_API_KEY`).

---

## Troubleshooting / doctor

`python -m jarvis doctor` is the first thing to run on a new machine — it reports and *fixes*:

| Symptom | Fix (JARVIS prints this verbatim) |
| --- | --- |
| `no display` / screen tools refuse | `bash scripts/start-xvfb.sh` or log into a desktop session |
| cursor/typing does nothing on Linux | `apt-get install xdotool` (or `ydotool` on Wayland) |
| `browser: needs Chrome` | `pip install playwright && playwright install chromium` |
| OCR returns nothing | `apt-get install tesseract-ocr` + `pip install pytesseract` |
| `no API key` for every LLM | `ollama serve && ollama pull hermes3:3b`, or set one free key |
| search finds nothing | keyless DDG/Jina need outbound network; a key is faster |
| first response is slow | first ollama call loads the model into RAM (normal) |

---

## Tests

```bash
python -m pytest tests/ -q          # 179 tests, no network, no model needed
```

| File | Covers |
| --- | --- |
| `tests/test_core.py` | config, messages, context budgeting, api manager fallback, quota, rate limiting |
| `tests/test_tools.py` | registry, schema validation, filesystem, bash safety matrix, memory tools, honest-failure contract |
| `tests/test_browser_screen.py` | DOM parsing, selectors, screen-point mapping, harness chain, cursor maths |
| `tests/test_memory.py` | importance gate, dedupe, decay, consolidation, recall budget, skills, sessions |
| `tests/test_agent_flow.py` | orchestrator turns, tool selection, agent routing, API surface, CLI wiring |
| `tests/test_obsidian.py` | vault parsing, search, daily notes, flashcards + SM-2, study kit/plan, plugin install, prebuilt bundles (local HTTP server, digest checks, offline fallback) |
| `tests/test_coach.py` | retention analytics, the daily brief, idempotent daily-note writes, the scheduler (injected clock), review logging, PyYAML-free frontmatter |

Everything is hermetic: a temporary config, a temporary home, blanked API keys, the stub provider.

---

## CI / CD on GitHub

Three workflows live in `.github/workflows/` and need no secrets beyond the
automatic `GITHUB_TOKEN`:

| Workflow | Trigger | Does |
| --- | --- | --- |
| `ci.yml` · CI | push to `main`/`arena/**`, every PR, manual | **lint** (compileall + a narrow ruff gate on real bugs + advisory full report), **tests** on Python 3.10/3.11/3.12, an advisory **Windows** run (where the screen tools actually run), a CLI smoke, and **package** (builds the wheel/sdist, installs it into a clean venv and runs it) |
| `cd-release.yml` · CD · release | a `v*` tag, or manual | re-runs the suite, builds the wheel + sdist + `jarvis-obsidian-plugin.zip`, installs the wheel clean, then publishes a GitHub Release with generated notes and every asset attached |
| `cd-website.yml` · CD · website | manual, or a `site-*` tag | publishes `docs/website` to GitHub Pages |

Cut a release:

```bash
git tag v1.0.1
git push origin v1.0.1        # → the Release appears with artefacts attached
```

One-time setup for the website: **Settings → Pages → Build and deployment →
Source: GitHub Actions**, then run *CD · website* from the Actions tab. It is
manual on purpose so an unconfigured Pages setup can never turn `main` red.

Run the same gates locally before pushing:

```bash
python -m compileall -q jarvis
ruff check --select F821,F822,F811,F823,F841,B023,B904,E713,E9 .
python -m pytest tests/ -q
python -m jarvis doctor && python -m jarvis prompt --budget 3000
```

## Project layout

```
jarvis/
├── core/            orchestrator, brain, api manager, context manager, messages, providers
├── memory/          manager, embedder, vector store, tiers, profile, skills, sessions, knowledge
├── tools/
│   ├── browser/     harness, DOM parser, CDP, terminal launcher, actions, screenshots
│   ├── screen/      backends, mouse/keyboard, capture, OCR, windows, actions
│   ├── search/      base (quota+http), tavily, serper, searxng, parallel
│   ├── crawl/       reader chain, jina, firecrawl, local extractor
│   ├── code/        python executor, bash executor (guarded), analyzer
│   ├── filesystem/  file ops (trash-aware), directory tree, search
│   ├── utility/     calculator, datetime, json, clipboard, image, memory, discovery
│   ├── obsidian/    vault, flashcards + SM-2 scheduler, study kit/plan, plugins
│   └── communication/ email, webhooks, notifications, speech
├── agents/          base, 6 specialists, factory + routing
├── interfaces/      CLI (rich), API server (stdlib/FastAPI)
├── utils/           logger, tokens, sanitizer, rate limiting, quota, platform probe
├── persona.py       who Heoster is (drives profile + system prompt)
├── config.py        layered config + .env + JARVIS__ overrides
└── version.py       name, version, banner
obsidian-plugin/      JARVIS for Obsidian (plain-JS community plugin, no build step)
  └── prebuilt/       offline cache of community plugin bundles (see its README)
knowledge/            operator profile, command reference, student workflow
scripts/             setup, chrome harness, Xvfb, doctor, wrapper
tests/               179 hermetic tests
```

---

## Status & limits

- **Verified here**: 179/179 tests, CLI end-to-end, orchestrator turns (real fallback to `stub`), memory write/recall/consolidate, API server endpoints, crawl chain reporting, bash safety matrix, tool schemas.
- **Needs hardware to verify**: real Chrome engines, cursor movement and OCR — this sandbox has no browser binary, no `DISPLAY` and no `xdotool`. The harness reports *precisely* what is missing rather than failing silently, and `browser --verify --deep` performs the same check on your machine.
- **Not implemented on purpose**: any paid API, any vision model, any telemetry. JARVIS sends data nowhere except the providers and readers you configure.

Built for **Heoster** — CODEEX-AI · MIT licensed · *free for everyone, at zero cost.*
