# JARVIS as an agentic harness

Reference: Databricks, *What is an AI Agent Harness?* (https://www.databricks.com/blog/ai-harness).

The article's core claim: **Agent = Model + Harness.** The model reasons; the harness
supplies the tools, memory, workspace, guardrails, feedback loops, observability and
context management that turn the model into a dependable agent. The core loop is
**reason → act → observe → repeat**.

JARVIS keeps the model behind a provider chain (`jarvis/core/providers.py`, `brain.py`)
and puts everything else in the harness: `jarvis/core/orchestrator.py` runs the loop, and
`jarvis/core/harness.py` holds the guardrail, verification, breaker and audit logic.

## The eight building blocks

| Block | Where it lives in JARVIS | Status |
|---|---|---|
| 1. Model | `brain.py`, `providers.py`: ollama → groq → google → openrouter → … | Done |
| 2. Tools | `jarvis/tools/` registry (about 125 tools, 10 categories), `tool_guide.py` (when and how each tool is used), `text_tool_calls.py` (tool calls written as text) | Done |
| 3. Memory | `memory/` (working window, long-term store, session store, operator profile) | Done |
| 4. Workspace / sandbox | `harness.workspace_roots`; writes outside the roots need approval | **Partial**: see gaps |
| 5. Guardrails and approvals | `Harness.decide`, `harness.approval` = `ask` / `strict` / `auto`, `allow_tools`, `deny_tools` | Done |
| 6. Feedback loops | `verify_result` reads back file writes; `FailureBreaker`; the nudge that makes the model act when it only claims; the claim check that flags an action that was never run | Done |
| 7. Observability | `AuditLog` writes one JSON line per decision and per call to `logs/audit.jsonl` (or `$JARVIS_LOG_DIR`), with secrets redacted | Done |
| 8. Context management | `context_manager.py` shrinks tool results, keeps a token budget, and passes only the relevant tools (`brain.select_relevant_tools`) | Done |

## The loop, step by step

For each model step, the orchestrator does this:

1. **Ask the model** with the relevant tools only.
2. **Parse the calls.** Native tool calls are used as they are. If the model wrote a
   call as text, `extract_text_tool_calls` accepts it only when the name is a bound tool.
3. **Loop breaker.** A tool that has failed `max_failures_per_tool` times in this turn
   (default 2) is blocked. The model gets a "Stopped: … Do not retry" message. The
   loop guard also stops an identical call (same name *and* same arguments) from
   repeating.
4. **Guardrail.** `Harness.decide` classifies the call (see the table below) and decides
   `allow`, `approve` or `deny`.
   - `approve` runs the approver in a worker thread, so the event loop is not blocked.
     With no terminal (API, service, tests) the answer is **no**.
   - `deny` is recorded in the audit log. The model is told the call was not run and
     must not retry it.
5. **Act.** Allowed calls run through the registry.
6. **Verify.** For file writes, the result is read back from disk. A mismatch is shown as
   `VERIFICATION FAILED: …`, and it counts as a failure for the breaker.
7. **Audit.** Each call is recorded with its risk, decision, outcome, verification and
   elapsed time.
8. **Observe and repeat.** The result goes back to the model as a tool message. A final
   answer that claims an action without a matching tool call is flagged, not believed.

## Risk levels and decisions

`classify()` assigns each call one of five levels:

| Level | Examples | `ask` mode | `strict` mode | `auto` mode |
|---|---|---|---|---|
| `read` | `read_file`, `search_web`, `media_play`, `run_pipeline` (see note) | allow | allow | allow |
| `write` (inside the workspace roots) | `write_file`, `append_file`, `copy_path`, `move_path`, `remember` | allow | approve | allow |
| `write` outside the roots, and `external` | writes outside the roots; `send_email`, `http_request`, `download_file`, communication tools | approve | approve | allow |
| `system` | `run_bash`, `run_python`, `launch_application`, `page_click`, `keyboard_type`, desktop and screen tools | approve | approve | allow |
| `destructive` | `delete_path`, or any tool flagged `dangerous` | approve | approve | allow |

- `run_pipeline` is in `DEFAULT_ALLOW_TOOLS`. Pipelines are user-owned files in
  `pipelines/`, and a request to search is the operator's own authorization. A
  `deny_tools` entry still wins.
- `allow_tools` adds to the default allow list. `deny_tools` always wins.
- With no approver, or when the approver fails, the answer is **deny**.

## Configuration

```yaml
harness:
  approval: ask                    # ask | strict | auto
  allow_tools: []                  # run without a prompt, in addition to run_pipeline
  deny_tools: []                   # never run
  workspace_roots: ["."]           # where writes are allowed without approval
  max_failures_per_tool: 2         # breaker limit per tool, per turn
  verify_writes: true              # read back file writes
  audit: true
  audit_path: logs/audit.jsonl     # or set JARVIS_LOG_DIR
```

## Audit log format

`logs/audit.jsonl`, one JSON object per line:

```json
{"ts": 1760000000.1, "event": "approval", "tool": "run_bash", "risk": "system", "approved": false, "args": {"command": "ls"}}
{"ts": 1760000001.4, "event": "tool", "turn": 3, "tool": "write_file", "risk": "write", "decision": "allow", "ok": true, "verified": "verified: notes.txt holds exactly the new content", "elapsed_ms": 4, "args": {"path": "notes.txt"}}
{"ts": 1760000002.0, "event": "denied", "tool": "delete_path", "risk": "destructive", "reason": "the operator did not approve"}
```

Secrets are redacted by `redact_secrets` before each line is written. Auditing never
breaks a turn: a write error is logged as a warning.

## Known gaps (honest status)

- **Sandbox boundary is partial.** `workspace_roots` decides whether a *file write* needs
  approval. `run_bash` and `run_python` are not confined to the roots. They are gated by
  approval (`system` risk), and nothing stops an approved command from touching other
  paths. A real sandbox (container, chroot or a restricted working directory) is future work.
- **Approval UI.** The console approver works only with a terminal. Anywhere without one
  (the API server, tests, services), an `ask`-level call is denied unless `approval: auto`
  or `allow_tools` covers it. A richer approval UI is future work.
- **Verification covers files only.** Browser and desktop actions are confirmed by their
  own tools (for example, `read_screen_text` with `expect`, and the play flow's
  "reports only what the browser confirms"). They do not have a generic read-back.
- **Breaker is per turn.** A tool that fails in one turn can be tried again in the next.
- **Audit is a local file.** It is not shipped anywhere, and there is no trace viewer yet.

## Tests

`tests/test_harness.py` covers the risk matrix for each approval mode, the no-terminal
deny, the broken-approver deny, write and delete verification, the breaker, the redacted
JSONL audit, and the orchestrator path (a denied call never runs, an approved call is
audited, and a repeatedly failing tool is blocked).
