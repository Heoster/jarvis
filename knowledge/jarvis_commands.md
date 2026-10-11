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
| Cerebras | generous RPM/RPD | second fallback |
| OpenRouter `:free` | 20 req/min-ish | third fallback |
| Gemini | generous RPD | fourth fallback |
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
```
