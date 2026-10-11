# JARVIS

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
- [Tools (100)](#tools-100)
- [Agents](#agents)
- [Architecture](#architecture)
- [Free-tier limits](#free-tier-limits)
- [Configuration](#configuration)
- [Troubleshooting / doctor](#troubleshooting--doctor)
- [Tests](#tests)
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
| Tools | **100**, in 9 categories, all schema-validated |
| Memory | short-term window + episodic / semantic / procedural long-term tiers, hybrid recall |
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

```bash
git clone https://github.com/Heoster/jarvis.git
cd jarvis

# one-shot: venv + core deps + focus-area extras + browser + optional model
bash scripts/jarvis-setup.sh

# or manually
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install playwright selenium websockets mss Pillow pytesseract trafilatura beautifulsoup4
playwright install chromium
```

Then check the machine and doctor the install:

```bash
python -m jarvis doctor          # hardware, screen, browser, memory, API, crawl
```

Nothing above is mandatory beyond the core three (`pyyaml`, `httpx`, `rich`, `click`). Every optional piece degrades to a clear message instead of a crash.

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
| `GET /tools` | all 100 tools with categories |
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

## Tools (100)

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

Every tool has a validated JSON schema (`python -m jarvis tools spec calculate` shows it), a timeout, and returns `ToolResult(ok, content, data, error, elapsed_ms, artifacts)`. Blocked and destructive operations return `ok=false` with the reason — never a silent success.

---

## Agents

Six specialists share the same brain, memory and registry, but only see the tools they need:

| Agent | Iterations | Calls | Toolbox |
| --- | --- | --- | --- |
| `browser` | 8 | 14 | browser + crawl |
| `research` | 7 | 12 | search + crawl + memory |
| `code` | 8 | 16 | code + file |
| `file` | 6 | 12 | file |
| `screen` | 8 | 16 | screen + utility |
| `memory` | 5 | 10 | memory + utility |

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
              │  Brain         │   │  Agents (6)      │
              │  prompt+pick   │   │  browser/research│
              └────┬───────────┘   └────┬─────────────┘
                   │                    │
        ┌──────────▼─────────┐   ┌──────▼────────────┐
        │ API Manager        │   │  Tool Registry    │
        │ health·quota·retry │   │  100 tools / 9    │
        │ verified failover  │   │  categories       │
        └──────────┬─────────┘   └──────┬────────────┘
                   │                    │
   ollama→groq→cerebras→openrouter→   browser·screen·search·crawl·
   gemini→hf→stub                     code·file·utility·comms·memory
                   │                    │
              ┌────▼────────────────────▼─────┐
              │  Smart Memory Manager         │
              │  working/episodic/semantic/   │
              │  procedural · hybrid recall   │
              └───────────────────────────────┘
```

- **Context manager** keeps the prompt inside a 14 000-token budget: it merges system blocks, shrinks tool results, compacts old turns into a running summary, and guarantees the newest user turn always survives.
- **Tool selection** scores every tool against the request (category + name + docstring overlap with regex boosts for maths, dates and URLs), so a small local model gets ≤ 9 relevant tools instead of all 100.
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
python -m pytest tests/ -q          # 86 tests, no network, no model needed
```

| File | Covers |
| --- | --- |
| `tests/test_core.py` | config, messages, context budgeting, api manager fallback, quota, rate limiting |
| `tests/test_tools.py` | registry, schema validation, filesystem, bash safety matrix, memory tools, honest-failure contract |
| `tests/test_browser_screen.py` | DOM parsing, selectors, screen-point mapping, harness chain, cursor maths |
| `tests/test_memory.py` | importance gate, dedupe, decay, consolidation, recall budget, skills, sessions |
| `tests/test_agent_flow.py` | orchestrator turns, tool selection, agent routing, API surface, CLI wiring |

Everything is hermetic: a temporary config, a temporary home, blanked API keys, the stub provider.

---

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
│   └── communication/ email, webhooks, notifications, speech
├── agents/          base, 6 specialists, factory + routing
├── interfaces/      CLI (rich), API server (stdlib/FastAPI)
├── utils/           logger, tokens, sanitizer, rate limiting, quota, platform probe
├── persona.py       who Heoster is (drives profile + system prompt)
├── config.py        layered config + .env + JARVIS__ overrides
└── version.py       name, version, banner
knowledge/            operator profile + command reference (auto-ingested)
scripts/             setup, chrome harness, Xvfb, doctor, wrapper
tests/               86 hermetic tests
```

---

## Status & limits

- **Verified here**: 86/86 tests, CLI end-to-end, orchestrator turns (real fallback to `stub`), memory write/recall/consolidate, API server endpoints, crawl chain reporting, bash safety matrix, tool schemas.
- **Needs hardware to verify**: real Chrome engines, cursor movement and OCR — this sandbox has no browser binary, no `DISPLAY` and no `xdotool`. The harness reports *precisely* what is missing rather than failing silently, and `browser --verify --deep` performs the same check on your machine.
- **Not implemented on purpose**: any paid API, any vision model, any telemetry. JARVIS sends data nowhere except the providers and readers you configure.

Built for **Heoster** — CODEEX-AI · MIT licensed · *free for everyone, at zero cost.*
