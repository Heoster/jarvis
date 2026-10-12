"""The agent harness around the model: guardrails, approvals, verification, audit, loop breaker.

The model decides *what* to do.  This module decides what the harness will *let*
happen, checks that it happened, and records it.  It maps to the building blocks
in Databricks' "What is an AI agent harness?" (see docs/HARNESS.md):

- **Guardrails / human-in-the-loop** — :func:`classify` gives every tool call a
  risk level (read, write, external, system, destructive) and :class:`Harness`
  asks the operator before anything that is not a plain read.
- **Feedback loops / verification** — :func:`verify_result` reads back the effect of
  a file write, move or delete, so a success message is checked, not trusted.
- **Observability** — :class:`AuditLog` appends one JSON line per tool decision.
- **Loop control** — :class:`FailureBreaker` stops a tool that keeps failing in one turn.

Everything here is pure policy or small I/O; the orchestrator wires it in.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from ..utils.logger import get_logger
from ..utils.sanitizer import redact_secrets, truncate

log = get_logger("harness")

__all__ = ["RISK_LEVELS", "Decision", "ApprovalRequest", "Harness", "AuditLog",
           "FailureBreaker", "classify", "verify_result", "console_approver"]

RISK_LEVELS = ("read", "write", "external", "system", "destructive")

# Tools whose effect is outside the machine or outside the operator's control.
_EXTERNAL = {"send_email", "send_notification", "send_webhook", "http_request",
             "download_file", "firecrawl_crawl"}
# Tools that act on the desktop or a live browser page: they can click or type into anything.
_SYSTEM = {"run_bash", "run_python", "launch_application", "browser_launch",
           "page_click", "page_type", "page_press", "cursor_click", "cursor_move",
           "keyboard_type", "keyboard_press", "screen_click_text", "screen_fill_field",
           "screen_focus_window", "screen_window_action", "screen_scroll",
           "run_in_terminal_window", "run_pipeline", "clipboard_set"}
# Tools that change files or stored data.
_WRITE = {"write_file", "append_file", "copy_path", "move_path", "obsidian_write_note",
          "obsidian_flashcards_add", "obsidian_daily_note", "remember", "forget"}
_DESTRUCTIVE = {"delete_path"}
# Allowed without a prompt in ``ask`` mode (still audited and breaker-checked).
DEFAULT_ALLOW_TOOLS = {"run_pipeline"}
_PATH_ARGS = ("path", "destination", "src", "dst", "directory", "root")


@dataclass
class Decision:
    tool: str
    risk: str
    action: str            # "allow" | "approve" | "deny"
    reason: str = ""


@dataclass
class ApprovalRequest:
    tool: str
    risk: str
    args: Dict[str, Any]
    reason: str

    def prompt(self) -> str:
        shown = truncate(redact_secrets(json.dumps(self.args, default=str)), 240)
        return f"JARVIS wants to run {self.tool} ({self.risk}: {self.reason})\n  args: {shown}"


def _inside(path_text: str, roots: Sequence[Path]) -> bool:
    try:
        target = Path(os.path.expandvars(str(path_text))).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def classify(name: str, args: Mapping[str, Any], *, category: str = "",
             dangerous: bool = False, roots: Sequence[Path] = ()) -> Dict[str, str]:
    """Risk level of a call: ``{"risk": ..., "reason": ...}``.

    Writes inside the workspace roots are "write"; writes outside them are "external"
    so they need approval.  Unknown tools take the risk of their category.
    """
    if name in _DESTRUCTIVE or dangerous:
        return {"risk": "destructive", "reason": "deletes or overwrites data"}
    if name in _EXTERNAL:
        return {"risk": "external", "reason": "acts outside this machine"}
    if name in _SYSTEM:
        return {"risk": "system", "reason": "controls the desktop, browser or a shell"}
    if name in _WRITE or name.startswith(("write_", "append_", "obsidian_write")):
        paths = [str(args[k]) for k in _PATH_ARGS if args.get(k)]
        if paths and roots and not all(_inside(p, roots) for p in paths):
            return {"risk": "external", "reason": "writes outside the workspace"}
        return {"risk": "write", "reason": "changes local files or stored data"}
    if category in ("communication",):
        return {"risk": "external", "reason": "communication"}
    if category in ("screen", "code"):
        return {"risk": "system", "reason": f"{category} tool"}
    return {"risk": "read", "reason": "read-only"}


class Harness:
    """Decides, asks, verifies and records every tool call the orchestrator makes."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None, *,
                 approver: Optional[Callable[[ApprovalRequest], bool]] = None,
                 audit: Optional["AuditLog"] = None):
        cfg = dict((config or {}).get("harness") or {})
        self.mode = str(cfg.get("approval", "ask")).lower()
        if self.mode not in ("ask", "strict", "auto"):
            self.mode = "ask"
        # Pipelines are user-owned files in ``pipelines/``; a request to search is the
        # operator's own authorization, so ``run_pipeline`` is allowed by default.
        self.allow_tools = set(DEFAULT_ALLOW_TOOLS) | set(cfg.get("allow_tools") or [])
        self.deny_tools = set(cfg.get("deny_tools") or [])
        roots = cfg.get("workspace_roots") or ["."]
        self.roots = [Path(str(r)).expanduser().resolve() for r in roots]
        self.max_failures = int(cfg.get("max_failures_per_tool", 2))
        self.verify_writes = bool(cfg.get("verify_writes", True))
        self.approver = approver if approver is not None else console_approver
        audit_path = cfg.get("audit_path") or os.path.join(
            os.environ.get("JARVIS_LOG_DIR") or "logs", "audit.jsonl")
        self.audit = audit if audit is not None else AuditLog(
            Path(audit_path) if bool(cfg.get("audit", True)) else None)

    def decide(self, name: str, args: Mapping[str, Any], *, category: str = "",
               dangerous: bool = False) -> Decision:
        if name in self.deny_tools:
            return Decision(name, "system", "deny", "listed in harness.deny_tools")
        info = classify(name, args, category=category, dangerous=dangerous, roots=self.roots)
        risk, reason = info["risk"], info["reason"]
        if name in self.allow_tools or self.mode == "auto" or risk == "read":
            return Decision(name, risk, "allow", reason)
        if self.mode == "strict" or risk != "write":
            return Decision(name, risk, "approve", reason)
        return Decision(name, risk, "allow", reason)

    def ask(self, request: ApprovalRequest) -> bool:
        """Synchronous approval: the approver decides; no approver means no."""
        try:
            return bool(self.approver(request))
        except Exception as exc:  # an approver that breaks must not approve
            log.warning(f"approver failed, denying: {exc}")
            return False


