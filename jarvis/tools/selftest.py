"""Offline self-test for the tool layer — **no model, no provider, no LLM**.

Two different questions get answered here, and keeping them separate is the
point:

1. **Is the contract valid?** Every tool JARVIS exposes is a JSON-schema function
   the model must be able to read and call: name, description, parameter types,
   required list. A tool with an undocumented parameter or a ``required`` entry
   that is not a property is a broken promise to the model, and it is checkable
   by pure introspection — :func:`contract_issues`.
2. **Does it run?** Some tools can be exercised for real without touching the
   network, the desktop or the operator's data — the calculator, the JSON
   helpers, reading a file from a scratch directory. :data:`PROBES` is that
   curated list, and :func:`run_probe` executes one and reports whether it
   succeeded, failed *gracefully* (a message the model can act on) or crashed
   (a raw Python exception leaking through).

Everything here runs in under a second, needs no API key and never starts
Chrome or Ollama, which makes it the fastest way to answer "did I break the
tool layer?" — from the CLI (``python -m jarvis tools --smoke``) or from pytest
(``tests/test_tool_smoke.py``).

Deliberately **not** probed: anything that talks to the network, moves the
cursor, launches a browser, sends mail or writes outside a scratch directory.
Those are covered by contract checks only; the harness's own ``--verify``
(browser) and ``doctor`` (environment) cover their runtime.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from .base import Tool
from .registry import ToolRegistry

log = get_logger("tools.selftest")

__all__ = ["Issue", "Probe", "ProbeResult", "PROBES", "contract_issues",
           "contract_problems", "check_contracts", "prepare_workspace",
           "run_probe", "arun_probe", "run_smoke", "smoke_report"]

#: What a tool name has to look like for every provider we send it to.
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
#: Below this, the model has nothing to decide with.
MIN_DESCRIPTION_CHARS = 40
#: ``ValueError: …`` leaking out of a tool means nobody handled the input.
_CRASH_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Signal)"
                       r"(?:\s*\(.*\))?\s*:")
_TIMEOUT_RE = re.compile(r"timed out after", re.I)


@dataclass(frozen=True)
class Issue:
    """One contract complaint: ``level`` is ``"error"`` or ``"warning"``."""

    level: str
    message: str

    def line(self) -> str:
        icon = "❌" if self.level == "error" else "⚠️"
        return f"{icon} {self.message}"


def contract_issues(tool: Tool) -> List[Issue]:
    """Every way this tool's *contract with the model* is broken or thin."""
    issues: List[Issue] = []
    name = str(getattr(tool, "name", "") or "")
    if not TOOL_NAME_RE.match(name):
        issues.append(Issue("error", f"{name!r}: tool names must be snake_case "
                                     "(^[a-z][a-z0-9_]{1,63}$) — providers reject the rest"))
    description = str(getattr(tool, "description", "") or "").strip()
    if not description:
        issues.append(Issue("error", f"{name}: no description — the model cannot "
                                     "know when to call it"))
    elif len(description) < MIN_DESCRIPTION_CHARS:
        issues.append(Issue("warning", f"{name}: description is only {len(description)} "
                                       f"chars (aim for {MIN_DESCRIPTION_CHARS}+, with "
                                       "WHEN to use it)"))
    if not str(getattr(tool, "category", "") or "").strip():
        issues.append(Issue("error", f"{name}: no category — selective binding cannot "
                                     "see it"))

    schema = dict(getattr(tool, "args_schema", None) or {})
    if schema.get("type") != "object":
        issues.append(Issue("error", f"{name}: parameters.type is "
                                     f"{schema.get('type')!r}, not 'object'"))
    properties = schema.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        issues.append(Issue("error", f"{name}: parameters.properties must be an object"))
        properties = {}
    for parameter, spec in properties.items():
        if not isinstance(spec, dict):
            issues.append(Issue("error", f"{name}: parameter {parameter!r} has no schema"))
            continue
        if not spec.get("type"):
            issues.append(Issue("error", f"{name}: parameter {parameter!r} has no type"))
        if not str(spec.get("description") or "").strip():
            issues.append(Issue("warning", f"{name}: parameter {parameter!r} is not "
                                           "documented (the model guesses its meaning)"))
    for required in schema.get("required") or []:
        if required not in properties:
            issues.append(Issue("error", f"{name}: required parameter {required!r} is "
                                         "not in properties — the call can never "
                                         "validate"))
        elif (properties.get(required) or {}).get("default") is not None:
            issues.append(Issue("warning", f"{name}: {required!r} is required *and* has "
                                           "a default — drop one of the two"))
    try:
        json.dumps(schema)
    except (TypeError, ValueError) as exc:
        issues.append(Issue("error", f"{name}: schema is not JSON-serialisable ({exc})"))

    timeout = getattr(tool, "timeout", None)
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        issues.append(Issue("error", f"{name}: timeout must be a positive number, "
                                     f"got {timeout!r}"))
    return issues


