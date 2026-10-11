"""Shell command execution with a blocklist and confirmation hooks.

Safety model (pragmatic, not theatrical):

* an explicit **blocklist** for catastrophic commands (disk wipe, fork bomb…);
* **destructive-pattern detection** (``rm -rf``, ``git reset --hard``,
  ``DROP TABLE``…) that returns a *confirmation prompt* instead of executing;
* a timeout and output caps so one runaway command cannot wedge the session;
* every command is logged to the audit trail.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...config import load_config
from ...utils.logger import get_logger
from ...utils.sanitizer import redact_secrets
from ..base import tool

log = get_logger("code.bash")

__all__ = ["run_bash", "BashResult", "check_command", "BLOCKED", "DESTRUCTIVE"]

BLOCKED: List[str] = [
    "mkfs", "wipefs", ":(){", "fork bomb", "shutdown", "reboot", "poweroff",
    "chmod -R 777 /", "format c:", "del /f /s /q c:\\",
]

# Regex blocklist: matches the *shape* of a catastrophic command, so a harmless
# `rm -rf /tmp/scratch` passes while `rm -rf /` and `rm -rf /etc` never do.
BLOCKED_PATTERNS: List[str] = [
    r"rm\s+-[a-z]*[rf][a-z]*\s+(/|/\*|~|\$HOME)\s*$",
    r"rm\s+-[a-z]*[rf][a-z]*\s+/(etc|usr|var|bin|sbin|lib|lib64|boot|dev|proc|sys)\b",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b.*\b(of|if)=\s*/dev/(sd|nvme|hd|mmcblk|disk)",
    r"\bdd\b.*\bof=\s*/dev/",
    r"\b(shutdown|reboot|halt|poweroff)\b",
    r"\binit\s+0\b",
    r":\s*\(\s*\)\s*\{",
    r"\bchmod\s+-R\s+777\s+/\s*$",
    r"\bchown\s+-R\s+\S+\s+/\s*$",
    r"\bwipefs\b",
    r"(curl|wget)[^|]*\|\s*(sudo\s+)?(ba)?sh\b",
    r"\bmv\s+/\*\s",
    r"\bformat\s+c:",
    r"\bdel\s+/f\s+/s\s+/q\s+c:",
]

DESTRUCTIVE: List[tuple[str, str]] = [
    (r"\brm\s+-[rf]{1,2}\b", "recursive/forced delete"),
    (r"\brm\s+.*\*", "wildcard delete"),
    (r"\bgit\s+reset\s+--hard\b", "discards uncommitted work"),
    (r"\bgit\s+clean\s+-[a-z]*f", "deletes untracked files"),
    (r"\bgit\s+push\s+--force\b", "rewrites remote history"),
    (r"\bdrop\s+(table|database)\b", "database destruction"),
    (r"\btruncate\s+-s\s*0\b", "empties a file"),
    (r"\b>\s*/dev/sd", "writes to a raw disk"),
    (r"\bkillall\b|\bpkill\s+-9\b", "kills processes"),
    (r"\bsudo\b", "elevated privileges"),
    (r"\bpip\s+uninstall\b.*-y", "removes installed packages"),
]


@dataclass
class BashResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    elapsed_ms: int = 0
    blocked: bool = False
    needs_confirmation: bool = False
    reason: str = ""

    def as_text(self) -> str:
        if self.blocked:
            return f"🚫 **Blocked:** {self.reason}"
        if self.needs_confirmation:
            return (f"⚠️ **Confirmation required:** {self.reason}\n"
                    f"Command: `{self.stdout}`\n\n"
                    f"If Heoster confirms, re-run with `confirm_destructive=True`.")
        parts: List[str] = []
        if self.stdout:
            parts.append(f"```\n{self.stdout.rstrip()}\n```")
        if self.stderr:
            parts.append(f"**stderr:**\n```\n{self.stderr.rstrip()[:1500]}\n```")
        if not parts:
            parts.append(f"✅ Command completed (exit code {self.returncode})")
        else:
            parts.append(f"_(exit code {self.returncode}, {self.elapsed_ms} ms)_")
        return "\n\n".join(parts)


def _config() -> Dict[str, Any]:
    config = load_config()
    bash_cfg = ((config.get("code") or {}).get("bash") or {})
    return {"timeout": float(bash_cfg.get("timeout", 15)),
            "enabled": bool(bash_cfg.get("enabled", True)),
            "blocked": list(bash_cfg.get("blocked_commands") or [])}


def check_command(command: str, *, confirm_destructive: bool = False) -> Dict[str, Any]:
    """Classify a command: ``ok`` / ``blocked`` / ``needs_confirmation``."""
    lowered = command.lower().strip()
    if not lowered:
        return {"status": "blocked", "reason": "empty command"}

    for pattern in BLOCKED + _config()["blocked"]:
        if pattern and pattern.lower() in lowered:
            return {"status": "blocked",
                    "reason": f"'{pattern}' is on the dangerous-command list"}

    # regex blocklist: matches the *shape* of a catastrophic command, so
    # `rm -rf /tmp/scratch` passes while `rm -rf /` and `rm -rf /etc` do not.
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, lowered):
            return {"status": "blocked",
                    "reason": f"matched dangerous pattern {pattern!r}"}

    if confirm_destructive:
        return {"status": "ok"}

    for pattern, why in DESTRUCTIVE:
        if re.search(pattern, lowered):
            return {"status": "needs_confirmation", "reason": why,
                    "pattern": pattern}
    return {"status": "ok"}


def _run_sync(command: str, timeout: float, cwd: Optional[str],
              env: Optional[Dict[str, str]]) -> BashResult:
    start = time.perf_counter()
    try:
        completed = subprocess.run(command, shell=True, capture_output=True,
                                   text=True, timeout=timeout, cwd=cwd,
                                   stdin=subprocess.DEVNULL, env=env)
        return BashResult(
            ok=completed.returncode == 0,
            stdout=redact_secrets(completed.stdout[-8000:]) if completed.stdout else "",
            stderr=redact_secrets(completed.stderr[-4000:]) if completed.stderr else "",
            returncode=completed.returncode,
            elapsed_ms=int((time.perf_counter() - start) * 1000))
    except subprocess.TimeoutExpired:
        return BashResult(ok=False, stderr=f"⏰ timed out after {timeout}s",
                          elapsed_ms=int((time.perf_counter() - start) * 1000))
    except Exception as exc:
        return BashResult(ok=False, stderr=f"{type(exc).__name__}: {exc}")


@tool(category="code", timeout=120)
def run_bash(command: str, timeout: float = 15, working_dir: str = "",
             confirm_destructive: bool = False) -> str:
    """Run a shell command and return stdout/stderr.

    Use for: git, pip, file inspection, running scripts, checking processes.
    Dangerous commands (rm -rf, disk tools) are blocked; genuinely destructive
    ones ask for confirmation first.

    Args:
        command: The shell command to run
        timeout: Seconds before the command is killed
        working_dir: Directory to run in (default: project root)
        confirm_destructive: Set true only after Heoster explicitly approves a destructive command
    """
    config = _config()
    if not config["enabled"]:
        return (False, "Shell execution is disabled in config.yaml (code.bash.enabled).")

    verdict = check_command(command, confirm_destructive=confirm_destructive)
    if verdict["status"] == "blocked":
        log.warning(f"blocked command: {redact_secrets(command)[:120]}")
        return (False, BashResult(ok=False, blocked=True,
                                  reason=verdict["reason"]).as_text())
    if verdict["status"] == "needs_confirmation":
        return (False, BashResult(ok=False, needs_confirmation=True,
                                  stdout=command,
                                  reason=verdict["reason"]).as_text())

    cwd = working_dir or str(Path(__file__).resolve().parents[3])
    effective_timeout = min(float(timeout or config["timeout"]), 600.0)
    log.debug(f"bash: {redact_secrets(command)[:160]} (cwd={cwd})")
    result = _run_sync(command, effective_timeout, cwd, None)
    if not result.ok:
        return (False, result.as_text())
    return result.as_text()


@tool(category="code", timeout=60)
def which_command(name: str) -> str:
    """Check whether a program exists on PATH and where it lives.

    Args:
        name: Program name, e.g. "google-chrome", "tesseract", "ffmpeg"
    """
    import shutil

    path = shutil.which(name)
    if path:
        return f"✅ {name} → {path}"
    return f"❌ {name} is not installed (not on PATH)"


@tool(category="code", timeout=30)
def system_info() -> str:
    """Report OS, CPU, memory, Python version and key tool availability.

    Useful before planning anything heavy (models, browsers, screen control).
    """
    from ...utils.platform_probe import probe_platform

    info = probe_platform(refresh=True)
    return info.summary()


SHELL_TOOLS = [run_bash, which_command, system_info]
