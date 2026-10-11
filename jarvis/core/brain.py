"""The Brain — JARVIS's LLM interface.

Sits between the orchestrator and the :class:`~jarvis.core.api_manager.APIManager`
and adds the two things that make a 3B model usable on a 2-core CPU:

* **selective tool binding** — instead of shipping 40 tool schemas (≈3 000
  tokens) on every turn, score the user's message and bind only the 5–9 tools
  that plausibly matter.  The rest stay one message away: the agent can always
  call ``list_available_tools`` when it needs something else.
* **budget-aware prompting** — every prompt goes through the context manager,
  and the manager reports what each section cost.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..persona import build_system_prompt
from ..utils.logger import get_logger
from .api_manager import APIManager, get_api_manager
from .context_manager import ContextManager
from .messages import (
    AIMessage, HumanMessage, Message, SystemMessage, ensure_messages, normalise_role,
    text_of,
)
from .providers import LLMResponse

log = get_logger("brain")

__all__ = ["Brain", "ThinkingTrace", "TOOL_KEYWORDS"]


# Category → trigger words.  Deliberately multi-lingual (Heoster mixes Hindi
# and English) and biased toward the three focus areas.
TOOL_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "browser": ("open", "browse", "browser", "website", "site", "url", "link",
                "click", "navigate", "page", "web", "youtube", "chrome", "tab",
                "login", "form", "scroll", "download", "play", "video",
                "khol", "kholo", "browser me"),
    "screen": ("screen", "screenshot", "cursor", "mouse", "click", "keyboard",
               "type", "desktop", "window", "app", "application", "focus",
               "minimise", "minimize", "maximize", "drag", "drag-and-drop",
               "notepad", "excel", "word", "paint", "file explorer", "volume",
               "brightness", "screen par", "dikhao"),
    "search": ("search", "google", "find", "lookup", "look up", "research",
               "information", "what is", "who is", "when did", "latest", "news",
               "price", "weather", "score", "dhundo", "kya hai", "kaun hai"),
    "crawl": ("read", "article", "documentation", "docs", "blog", "scrape",
              "extract", "summarise this page", "summarize this page", "wikipedia",
              "padho", "summary"),
    "code": ("code", "python", "script", "program", "run", "execute", "function",
             "debug", "error", "traceback", "test", "pytest", "compile",
             "calculate", "compute", "chal", "chalao"),
    "file": ("file", "folder", "directory", "path", "read file", "write",
             "save", "create", "delete", "move", "copy", "rename", "grep",
             "list files", "repo", "project", "csv", "json", "log"),
    "memory": ("remember", "recall", "forget", "memory", "you know about me",
               "my name", "earlier", "last time", "yaad", "preference",
               "what do you know", "who am i"),
    "communication": ("email", "mail", "send", "notify", "notification",
                      "webhook", "message", "telegram", "whatsapp", "sms"),
    "utility": ("time", "date", "today", "calculate", "convert", "json",
                "clipboard", "colour", "color", "image", "download", "timer"),
}

_ALWAYS_ON = ("utility", "memory")


@dataclass
class ThinkingTrace:
    """What happened on one brain call (shown by ``/thinking`` / ``--debug``)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    tools_bound: int = 0
    tools_all: int = 0
    categories: List[str] = field(default_factory=list)
    fallback_from: str = ""
    iterations: int = 1
    context_line: str = ""

    def line(self) -> str:
        bits = [f"{self.provider or '?'}/{self.model or '?'}"]
        if self.latency_ms:
            bits.append(f"{self.latency_ms} ms")
        bits.append(f"tools {self.tools_bound}/{self.tools_all}")
        if self.categories:
            bits.append("+".join(self.categories))
        bits.append(f"tokens {self.prompt_tokens}→{self.completion_tokens}")
        if self.fallback_from:
            bits.append(f"fallback from {self.fallback_from}")
        return " · ".join(bits)