def contract_problems(tool: Tool) -> List[str]:
    """The hard errors only (warnings are advice, not failures)."""
    return [issue.message for issue in contract_issues(tool) if issue.level == "error"]


def check_contracts(registry: ToolRegistry) -> Dict[str, List[Issue]]:
    """``{tool_name: issues}`` for every registered tool (empty means clean)."""
    return {tool.name: contract_issues(tool) for tool in registry.get_all_tools()}


# ── probes: tools that can be exercised for real, safely ────────────────────
@dataclass(frozen=True)
class Probe:
    """One tool call that is safe to run anywhere, any time.

    ``args`` may use the ``{workspace}`` placeholder, which
    :func:`prepare_workspace` fills with a scratch directory — that is how a
    probe stays read-only without hard-coding ``/tmp`` paths.

    ``expect`` is either ``"ok"`` (the call must succeed — a pure function with
    no dependencies) or ``"clean"`` (it may fail, but it must fail with a
    readable message rather than an exception: a tool that needs a browser, a
    vault or a clipboard does that legitimately).
    """

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    expect: str = "ok"
    why: str = ""

    def resolved(self, workspace: Path) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in dict(self.args or {}).items():
            if isinstance(value, str):
                out[key] = value.replace("{workspace}", str(workspace))
            else:
                out[key] = value
        return out


#: The curated list. Keep it read-only, offline and side-effect free; anything
#: else belongs in a real test with fixtures, not in a global smoke run.
PROBES: Tuple[Probe, ...] = (
    # ── pure functions: they must work ────────────────────────────────────
    Probe("calculate", {"expression": "2 + 2 * 3"}, "ok", "arithmetic, no I/O"),
    Probe("percentage", {"part": 25, "whole": 200}, "ok", "arithmetic"),
    Probe("json_parse", {"text": '{"a": 1, "b": [2, 3]}'}, "ok", "JSON parsing"),
    Probe("json_query", {"text": '{"a": {"b": 42}}', "path": "a.b"}, "ok", "JSONPath"),
    Probe("text_stats", {"text": "hello jarvis"}, "ok", "text metrics"),
    Probe("get_current_datetime", {}, "ok", "clock"),
    Probe("convert_timezone", {"moment": "2026-10-13 15:00",
                               "from_timezone": "Asia/Kolkata",
                               "to_timezone": "UTC"}, "ok", "timezone maths"),
    Probe("time_since", {"moment": "2026-10-01"}, "ok", "relative time"),
    Probe("add_time", {"moment": "2026-10-13", "amount": 2, "unit": "days"}, "ok",
          "date maths"),
    # ── introspection: they describe JARVIS to the model ──────────────────
    Probe("list_available_tools", {}, "ok", "tool listing"),
    Probe("tool_help", {"tool_name": "calculate"}, "ok", "per-tool help"),
    Probe("system_info", {}, "ok", "environment report"),
    Probe("browser_engines", {}, "ok", "engine availability (no browser started)"),
    Probe("browser_status", {}, "ok", "harness status (no browser started)"),
    Probe("crawl_tools_report", {}, "ok", "reader chain report"),
    Probe("search_providers", {}, "ok", "search chain report"),
    Probe("email_status", {}, "ok", "mail configuration report"),
    Probe("memory_stats", {}, "ok", "memory counters"),
    # ── filesystem against a scratch directory ────────────────────────────
    Probe("list_directory", {"directory": "{workspace}"}, "ok", "lists the scratch dir"),
    Probe("directory_size", {"directory": "{workspace}"}, "ok", "sizes the scratch dir"),
    Probe("file_info", {"path": "{workspace}/sample.txt"}, "ok", "stat one file"),
    Probe("read_file", {"path": "{workspace}/sample.txt"}, "ok", "reads one file"),
    Probe("read_file", {"path": "{workspace}/does-not-exist.txt"}, "clean",
          "a missing file must be a message, not a traceback"),
    Probe("find_files_by_name", {"name_pattern": "*.txt", "root": "{workspace}"}, "ok",
          "filename search"),
    Probe("file_search", {"pattern": "jarvis", "root": "{workspace}"}, "ok",
          "content search"),
    Probe("code_outline", {"path": "{workspace}/sample.py"}, "ok", "python outline"),
    Probe("code_project_summary", {"root": "{workspace}"}, "ok", "project summary"),
    Probe("code_find_symbol", {"symbol": "double", "root": "{workspace}"}, "ok",
          "symbol search"),
    Probe("analyze_python", {"code": "def double(x):\n    return x * 2"}, "ok",
          "syntax check — never executes the code"),
    Probe("which_command", {"name": "python3"}, "clean",
          "PATH lookup: the name may not exist everywhere"),
    # ── memory: reads only, and the workspace keeps it isolated ───────────
    Probe("recall", {"query": "jarvis selftest"}, "ok", "memory recall"),
    Probe("search_memory", {"query": "jarvis", "limit": 3}, "ok", "memory search"),
    Probe("media_history", {"limit": 3}, "ok", "play history listing"),
    Probe("media_favourite", {}, "ok", "favourites listing"),
    # ── dependent on hardware or configuration: must degrade politely ─────
    Probe("image_info", {"path": "{workspace}/does-not-exist.png"}, "clean",
          "needs no display"),
    Probe("clipboard_get", {}, "clean", "needs a clipboard backend"),
    Probe("obsidian_status", {}, "clean", "needs a vault, which may not be configured"),
    Probe("obsidian_list_notes", {"limit": 3}, "clean", "needs a vault"),
    Probe("ocr_report", {}, "clean", "needs tesseract"),
    Probe("list_screen_monitors", {}, "clean", "needs a display"),
)


