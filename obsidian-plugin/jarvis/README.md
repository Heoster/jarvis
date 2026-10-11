# JARVIS for Obsidian

Ask your local **JARVIS** operator about the note you are reading: summaries,
explanations, research, flashcards and your revision queue — without leaving
Obsidian, without an account, and without paying anything.

## Install

**From a terminal (recommended — it enables the plugin for you):**

```bash
python -m jarvis obsidian plugins --install          # uses OBSIDIAN_VAULT / config
# or point it at a vault explicitly:
OBSIDIAN_VAULT="/path/to/MyVault" python -m jarvis obsidian plugins --install
```

Then in Obsidian: **Settings → Community plugins → turn off Restricted mode →
enable “JARVIS”**.

**Manually:** copy this whole folder to `<vault>/.obsidian/plugins/jarvis/`
(it must contain `manifest.json`, `main.js`, `styles.css`).

## Requirements

1. The JARVIS API server running locally:

   ```bash
   python -m jarvis serve --port 8077
   ```

2. That's it. No keys, no cloud. If you use `JARVIS_API_TOKEN` on the server,
   paste the same token into the plugin settings.

## Commands (Ctrl/Cmd+P)

| Command | What it does |
| --- | --- |
| **Ask JARVIS a question** | free-form question, answer inserted into the note |
| **Explain the current note** | core ideas → tricky parts → worked example |
| **Summarize the current note** | 5 revision bullets + 3 exam questions |
| **Explain the selected text** | step-by-step explanation under the selection |
| **Make flashcards from the current note** | writes `Question :: Answer` lines under `## Flashcards` |
| **Show today's revision queue** | due cards + overdue tasks + `#revision` notes |
| **Research this topic with JARVIS** | JARVIS searches and reads the web, then writes the section |
| **Save selection to JARVIS memory** | stores the text in long-term memory |
| **Test the JARVIS connection** | confirms the server is reachable |

The **ribbon robot icon** opens a chat panel: answers render as markdown and each
one has *Insert into note*, *Copy* and *Flashcards* buttons. A status-bar
indicator shows `● JARVIS` (online) or `○ JARVIS offline`.

## Flashcards & the revision queue

Cards are plain markdown in your notes:

```markdown
- What is the SI unit of pressure? :: pascal (Pa)
```

or cloze deletions:

```markdown
Photosynthesis happens in the {{c1::chloroplast}}.
```

JARVIS schedules them (SM-2) in `data/obsidian_srs.json` and hands them back via
`obsidian_flashcards_due` / `obsidian_revision_queue`. The **Spaced Repetition**
community plugin understands the same syntax, so both can live side by side.

## Settings

- **API URL** — default `http://127.0.0.1:8077`
- **Bearer token** — only if the server requires one
- **Timeout** — local models on a laptop need 1–2 minutes
- **Insert answers as** — callout block, plain markdown, or nothing
- **Flashcard / summary headings** — where generated content goes

## Privacy

Everything is sent to `127.0.0.1` only. Your notes leave the machine **only** if
you configured a cloud LLM provider in `config.yaml` — and JARVIS is designed to
work fully offline with ollama.

## Notes

- Plain CommonJS, no build step, no dependencies: `main.js` talks to the JARVIS
  REST API with Obsidian's `requestUrl` (so there are no CORS surprises).
- Works on desktop and mobile as long as the vault can reach the JARVIS server.
- MIT licensed, like JARVIS itself.
