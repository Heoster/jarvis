# JARVIS quick reference

## Start

```bash
python -m jarvis                  # interactive chat
python -m jarvis doctor           # what works on this machine?
python -m jarvis ask "question"   # one-shot
```

## Focus areas

```bash
python -m jarvis browser --launch --url youtube.com   # real Chrome, from the terminal
python -m jarvis browser --verify --deep              # prove the engine fallback works
python -m jarvis screen                               # screen capabilities (OCR, no vision model)
python -m jarvis screen --click-text "Sign in"        # find text on screen and click it
python -m jarvis memory                               # smart memory report
python -m jarvis api --verify                         # prove the LLM fallback works
python -m jarvis quota                                # free-tier usage per provider
```

## Run any single tool (with real JSON)

```bash
python -m jarvis call media_history '{"day":"yesterday"}'
python -m jarvis call media_play    '{"query":"kesariya arijit singh"}'
python -m jarvis call page_type     '{"selector":"input#search","text":"hello"}'
python -m jarvis call page_click    '{"selector":"button#go"}'
python -m jarvis call page_tab_switch '{"target":"results"}'
python -m jarvis call page_tabs       '{}'
python -m jarvis call page_tab_close  '{"target":"2"}'
```

## See the system prompt the model receives

```bash
python -m jarvis prompt                       # the live prompt (real facts)
python -m jarvis prompt --budget 3000         # what fits in 3 000 characters
python -m jarvis prompt --memory "favourite song: Kesariya" --out prompt.txt
python -m jarvis prompt --style persona       # the older hand-written persona prompt
```

## Music: what was played, favourites, replay

```bash
python -m jarvis call media_play '{}'                        # replay the last song
python -m jarvis call media_play '{"query":"my favourite song"}'
python -m jarvis call media_favourite '{"song":"Kesariya – Arijit Singh"}'
python -m jarvis call media_history '{"day":"yesterday"}'    # what did I play yesterday?
python -m jarvis call media_history '{"query":"arijit","since_days":30}'
```

In chat, just say it plainly: *"play my favourite song"*, *"what did I play
yesterday?"*, *"open youtube in a new tab and search for lofi"* — the browser
playbooks in the system prompt tell the model which tools to chain.

## Useful shell helpers

```bash
scripts/jarvis-setup.sh      # install everything (venv + deps + chromium)
scripts/jarvis-doctor.sh     # run the capability report
scripts/chrome-harness.sh    # launch Chrome with the debug port JARVIS expects
scripts/start-xvfb.sh        # headless Linux: virtual display for screen control
```

## Free-tier budget cheatsheet

| Provider | Free allowance | JARVIS default |
| --- | --- | --- |
| Ollama (local) | unlimited | primary brain |
| Groq | generous RPM/RPD | first cloud fallback |
| OpenRouter (free router) | 20 req/min-ish | third fallback |
| Gemini | generous RPD | fourth fallback |
| Hugging Face | serverless rate limited | fifth fallback |
| Tavily | 1 000 searches/month | search #1 |
| Serper | 2 500 credits total | search #2 |
| SearXNG (self-host) | unlimited | search #3 |
| DuckDuckGo HTML | free, unofficial | search #4 |
| Jina Reader | free tier | crawl #1 |
| Firecrawl | 500 credits total | crawl #3 |

## Obsidian (notes · flashcards · study plans)

```bash
python -m jarvis obsidian status                  # vault, due cards, exams
python -m jarvis obsidian search "thermodynamics" # search every note
python -m jarvis obsidian daily --text "- [ ] revise optics"
python -m jarvis obsidian cards due               # today's revision
python -m jarvis obsidian cards add "Q :: A"      # write a flashcard
python -m jarvis obsidian cards from-note --note "Newton's Laws"
python -m jarvis obsidian cards review --note X --grade good "question"
python -m jarvis obsidian plan --exam 2027-03-01  # day-by-day revision plan
python -m jarvis obsidian queue                   # what to revise right now
python -m jarvis obsidian kit                     # build the student vault
python -m jarvis obsidian plugins                 # plugins + reasons
python -m jarvis obsidian plugins --install       # install the JARVIS Obsidian plugin
python -m jarvis obsidian plugins --install-prebuilt all   # prebuilt community plugins
python -m jarvis obsidian plugins --install-prebuilt "dataview, tasks"
python -m jarvis obsidian plugins --install-prebuilt all --offline
python -m jarvis obsidian plugins --update        # newest release of what's installed
python -m jarvis obsidian plugins --list-prebuilt --checked   # catalogue + updates
python scripts/fetch_prebuilt_plugins.py          # vendor bundles for offline use
```

## Study coach (brief · retention · schedule)

```bash
python -m jarvis coach                            # retention + next brief time
python -m jarvis coach weak --limit 5 --days 30   # what you keep forgetting
python -m jarvis coach brief                      # what to study right now
python -m jarvis coach brief --write              # into today's daily note
python -m jarvis coach brief --json               # for scripts
python -m jarvis coach weekly                     # review note (retention table)
python -m jarvis coach watch --at 19:30           # background brief + notification
python -m jarvis coach watch --at 07:00,19:30 --speak --weekly
python -m jarvis coach watch --once               # one cycle now (cron-friendly)
```
