"""Base sub-agent — a specialised, tool-restricted worker.

Sub-agents exist because a 3B model does better with *fewer* choices and a
sharper prompt.  Each one gets:

* its own system prompt (role + rules + output shape),
* a restricted tool whitelist (by category and/or name),
* the same memory/brain machinery as the main loop, but a smaller iteration cap.

The orchestrator can dispatch one, or fan several out in parallel for compound
requests ("research X and then open it in the browser").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..core.brain import Brain
from ..core.messages import (
    HumanMessage, Message, SystemMessage, ToolMessage, ensure_messages, normalise_role,
)
from ..memory.memory_manager import MemoryManager, get_memory_manager
from ..tools.registry import ToolRegistry
from ..utils.logger import get_logger
from ..utils.sanitizer import truncate

log = get_logger("agents")

__all__ = ["BaseAgent", "AgentResult", "AgentSpec"]


@dataclass
class AgentSpec:
    """Declarative description of a specialist."""

    name: str
    prompt: str
    categories: Sequence[str] = ("utility",)
    tools: Sequence[str] = ()
    max_iterations: int = 6
    max_tool_calls: int = 10
    description: str = ""

    def tool_hint(self) -> str:
        return ", ".join(self.tools) if self.tools else ", ".join(self.categories)


@dataclass
class AgentResult:
    agent: str = ""
    task: str = ""
    answer: str = ""
    ok: bool = True
    iterations: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    provider: str = ""
    error: str = ""

    def summary(self) -> str:
        return (f"[{self.agent}] {'ok' if self.ok else 'failed'} · "
                f"{self.iterations} step(s) · {len(self.tool_calls)} tool call(s) · "
                f"{self.latency_ms} ms")


class BaseAgent:
    """A specialist that runs its own compact ReAct loop."""

    spec = AgentSpec(name="base", prompt="You are a helpful sub-agent.")

    def __init__(self, config: Mapping[str, Any], *,
                 brain: Optional[Brain] = None,
                 memory: Optional[MemoryManager] = None,
                 registry: Optional[ToolRegistry] = None):
        self.config = dict(config or {})
        self.brain = brain or Brain(self.config)
        self.memory = memory or get_memory_manager(self.config)
        self.registry = (registry or getattr(self.brain, "tool_registry", None)
                        or ToolRegistry())
        if not self.registry.get_all_tools():
            self.registry.load_all_default_tools(self.config, verbose=False)

    # tools the agent is allowed to use ---------------------------------------
    def tools(self) -> List[Any]:
        spec = self.spec
        selected: List[Any] = []
        for tool in self.registry.get_all_tools():
            if spec.tools and tool.name in spec.tools:
                selected.append(tool)
            elif not spec.tools and getattr(tool, "category", "utility") in spec.categories:
                selected.append(tool)
        if not selected:  # never hand an agent an empty toolbox
            selected = self.registry.get_by_category("utility")
        return selected

    def system_prompt(self, task: str) -> str:
        return (f"{self.spec.prompt}\n\n"
                f"You are a sub-agent of JARVIS working for Heoster (Harsh Saini). "
                f"Available tools: {self.spec.tool_hint()}.\n"
                f"Work in at most {self.spec.max_iterations} steps, then report. "
                f"Be concise and factual; never invent tool output.")

    # execution ---------------------------------------------------------------
    async def run(self, task: str, *, context: str = "") -> AgentResult:
        started = time.perf_counter()
        result = AgentResult(agent=self.spec.name, task=task)
        messages: List[Message] = [SystemMessage(content=self.system_prompt(task))]
        if context:
            messages.append(SystemMessage(content=f"Context from JARVIS:\n{context}"))
        messages.append(HumanMessage(content=task))

        tools = self.tools()
        log.info(f"agent {self.spec.name} starting: {truncate(task, 80)} "
                 f"({len(tools)} tools)")
        final = ""

        for iteration in range(1, self.spec.max_iterations + 1):
            result.iterations = iteration
            try:
                response = await self.brain.athink(messages, tools=tools)
            except Exception as exc:
                result.ok = False
                result.error = str(exc)
                log.warning(f"agent {self.spec.name} LLM failure: {exc}")
                break

            result.provider = response.provider
            if response.content:
                final = response.content
            messages.append(response.as_message())

            if not response.has_tool_calls:
                break

            calls = [call for call in response.tool_calls
                     if call.get("name") in self.registry][: self.spec.max_tool_calls]
            if not calls:
                break
            tool_results = await self.registry.execute_many(calls, parallel=True)
            for call, tool_result in zip(calls, tool_results):
                result.tool_calls.append({"name": call["name"],
                                          "args": call.get("args") or {},
                                          "ok": tool_result.ok})
                body = tool_result.content if tool_result.ok else f"Error: {tool_result.error}"
                messages.append(ToolMessage(
                    content=self.brain.context.shrink_tool_result(body),
                    tool_call_id=call.get("id", ""), name=call["name"]))

        result.answer = final or "(sub-agent produced no answer)"
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            self.memory.long.store(
                f"[{self.spec.name} agent] {truncate(task, 300)} → "
                f"{truncate(result.answer, 400)}",
                importance=0.45, tags=["agent", self.spec.name], source="agent",
                dedupe_threshold=0.95)
        except Exception:
            pass
        log.info(result.summary())
        return result

    def run_sync(self, task: str, **kwargs: Any) -> AgentResult:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(task, **kwargs))
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self.run(task, **kwargs))).result()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.spec.name}>"
