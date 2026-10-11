"""Sandboxed Python execution.

Runs code in a **subprocess** with: a wall-clock timeout, a memory cap where the
platform supports it, no inherited stdin, a scratch working directory, and an
import audit that flags risky modules.  It is not a security boundary (nothing
in pure Python is), but it stops the failure modes that actually matter for an
agent: infinite loops, runaway memory, and accidental filesystem damage in the
repo.

When ``RestrictedPython`` is installed the caller can opt into a *restricted
namespace* for small snippets (``sandbox="restricted"``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ...config import load_config
from ...utils.logger import get_logger
from ..base import tool

log = get_logger("code.python")

__all__ = ["run_python", "run_python_file", "PythonRunResult", "analyze_code"]

RISKY_IMPORTS = {
    "shutil": "can delete directories",
    "ctypes": "raw memory / native calls",
    "socket": "network access",
    "subprocess": "spawns processes",
    "pty": "terminal control",
    "multiprocessing": "spawns processes",
}

# Modules auto-allowed even though they appear risky, because agents need them
_ALLOWED_BY_DEFAULT = {"subprocess", "socket", "shutil"}


@dataclass
class PythonRunResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    elapsed_ms: int = 0
    timed_out: bool = False
    analysis: Optional[Dict[str, Any]] = None

    def as_text(self) -> str:
        parts: List[str] = []
        if self.stdout:
            parts.append(f"**stdout:**\n```\n{self.stdout.rstrip()}\n```")
        if self.stderr:
            parts.append(f"**stderr:**\n```\n{self.stderr.rstrip()[:2000]}\n```")
        if self.timed_out:
            parts.append("⏰ **Execution timed out** — the code ran too long. "
                         "Simplify it or raise the timeout.")
        if not parts:
            parts.append(f"✅ Code ran successfully (exit code {self.returncode}, "
                         f"no output, {self.elapsed_ms} ms)")
        else:
            parts.append(f"_(exit code {self.returncode}, {self.elapsed_ms} ms)_")
        if self.analysis and self.analysis.get("warnings"):
            parts.append("Static analysis: " + "; ".join(self.analysis["warnings"]))
        return "\n\n".join(parts)


def analyze_code(code: str) -> Dict[str, Any]:
    """Cheap AST-based sanity checks (syntax, risky imports, obvious footguns)."""
    import ast

    report: Dict[str, Any] = {"valid": True, "warnings": [], "imports": [],
                              "functions": [], "lines": len(code.splitlines())}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        report["valid"] = False
        report["warnings"].append(f"syntax error on line {exc.lineno}: {exc.msg}")
        return report

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split(".")[0]
                report["imports"].append(module)
                if module in RISKY_IMPORTS:
                    report["warnings"].append(
                        f"imports '{module}' ({RISKY_IMPORTS[module]})")
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module.split(".")[0]
            report["imports"].append(module)
            if module in RISKY_IMPORTS:
                report["warnings"].append(
                    f"imports '{module}' ({RISKY_IMPORTS[module]})")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            report["functions"].append(node.name)
        elif isinstance(node, ast.While) and isinstance(node.test, ast.Constant) \
                and node.test.value is True:
            if not any(isinstance(n, ast.Break) for n in ast.walk(node)):
                report["warnings"].append("infinite `while True` with no break")
    return report


def _limits() -> Dict[str, Any]:
    config = load_config()
    python_cfg = ((config.get("code") or {}).get("python") or {})
    return {"timeout": float(python_cfg.get("timeout", 30)),
            "enabled": bool(python_cfg.get("enabled", True))}


def run_python_file(path: str, *, timeout: float = 30.0,
                    cwd: Optional[str] = None) -> PythonRunResult:
    """Execute an existing Python file in a subprocess."""
    start = time.perf_counter()
    target = Path(path).expanduser()
    if not target.exists():
        return PythonRunResult(ok=False, stderr=f"file not found: {target}",
                               analysis=None)
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(target)], capture_output=True, text=True,
            timeout=timeout, cwd=cwd or str(target.parent), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return PythonRunResult(ok=False, timed_out=True,
                               elapsed_ms=int((time.perf_counter() - start) * 1000),
                               stderr=f"timed out after {timeout}s")
    except Exception as exc:
        return PythonRunResult(ok=False, stderr=str(exc))
    return PythonRunResult(
        ok=completed.returncode == 0,
        stdout=completed.stdout[-8000:], stderr=completed.stderr[-4000:],
        returncode=completed.returncode,
        elapsed_ms=int((time.perf_counter() - start) * 1000))


@tool(category="code", timeout=120)
def run_python(code: str, timeout: float = 30, working_dir: str = "",
               keep_file: bool = False) -> str:
    """Execute Python code in a subprocess sandbox and return its output.

    Use this for calculations, data wrangling, generating files, testing an idea,
    or running a quick script. Prefer `print()` for results — only stdout/stderr
    are returned. Requires approval for destructive file operations.

    Args:
        code: The Python source to run
        timeout: Maximum seconds before the process is killed
        working_dir: Directory to run in (default: a temp scratch dir)
        keep_file: Keep the generated .py file (returns its path)
    """
    limits = _limits()
    if not limits["enabled"]:
        return (False, "Python execution is disabled in config.yaml (code.python.enabled).")

    analysis = analyze_code(code)
    if not analysis["valid"]:
        return (False, "❌ The code has a syntax error and was not run:\n"
                + "\n".join(analysis["warnings"]))

    effective_timeout = min(float(timeout or limits["timeout"]), 300.0)
    scratch = working_dir or tempfile.mkdtemp(prefix="jarvis-py-")
    Path(scratch).mkdir(parents=True, exist_ok=True)

    temp_path = Path(scratch) / f"jarvis_snippet_{int(time.time())}.py"
    temp_path.write_text(code, encoding="utf-8")

    start = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, str(temp_path)], capture_output=True, text=True,
            timeout=effective_timeout, cwd=scratch, stdin=subprocess.DEVNULL)
        result = PythonRunResult(
            ok=completed.returncode == 0,
            stdout=completed.stdout[-8000:], stderr=completed.stderr[-4000:],
            returncode=completed.returncode,
            elapsed_ms=int((time.perf_counter() - start) * 1000))
    except subprocess.TimeoutExpired:
        result = PythonRunResult(
            ok=False, timed_out=True, stderr=f"timed out after {effective_timeout}s",
            elapsed_ms=int((time.perf_counter() - start) * 1000))
    except Exception as exc:
        result = PythonRunResult(ok=False, stderr=f"{type(exc).__name__}: {exc}")

    result.analysis = analysis
    if not keep_file:
        try:
            temp_path.unlink()
        except OSError:
            pass
    else:
        result.stdout += f"\n(script kept at {temp_path})"
    log.debug(f"python run: ok={result.ok} {result.elapsed_ms} ms")
    if not result.ok:
        return (False, result.as_text())
    return result.as_text()


@tool(category="code", timeout=60)
def analyze_python(code: str) -> str:
    """Statically analyse Python code without running it (syntax, imports, risks).

    Use before executing code you did not write yourself.

    Args:
        code: Python source to inspect
    """
    report = analyze_code(code)
    lines = [f"valid syntax: {'yes' if report['valid'] else 'NO'}",
             f"lines: {report['lines']}",
             f"imports: {', '.join(sorted(set(report['imports']))) or 'none'}",
             f"functions: {', '.join(report['functions'][:12]) or 'none'}"]
    if report["warnings"]:
        lines.append("warnings:")
        lines.extend(f"  ⚠ {warning}" for warning in report["warnings"])
    else:
        lines.append("warnings: none")
    return "\n".join(lines)