@dataclass
class ProbeResult:
    """What one probe did: ``status`` is ``ok``, ``clean``, ``failed``, ``crashed``
    or ``missing``."""

    tool: str
    status: str
    elapsed_ms: int = 0
    message: str = ""
    expect: str = "ok"

    @property
    def passed(self) -> bool:
        if self.status == "ok":
            return True
        return self.status == "clean" and self.expect == "clean"

    def line(self) -> str:
        icon = {"ok": "✅", "clean": "💤", "failed": "❌", "crashed": "💥",
                "missing": "❔"}.get(self.status, "•")
        return f"  {icon} {self.tool:<24} {self.elapsed_ms:>5} ms  " \
               f"{truncate(self.message, 90)}"


def prepare_workspace(root: Optional[Path] = None) -> Path:
    """Create (or fill) the scratch directory ``{workspace}`` resolves to."""
    base = Path(root) if root else Path(tempfile.mkdtemp(prefix="jarvis-selftest-"))
    (base / "nested").mkdir(parents=True, exist_ok=True)
    (base / "sample.txt").write_text("hello jarvis\nsecond line\n", encoding="utf-8")
    (base / "nested" / "third.txt").write_text("jarvis again\n", encoding="utf-8")
    (base / "sample.py").write_text("def double(x):\n    return x * 2\n",
                                    encoding="utf-8")
    return base


def _verdict(result: Any, expect: str, elapsed_ms: int) -> ProbeResult:
    tool = str(getattr(result, "tool", "") or "")
    text = (result.content if result.ok else result.error) or ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    if result.ok:
        return ProbeResult(tool, "ok", elapsed_ms, first, expect)
    error = str(result.error or "")
    if _CRASH_RE.match(error.strip()) or _TIMEOUT_RE.search(error):
        return ProbeResult(tool, "crashed", elapsed_ms, error.strip().splitlines()[0],
                           expect)
    return ProbeResult(tool, "clean", elapsed_ms, first, expect)


async def arun_probe(registry: ToolRegistry, probe: Probe, *,
                     workspace: Optional[Path] = None,
                     timeout: float = 30.0) -> ProbeResult:
    """Run one probe and classify the outcome (async — use inside a loop)."""
    tool_obj = registry.get(probe.tool)
    if tool_obj is None:
        return ProbeResult(probe.tool, "missing", 0,
                           "not registered (disabled or renamed?)", probe.expect)
    started = time.perf_counter()
    try:
        result = await registry.execute(probe.tool, probe.resolved(
            workspace or prepare_workspace()), timeout=timeout)
    except Exception as exc:                            # pragma: no cover - defensive
        elapsed = int((time.perf_counter() - started) * 1000)
        return ProbeResult(probe.tool, "crashed", elapsed,
                           f"{type(exc).__name__}: {exc}", probe.expect)
    elapsed = int((time.perf_counter() - started) * 1000)
    return _verdict(result, probe.expect, elapsed)


