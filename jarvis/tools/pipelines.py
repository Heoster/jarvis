"""Pipelines: fixed multi-step recipes that the agent runs step by step.

A pipeline is a task (``web_search``) with one or more *strategies*.  A strategy
is an ordered list of tool steps.  The runner tries the strategies in order and
stops at the first one whose steps all succeed.  Each attempt is recorded, so a
failure always says which strategy and which step broke.

Definitions live in ``pipelines/*.json`` so they can be edited without code::

    {"name": "web_search", "description": "...", "inputs": ["query"],
     "strategies": [{"name": "desktop_keyboard", "steps": [
         {"tool": "keyboard_type", "args": {"text": "{query}"}, "wait": 0.5,
          "expect": "query", "note": "type into the focused window"}]}]}

Placeholders: ``{query}`` (raw input), ``{query_url}`` (URL-encoded input),
``{<any input>}``, and ``{last}`` (the previous step's output, truncated).
``expect`` checks the step output: ``"query"`` means some word of the query
must appear in it; any other string is a case-insensitive substring.

Tools are looked up in the registry the orchestrator is using (bound by
:func:`bind_registry`), so the same pipeline drives the same tools the agent sees.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import quote_plus

from ..utils.logger import get_logger
from .base import failure, tool

log = get_logger("tools.pipelines")

__all__ = ["Pipeline", "Strategy", "Step", "PipelineRunner", "PipelineResult",
           "PIPELINE_DIR", "load_pipelines", "bind_registry", "PIPELINE_TOOLS"]

PIPELINE_DIR = Path(__file__).resolve().parents[2] / "pipelines"

_ACTIVE: Dict[str, Any] = {"registry": None}


def bind_registry(registry: Any) -> None:
    """Remember the registry the agent runs with; pipeline steps resolve tools from it."""
    _ACTIVE["registry"] = registry


@dataclass
class Step:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    wait: float = 0.0
    expect: str = ""
    note: str = ""


@dataclass
class Strategy:
    name: str
    steps: List[Step]
    description: str = ""


@dataclass
class Pipeline:
    name: str
    description: str
    inputs: List[str]
    strategies: List[Strategy]


@dataclass
class PipelineResult:
    ok: bool
    pipeline: str
    strategy: str = ""
    output: str = ""
    attempts: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        lines = []
        for attempt in self.attempts:
            status = "ok" if attempt["ok"] else f"failed at step {attempt['failed_step']}"
            lines.append(f"- {attempt['strategy']}: {status}"
                         + (f" ({attempt['reason']})" if attempt.get("reason") else ""))
        return "\n".join(lines)


def _parse(data: Mapping[str, Any]) -> Pipeline:
    strategies = []
    for raw in data.get("strategies") or []:
        steps = [Step(tool=str(s["tool"]), args=dict(s.get("args") or {}),
                      wait=float(s.get("wait", 0) or 0), expect=str(s.get("expect", "")),
                      note=str(s.get("note", "")))
                 for s in raw.get("steps") or []]
        strategies.append(Strategy(name=str(raw.get("name", "strategy")), steps=steps,
                                   description=str(raw.get("description", ""))))
    return Pipeline(name=str(data["name"]), description=str(data.get("description", "")),
                    inputs=list(data.get("inputs") or ["query"]), strategies=strategies)


def load_pipelines(directory: Optional[Path] = None) -> Dict[str, Pipeline]:
    """Every valid pipeline in ``directory`` (default: the repo's ``pipelines/``)."""
    found: Dict[str, Pipeline] = {}
    folder = Path(directory or PIPELINE_DIR)
    for path in sorted(folder.glob("*.json")):
        try:
            pipeline = _parse(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            log.warning(f"cannot load pipeline {path.name}: {exc}")
            continue
        found[pipeline.name] = pipeline
    return found


def _render(text: str, values: Mapping[str, str]) -> str:
    out = text
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _expect_ok(expect: str, output: str, query: str) -> bool:
    if not expect:
        return True
    if expect == "query":
        words = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]
        return not words or any(w in output.lower() for w in words)
    return expect.lower() in output.lower()


class PipelineRunner:
    """Runs a pipeline's strategies in order against the given tool lookup."""

    def __init__(self, pipelines: Mapping[str, Pipeline],
                 lookup: Callable[[str], Any]):
        self.pipelines = dict(pipelines)
        self.lookup = lookup

    async def run(self, name: str, inputs: Mapping[str, str]) -> PipelineResult:
        pipeline = self.pipelines.get(name)
        if pipeline is None:
            return PipelineResult(ok=False, pipeline=name,
                                  output=f"no pipeline named '{name}'. "
                                         f"Known: {', '.join(sorted(self.pipelines)) or 'none'}")
        values = {k: str(v) for k, v in inputs.items()}
        values["query_url"] = quote_plus(values.get("query", ""))
        attempts: List[Dict[str, Any]] = []
        for strategy in pipeline.strategies:
            outcome = await self._run_strategy(pipeline, strategy, dict(values))
            attempts.append(outcome["attempt"])
            if outcome["ok"]:
                return PipelineResult(ok=True, pipeline=name, strategy=strategy.name,
                                      output=outcome["output"], attempts=attempts)
        return PipelineResult(ok=False, pipeline=name, attempts=attempts,
                              output=f"all {len(attempts)} strategies of '{name}' failed")

    async def _run_strategy(self, pipeline: Pipeline, strategy: Strategy,
                            values: Dict[str, str]) -> Dict[str, Any]:
        last = ""
        for index, step in enumerate(strategy.steps, 1):
            if step.wait:
                await asyncio.sleep(step.wait)
            step_values = {**values, "last": last[:2000]}
            args = {k: _render(v, step_values) if isinstance(v, str) else v
                    for k, v in step.args.items()}
            target = self.lookup(step.tool)
            if target is None:
                return self._fail(strategy, index, f"tool {step.tool} is not available")
            result = await target.ainvoke(args)
            output = result.content if result.ok else (result.error or "")
            if not result.ok:
                return self._fail(strategy, index, f"{step.tool}: {output[:160]}")
            if not _expect_ok(step.expect, output, values.get("query", "")):
                return self._fail(strategy, index,
                                  f"{step.tool} output did not show the expected text")
            last = output
            log.info(f"pipeline {pipeline.name}/{strategy.name} step {index} "
                     f"{step.tool} ok")
        return {"ok": True, "output": last,
                "attempt": {"strategy": strategy.name, "ok": True,
                            "failed_step": None, "reason": ""}}

    @staticmethod
    def _fail(strategy: Strategy, index: int, reason: str) -> Dict[str, Any]:
        log.warning(f"strategy {strategy.name} failed at step {index}: {reason}")
        return {"ok": False, "output": "",
                "attempt": {"strategy": strategy.name, "ok": False,
                            "failed_step": index, "reason": reason}}


def _default_runner() -> PipelineRunner:
    registry = _ACTIVE["registry"]
    if registry is None:
        raise RuntimeError("no tool registry is bound for pipelines")

    def lookup(name: str) -> Any:
        return registry.get(name) if hasattr(registry, "get") else None

    return PipelineRunner(load_pipelines(), lookup)


@tool(category="browser", timeout=300)
async def run_pipeline(name: str, query: str = "") -> str:
    """Run a fixed multi-step pipeline, for example a web search with the real cursor and keyboard.

    WHEN: the operator asks to search the web ("search X", "google X", "look up X").
    Use this instead of typing by hand. The pipeline tries its strategies in
    order (keyboard in Chrome, then browser typing, then the search API) and
    reports which one worked.
    HOW: name="web_search", query=the search words only (no "search for").

    Args:
        name: Pipeline name, e.g. "web_search"
        query: The text the pipeline works on (for web_search, the search words)
    """
    try:
        runner = _default_runner()
    except RuntimeError as exc:
        return failure(str(exc))
    result = await runner.run(name, {"query": query})
    if not result.ok:
        return failure(f"pipeline {name} failed.\n{result.summary()}\n{result.output}")
    return (f"✅ pipeline {name} ran via strategy “{result.strategy}”.\n"
            f"{result.summary()}\n\n{result.output}")


PIPELINE_TOOLS = [run_pipeline]
