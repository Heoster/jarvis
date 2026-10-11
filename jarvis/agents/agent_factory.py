"""Agent factory + dispatcher: pick the right specialist for a request.

Two dispatch modes:

* :meth:`AgentFactory.route` — classify a request and run the best specialist;
* :meth:`AgentFactory.dispatch_parallel` — for compound requests ("research X and
  screenshot it"), run several specialists concurrently and merge their findings.

The main orchestrator handles simple turns itself; specialists are for work that
needs a narrower focus or more steps than one turn should spend.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from .base_agent import AgentResult, BaseAgent
from .specialists import AGENT_CLASSES, get_agent

log = get_logger("agents.factory")

__all__ = ["AgentFactory", "RouteDecision", "AGENT_TRIGGERS"]

AGENT_TRIGGERS: Dict[str, Tuple[str, ...]] = {
    "browser": ("open the website", "open chrome", "go to", "log in", "login",
                "click on", "fill the form", "youtube", "browser", "website",
                "web app", "checkout", "sign up", "kholo", "browser me"),
    "screen": ("screen", "screenshot the desktop", "cursor", "mouse", "notepad",
               "excel", "paint", "window", "desktop app", "click at", "type into the app",
               "screen par", "app kholo"),
    "research": ("research", "compare", "find out", "investigate", "sources",
                 "latest news", "market", "who is", "what is the best", "review",
                 "deep dive", "report on"),
    "code": ("code", "python", "script", "debug", "error", "traceback", "function",
             "refactor", "test", "implement", "algorithm", "bug", "compile",
             "terminal command", "shell"),
    "file": ("file", "folder", "directory", "organise", "organize", "rename",
             "clean up", "csv", "log file", "summarise the document"),
    "memory": ("remember", "recall", "what do you know", "memory", "earlier",
               "last time", "my preferences", "yaad"),
    "study": ("obsidian", "my notes", "note down", "make notes", "flashcard",
              "flashcards", "revision", "revise", "study plan", "study for",
              "syllabus", "my vault", "lecture notes", "homework", "assignment",
              "exam on", "board exam", "mock test", "padhai", "likhna hai"),
}


@dataclass
class RouteDecision:
    agent: str
    confidence: float
    reason: str = ""
    secondary: List[str] = field(default_factory=list)

    def line(self) -> str:
        extra = f" (+{', '.join(self.secondary)})" if self.secondary else ""
        return f"{self.agent}{extra} — {self.confidence:.2f} {self.reason}"


class AgentFactory:
    """Creates and dispatches specialists."""

    def __init__(self, config: Mapping[str, Any], *,
                 brain: Any = None, memory: Any = None, registry: Any = None):
        self.config = dict(config or {})
        self.brain = brain
        self.memory = memory
        self.registry = registry
        self._cache: Dict[str, BaseAgent] = {}

    # ── creation ───────────────────────────────────────────────────────────
    def create(self, name: str) -> BaseAgent:
        key = name.lower()
        if key not in self._cache:
            self._cache[key] = get_agent(key, self.config, brain=self.brain,
                                         memory=self.memory, registry=self.registry)
            log.debug(f"created agent '{key}'")
        return self._cache[key]

    def available(self) -> Dict[str, str]:
        return {name: cls.spec.description for name, cls in AGENT_CLASSES.items()}

    # ── routing ────────────────────────────────────────────────────────────
    def route(self, request: str, *, threshold: float = 0.5) -> Optional[RouteDecision]:
        """Classify a request.  Returns ``None`` when the main loop should handle it."""
        lowered = (request or "").lower()
        if not lowered.strip():
            return None

        scores: Dict[str, float] = {}
        for agent, triggers in AGENT_TRIGGERS.items():
            hits = sum(1 for trigger in triggers if trigger in lowered)
            # weight multi-word triggers higher — they are far more specific
            weight = sum(2.0 if " " in trigger else 1.0 for trigger in triggers
                         if trigger in lowered)
            scores[agent] = hits + weight

        # structural hints
        if re.search(r"https?://\S+", lowered) and any(
                word in lowered for word in ("open", "go to", "visit", "click")):
            scores["browser"] = scores.get("browser", 0) + 3
        if re.search(r"\b(?:write|fix|run|debug)\b.*\b(?:code|script|function)\b", lowered):
            scores["code"] = scores.get("code", 0) + 3
        if re.search(r"\b(?:my|mera)\b.*\b(?:prefer|preference|remember)\b", lowered):
            scores["memory"] = scores.get("memory", 0) + 2

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best, score = ranked[0]
        if score < threshold:
            return None
        secondary = [name for name, value in ranked[1:3] if value >= threshold]
        return RouteDecision(agent=best, confidence=min(1.0, score / 6.0),
                             reason=f"matched {int(score)} trigger point(s)",
                             secondary=secondary)

    # ── dispatch ───────────────────────────────────────────────────────────
    async def dispatch(self, request: str, *, context: str = "",
                       agent: Optional[str] = None) -> Optional[AgentResult]:
        decision = RouteDecision(agent=agent, confidence=1.0, reason="explicit") \
            if agent else self.route(request)
        if decision is None:
            return None
        worker = self.create(decision.agent)
        result = await worker.run(request, context=context)
        result.agent = f"{result.agent} ({decision.confidence:.2f})"
        return result

    async def dispatch_parallel(self, request: str, *, agents: Sequence[str],
                                context: str = "") -> List[AgentResult]:
        """Run several specialists concurrently on the same request."""
        tasks = [self.create(name).run(request, context=context) for name in agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: List[AgentResult] = []
        for name, result in zip(agents, results):
            if isinstance(result, Exception):
                out.append(AgentResult(agent=name, task=request, ok=False,
                                       error=str(result)))
            else:
                out.append(result)
        return out

    def dispatch_sync(self, request: str, **kwargs: Any) -> Optional[AgentResult]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.dispatch(request, **kwargs))
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(
                self.dispatch(request, **kwargs))).result()

    # ── reporting ──────────────────────────────────────────────────────────
    def report(self) -> str:
        lines = ["Sub-agents (specialist worker pools):"]
        for name, description in self.available().items():
            spec = AGENT_CLASSES[name].spec
            created = " (loaded)" if name in self._cache else ""
            lines.append(f"  • {name:<9} {truncate(description, 60)}"
                         f" — tools: {spec.tool_hint()[:60]}{created}")
        return "\n".join(lines)

    @staticmethod
    def merge_results(results: Sequence[AgentResult]) -> str:
        """Combine specialist outputs into one answer block."""
        chunks = []
        for result in results:
            status = "✅" if result.ok else "❌"
            chunks.append(f"{status} **{result.agent}** ({result.iterations} steps)\n"
                          f"{result.answer}")
        return "\n\n".join(chunks)