def console_approver(request: ApprovalRequest) -> bool:
    """Ask on the terminal.  Without a terminal (API, service, tests) the answer is no."""
    if not sys.stdin or not sys.stdin.isatty():
        log.info(f"no terminal to approve {request.tool}; denied")
        return False
    print(request.prompt())
    try:
        answer = input("  approve? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


# ── verification ───────────────────────────────────────────────────────────
def verify_result(name: str, args: Mapping[str, Any]) -> Optional[str]:
    """Read back the effect of a file tool.  ``None`` if the tool has nothing to check.

    Returns ``"verified: …"`` or ``"VERIFICATION FAILED: …"``.
    """
    try:
        if name == "write_file" and str(args.get("mode", "w")) in ("w", "", "overwrite"):
            target = Path(str(args["path"])).expanduser()
            if not target.exists():
                return "VERIFICATION FAILED: file does not exist after write"
            got = target.read_text(encoding="utf-8", errors="replace")
            want = str(args.get("content", ""))
            if got.replace("\r\n", "\n") == want.replace("\r\n", "\n"):
                return f"verified: {target.name} holds exactly the new content"
            return (f"VERIFICATION FAILED: {target.name} differs from what was written "
                    f"({len(got)} chars on disk, {len(want)} expected)")
        if name == "append_file":
            target = Path(str(args["path"])).expanduser()
            got = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            if str(args.get("content", "")) and str(args["content"]) in got:
                return f"verified: appended text is in {target.name}"
            return f"VERIFICATION FAILED: appended text not found in {target.name}"
        if name == "delete_path":
            target = Path(str(args["path"])).expanduser()
            if target.exists():
                return f"VERIFICATION FAILED: {target} still exists after delete"
            return f"verified: {target.name} is gone"
        if name == "move_path":
            src = Path(str(args["src"])).expanduser()
            dst = Path(str(args["dst"])).expanduser()
            if dst.exists() and not src.exists():
                return f"verified: moved to {dst.name}"
            return "VERIFICATION FAILED: source still present or destination missing"
        if name == "copy_path":
            dst = Path(str(args["dst"])).expanduser()
            return (f"verified: copy exists at {dst.name}" if dst.exists()
                    else "VERIFICATION FAILED: copy not found at destination")
    except (KeyError, OSError, ValueError) as exc:
        return f"VERIFICATION FAILED: could not check ({exc})"
    return None


# ── audit ──────────────────────────────────────────────────────────────────
class AuditLog:
    """Append-only JSON-lines record of harness decisions.  Secrets are redacted."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        row = {"ts": round(time.time(), 3), **event}
        try:
            line = redact_secrets(json.dumps(row, default=str, ensure_ascii=False))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:  # auditing must never break a turn
            log.warning(f"audit write failed: {exc}")


# ── loop control ───────────────────────────────────────────────────────────
@dataclass
class FailureBreaker:
    """Counts failures per tool within one turn and blocks a tool that keeps failing."""

    limit: int = 2
    failures: Dict[str, int] = field(default_factory=dict)
    last_error: Dict[str, str] = field(default_factory=dict)

    def blocked(self, name: str) -> bool:
        return self.failures.get(name, 0) >= self.limit

    def note(self, name: str, ok: bool, error: str = "") -> None:
        if ok:
            self.failures[name] = 0
            return
        self.failures[name] = self.failures.get(name, 0) + 1
        self.last_error[name] = truncate(error or "failed", 160)

    def message(self, name: str) -> str:
        return (f"Stopped: {name} failed {self.failures.get(name, 0)} times this turn "
                f"(last error: {self.last_error.get(name, 'unknown')}). Do not retry it; "
                "answer with what happened, or use a different tool.")