class Brain:
    """LLM front-end: context-managed prompting + selective tool binding."""

    def __init__(self, config: Mapping[str, Any], *,
                 api_manager: Optional[APIManager] = None,
                 context_manager: Optional[ContextManager] = None):
        self.config = dict(config or {})
        self.llm_cfg: Mapping[str, Any] = self.config.get("llm") or {}
        self.api = api_manager or get_api_manager(self.config)
        self.context = context_manager or ContextManager(self.config)
        self.all_tools: List[Any] = []
        self.bound_tools: List[Any] = []
        self.trace = ThinkingTrace()
        self._memory_block: str = ""
        self._knowledge_block: str = ""
        self.model = str(self.llm_cfg.get("model", "hermes3:3b"))
        log.info(f"Brain ready: provider={self.api.llm_cfg.get('provider', 'auto')}, "
                 f"model={self.model}, budget={self.context.max_budget} tokens")

    # ── tool management ────────────────────────────────────────────────────
    def bind_tools(self, tools: Sequence[Any]) -> None:
        self.all_tools = list(tools)
        self.bound_tools = list(tools)
        log.info(f"bound {len(tools)} tools")

    def categories_for(self, text: str) -> List[str]:
        """Rank categories by how strongly the message matches their keywords."""
        lowered = (text or "").lower()
        scored: List[tuple[float, str]] = []
        for category, words in TOOL_KEYWORDS.items():
            score = 0.0
            for word in words:
                if word in lowered:
                    # longer keywords are far more specific ("screenshot" > "app")
                    score += 1.0 + (0.4 * min(len(word), 12) / 12)
            if score:
                scored.append((score, category))
        scored.sort(reverse=True)
        hits = [category for _score, category in scored]
        for category in _ALWAYS_ON:
            if category not in hits:
                hits.append(category)
        return hits

    def select_relevant_tools(self, message: str,
                              tools: Optional[Sequence[Any]] = None,
                              *, max_tools: int = 9) -> List[Any]:
        """Bind 5–9 tools instead of everything (≈2 000 tokens saved per turn).

        Scoring has three parts: the category score from :meth:`categories_for`,
        the overlap between the message and the tool's *name*, and the overlap
        with the tool's *docstring*.  Then the picker takes the best tools while
        guaranteeing every matched category is represented, so "search for X and
        open it" gets both search and browser tools.
        """
        pool = list(tools if tools is not None else self.all_tools)
        if not pool or not self.config.get("context", {}).get("selective_tools", True):
            return pool

        lowered = (message or "").lower()
        ranked_categories = self.categories_for(message)
        category_score = {name: len(ranked_categories) - index
                          for index, name in enumerate(ranked_categories)}

        # math / time phrasing should always wake the utility toolbox
        if re.search(r"\d\s*[+\-*/^%]\s*\d|sqrt\(|\bpercent\b|\d+\s*%", lowered):
            category_score["utility"] = category_score.get("utility", 0) + 4
        if re.search(r"\b(today|tomorrow|yesterday|time|date|kab|samay)\b", lowered):
            category_score["utility"] = category_score.get("utility", 0) + 3
        if re.search(r"https?://|www\.", lowered):
            category_score["browser"] = category_score.get("browser", 0) + 3
            category_score["crawl"] = category_score.get("crawl", 0) + 2

        message_words = {w for w in re.findall(r"[a-z0-9_]+", lowered) if len(w) > 2}

        def score(tool: Any) -> float:
            category = getattr(tool, "category", "utility")
            value = float(category_score.get(category, 0))
            name = str(getattr(tool, "name", ""))
            name_words = set(name.replace("_", " ").split())
            value += 2.0 * len(message_words & name_words)
            if name and name in lowered:
                value += 3.0
            doc = str(getattr(tool, "description", "") or "").lower()
            doc_words = {w for w in re.findall(r"[a-z0-9_]+", doc) if len(w) > 3}
            value += 0.35 * len(message_words & doc_words)
            return value

        scored = sorted(((score(tool), tool) for tool in pool),
                        key=lambda pair: pair[0], reverse=True)

        picked: List[Any] = []
        used_categories: Dict[str, int] = {}

        # pass 1 — guarantee representation for each matched category (top 2 each)
        for _value, tool in scored:
            if len(picked) >= max_tools:
                break
            category = getattr(tool, "category", "utility")
            if category_score.get(category, 0) <= 0:
                continue
            if used_categories.get(category, 0) >= 2:
                continue
            if tool not in picked:
                picked.append(tool)
                used_categories[category] = used_categories.get(category, 0) + 1

        # pass 2 — fill the remaining slots with the highest-scoring tools
        for _value, tool in scored:
            if len(picked) >= max_tools:
                break
            if tool not in picked:
                picked.append(tool)

        # discovery tool is cheap insurance against a wrong guess here
        if len(picked) < max_tools:
            for tool in pool:
                if getattr(tool, "name", "") == "list_available_tools" and tool not in picked:
                    picked.append(tool)
                    break

        if not picked:
            picked = pool[:max_tools]

        self.trace.tools_all = len(pool)
        self.trace.tools_bound = len(picked)
        self.trace.categories = ranked_categories[:4]
        log.debug(f"tool selection: {len(picked)}/{len(pool)} for {ranked_categories[:4]}")
        return picked

    def tool_schema(self) -> List[Dict[str, Any]]:
        return [t.schema() if hasattr(t, "schema") else t for t in self.bound_tools]

    # ── prompting ──────────────────────────────────────────────────────────
    def set_context_blocks(self, *, memory: str = "", knowledge: str = "") -> None:
        self._memory_block = memory
        self._knowledge_block = knowledge

    def system_prompt(self, *, memory: str = "", knowledge: str = "",
                      extra: str = "") -> str:
        blocks = {
            "memory": memory or self._memory_block,
            "knowledge": knowledge or self._knowledge_block,
        }
        return build_system_prompt(memories=blocks["memory"],
                                   knowledge=blocks["knowledge"], extra=extra)

    def build_messages(self, history: Sequence[Any], user_input: str, *,
                       system_extra: str = "", memory: str = "",
                       knowledge: str = "") -> List[Message]:
        messages: List[Message] = [SystemMessage(content=self.system_prompt(
            memory=memory, knowledge=knowledge, extra=system_extra))]
        messages.extend(ensure_messages(history))
        if user_input:
            messages.append(HumanMessage(content=user_input))
        return messages

    # ── inference ──────────────────────────────────────────────────────────
    def think(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
              temperature: Optional[float] = None,
              max_tokens: Optional[int] = None,
              json_mode: bool = False) -> LLMResponse:
        """Synchronous completion through the API manager's fallback chain."""
        fits = self.context.fit(messages, tools=tools)
        self.trace.context_line = self.context.stats.line()
        self.trace.prompt_tokens = self.context.stats.output_tokens
        bound = list(tools) if tools is not None else []
        response = self.api.complete(fits, tools=bound or None,
                                     temperature=temperature,
                                     max_tokens=max_tokens, json_mode=json_mode)
        self.trace.provider = response.provider
        self.trace.model = response.model
        self.trace.latency_ms = response.latency_ms
        self.trace.completion_tokens = response.completion_tokens or \
            self.context.count(response.content)
        self.trace.fallback_from = response.fallback_from
        self.trace.iterations += 1
        return response

    async def athink(self, messages: Sequence[Any], **kwargs: Any) -> LLMResponse:
        fits = self.context.fit(messages, tools=kwargs.get("tools"))
        self.trace.context_line = self.context.stats.line()
        self.trace.prompt_tokens = self.context.stats.output_tokens
        tools = kwargs.pop("tools", None)
        response = await self.api.acomplete(fits, tools=tools, **kwargs)
        self.trace.provider = response.provider
        self.trace.model = response.model
        self.trace.latency_ms = response.latency_ms
        self.trace.completion_tokens = response.completion_tokens or \
            self.context.count(response.content)
        self.trace.fallback_from = response.fallback_from
        self.trace.iterations += 1
        return response

    async def complete(self, messages: Sequence[Any], **kwargs: Any) -> LLMResponse:
        """Plain completion without tool binding (used for summaries/extraction)."""
        return await self.athink(messages, **kwargs)

    def reset_trace(self) -> None:
        iterations = self.trace.iterations
        self.trace = ThinkingTrace(iterations=0)
        self.trace.iterations = iterations

    # ── diagnostics ────────────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "provider_chain": self.api.chain,
            "active_provider": self.api.active_provider(),
            "tools_bound": len(self.bound_tools),
            "tools_total": len(self.all_tools),
            "context": self.context.stats.line(),
        }

    def health_report(self) -> str:
        lines = ["LLM providers (fallback order):"]
        for name, status, latency, note in self.api.summary_table():
            mark = {"healthy": "✓", "degraded": "~", "down": "✗",
                    "unconfigured": "·", "unknown": "?", "exhausted": "!"}.get(status, "?")
            lines.append(f"  {mark} {name:<11} {latency:<8} {note}")
        active = self.api.active_provider()
        lines.append(f"  → serving right now: {active}")
        return "\n".join(lines)