def run_probe(registry: ToolRegistry, probe: Probe, *,
              workspace: Optional[Path] = None, timeout: float = 30.0) -> ProbeResult:
    """Blocking wrapper around :func:`arun_probe` (for the CLI and scripts)."""
    return asyncio.run(arun_probe(registry, probe, workspace=workspace, timeout=timeout))


# ── the whole report ────────────────────────────────────────────────────────
@dataclass
class SmokeReport:
    """Everything :func:`run_smoke` found, ready to print or assert on."""

    tool_count: int = 0
    categories: Dict[str, int] = field(default_factory=dict)
    issues: Dict[str, List[Issue]] = field(default_factory=dict)
    results: List[ProbeResult] = field(default_factory=list)

    @property
    def errors(self) -> Dict[str, List[Issue]]:
        return {name: [issue for issue in issues if issue.level == "error"]
                for name, issues in self.issues.items()
                if any(issue.level == "error" for issue in issues)}

    @property
    def warnings(self) -> Dict[str, List[Issue]]:
        return {name: [issue for issue in issues if issue.level == "warning"]
                for name, issues in self.issues.items()
                if any(issue.level == "warning" for issue in issues)}

    @property
    def failed_probes(self) -> List[ProbeResult]:
        return [result for result in self.results if not result.passed]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.failed_probes

    def text(self, *, verbose: bool = False) -> str:
        lines = [f"JARVIS tool selftest — {self.tool_count} tools, "
                 f"{len(self.categories)} categories (no model involved)",
                 ""]
        error_count = sum(len(items) for items in self.errors.values())
        warning_count = sum(len(items) for items in self.warnings.values())
        lines.append(f"contract   {error_count} error(s), {warning_count} warning(s)")
        for name, items in sorted(self.errors.items()):
            for issue in items:
                lines.append(f"  {issue.line()}")
        for name, items in sorted(self.warnings.items()):
            for issue in items:
                lines.append(f"  {issue.line()}")
        counts: Dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
        lines.append("")
        lines.append(f"probes     {len(self.results)} run ({summary or 'none'})")
        for result in self.results:
            if verbose or not result.passed:
                lines.append(result.line())
        lines.append("")
        if self.ok:
            lines.append("✅ every tool contract is valid and every probe behaved")
        else:
            lines.append("❌ see the entries above — a broken contract is invisible "
                         "until the model calls it")
        return "\n".join(lines)


def run_smoke(registry: Optional[ToolRegistry] = None, *,
              config: Optional[Mapping[str, Any]] = None,
              workspace: Optional[Path] = None,
              probes: Sequence[Probe] = (),
              timeout: float = 30.0) -> SmokeReport:
    """Contract-check every registered tool and run the safe probes.

    Never contacts a model.  Ollama autostart is switched off for the duration
    because the memory tools would otherwise try to spawn a local daemon just to
    answer ``memory_stats``.
    """
    # A probe must never spawn a local model server as a side effect.
    os.environ.setdefault("JARVIS_OLLAMA_AUTOSTART", "0")
    if registry is None:
        from ..config import load_config

        registry = ToolRegistry()
        registry.load_all_default_tools(config or load_config(), verbose=False)
    report = SmokeReport(tool_count=len(registry),
                         categories=dict(registry.categories()))
    report.issues = check_contracts(registry)

    owns_workspace = workspace is None
    scratch = prepare_workspace(workspace) if workspace else prepare_workspace()
    try:
        report.results = [
            run_probe(registry, probe, workspace=scratch, timeout=timeout)
            for probe in (probes or PROBES)]
    finally:
        if owns_workspace:
            import contextlib
            import shutil

            with contextlib.suppress(Exception):
                shutil.rmtree(scratch, ignore_errors=True)
    return report


def smoke_report(*, verbose: bool = False, **kwargs: Any) -> str:
    """The printable report for ``python -m jarvis tools --smoke``."""
    return run_smoke(**kwargs).text(verbose=verbose)
