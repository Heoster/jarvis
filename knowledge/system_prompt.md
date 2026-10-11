# JARVIS — the universal system prompt

The model is only as good as what it is told before the first turn. JARVIS builds
**one** system prompt every turn, from live facts, in
`jarvis/core/system_prompt.py` (`build_universal_prompt`). This document explains
what is in it, why, and how to see or change it.

## What goes in

| Section | Content | Source |
| --- | --- | --- |
| Identity | Who JARVIS is, whose agent, Codeex-AI, free models + free APIs, tone | `persona.build_profile` |
| OPERATOR | Name, handle, school/class, timezone | profile (+ web research, cached) |
| RUNTIME | OS, Python, machine, model + provider chain, tool counts per category, search/browser/crawl chains | live: `collect_environment(config, registry=…)` |
| HOW YOU RESPOND | Answer first, then evidence; short; no invented facts | static contract |
| TOOL PROTOCOL | Call by exact name with JSON args; `ok=false` is information; ≤ 1 dependent call at a time; act → verify → answer | static contract |
| MEMORY | Recalled memory and knowledge below are true; look before asking; store preferences, deadlines, decisions | static contract |
| WORKFLOWS / PLAYBOOKS | Step-by-step recipes: browser, media, screen, memory, study, research | `PLAYBOOKS` + `PLAYBOOK_INDEX` |
| HARD RULES | Never invent a tool result / file / URL / number; confirm destructive or public actions; free tiers only | `HARD_RULES` |
| RECALLED MEMORY | Hits from the memory manager for this turn | memory manager |
| KNOWLEDGE BASE | Hits from the local knowledge base | memory manager |
| SESSION CONTEXT | Session id and turn, memory tiers, recent turns, tools bound this turn, local time (Asia/Kolkata) | orchestrator `_session_context()` |

## The budget ladder

The full prompt is ~11 000 characters (≈ 3 000 tokens). A small local model (or a
provider with a tight context) may need less, so the builder degrades in a fixed,
sane order — never by "drop the longest section":

1. full prompt
2. drop knowledge-base context
3. drop recalled memory
4. drop session context
5. compact operator/environment facts
6. one-line-per-playbook index (no full playbooks)
7. contract only (no context at all)
8. minimal contract (`MINIMAL_TEMPLATE`) — identity, operator, tool line, rules,
   and a one-line workflow index

Whatever the ceiling, the result **always** contains: the tool protocol, the
memory rule, the hard rules, and a workflow index. `len(prompt) <= budget + 1`
always holds.

Set the ceiling with `brain.system_prompt_budget` in `config.yaml` (default
12000). `brain.system_prompt: persona` switches back to the older hand-written
persona prompt.

## How it is wired

```
orchestrator.initialize()
  ├── registry.load_all_default_tools(config)
  ├── brain.attach_registry(registry)          # tool counts for RUNTIME
  └── brain.set_session_context(_session_context())

Brain.turn(...)
  ├── select_tools(request) → bind_tools(...)  # usually ≤ 9 tools
  ├── memory.recall(request) → memory block
  ├── brain.system_prompt(memory=…, knowledge=…)
  │     └── build_universal_prompt(environment, config, tools,
  │                                memories, knowledge, session, max_chars)
  └── messages[0] = system prompt, then the conversation
```

## See it

```bash
python -m jarvis prompt                                    # the real thing
python -m jarvis prompt --budget 3000 --style persona
python -m jarvis prompt --tools media_play,page_click --memory "favourite song: Kesariya"
python -m jarvis prompt --out prompt.txt
```

`tests/test_system_prompt.py` locks this in: environment facts, memory line,
contract sections, bound-tool-only listing, playbooks for browser/media/screen,
and the budget ladder from 1 200 to 12 000 characters.
