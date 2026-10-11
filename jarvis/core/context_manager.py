"""Context Manager — how JARVIS stays inside an 8K–16K window.

Budget for the default 16K configuration::

    ┌──────────────────────────────┬─────────┐
    │ system prompt + persona      │  2 000  │
    │ tool definitions             │  2 500  │
    │ recalled memory + RAG        │  3 500  │
    │ conversation history         │  3 500  │
    │ reserved for the reply       │  1 500  │
    ├──────────────────────────────┼─────────┤
    │ total                        │ 14 000  │  (of a 16K window)
    └──────────────────────────────┴─────────┘

The manager does four things, in priority order:

1. **Guarantees** the system message and the newest user turn survive.
2. **Compacts** older turns into a rolling summary instead of dropping them
   silently (extractive by default; LLM-summarised when a model is available).
3. **Shrinks tool results** — web pages and command output are the usual reason
   a small model blows its window, so they are trimmed head+tail, not truncated.
4. **Reports** exactly which budget each section used, for ``/context``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..utils.logger import get_logger
from ..utils.sanitizer import sanitize_text, truncate
from ..utils.token_counter import TokenCounter, estimate_tokens
from .messages import (
    Message, SystemMessage, ensure_message, ensure_messages, normalise_role,
    text_of,
)

log = get_logger("context")

__all__ = ["ContextManager", "ContextStats"]


@dataclass
class ContextStats:
    """Per-section token accounting for one ``fit`` call."""

    input_tokens: int = 0
    output_tokens: int = 0
    budget: int = 0
    dropped_messages: int = 0
    summarised_messages: int = 0
    trimmed_tool_results: int = 0
    sections: Dict[str, int] = field(default_factory=dict)
    compacted: bool = False

    @property
    def usage_percent(self) -> float:
        return round(100.0 * self.output_tokens / max(1, self.budget), 1)

    def line(self) -> str:
        parts = " · ".join(f"{k}:{v}" for k, v in self.sections.items())
        return (f"context {self.input_tokens}→{self.output_tokens} tokens "
                f"({self.usage_percent}% of {self.budget}) [{parts}]")


class ContextManager:
    """Token-budget enforcement + compaction for the message list."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 counter: Optional[TokenCounter] = None):
        cfg = dict(config or {})
        # accept either the whole config or just the `context:` section
        if "context" in cfg and isinstance(cfg["context"], Mapping):
            cfg = dict(cfg["context"])

        self.max_budget = int(cfg.get("max_budget", 14000))
        self.system_budget = int(cfg.get("system_prompt_budget", 2000))
        self.tool_budget = int(cfg.get("tool_definitions_budget", 2500))
        self.memory_budget = int(cfg.get("memory_budget", 3500))
        self.conversation_budget = int(cfg.get("conversation_budget", 3500))
        self.output_budget = int(cfg.get("output_budget", 1500))
        self.auto_compact = bool(cfg.get("auto_compact", True))
        self.compact_threshold = float(cfg.get("compact_threshold", 0.85))
        self.max_tool_result_chars = int(cfg.get("max_tool_result_chars", 4000))

        self.counter = counter or TokenCounter()
        # chars-per-token ratio of the active counter, used for quick trims
        self.chars_per_token = float(getattr(self.counter, "chars_per_token", 4.0) or 4.0)
        self.stats = ContextStats(budget=self.max_budget)
        self._rolling_summary: str = ""

    # ── counting ───────────────────────────────────────────────────────────
    def count(self, text: str) -> int:
        return self.counter.count(text)

    def count_messages(self, messages: Sequence[Any]) -> int:
        return self.counter.count_messages(messages)

    def count_tools(self, tools: Optional[Sequence[Any]]) -> int:
        if not tools:
            return 0
        total = 0
        for tool in tools:
            name = getattr(tool, "name", "")
            desc = getattr(tool, "description", "") or ""
            schema = getattr(tool, "args_schema", None)
            extra = 0
            if isinstance(schema, Mapping):
                extra = len(str(schema)) // 4
            total += self.count(f"{name}{desc}") + 8 + extra
        return total

    # ── fitting ────────────────────────────────────────────────────────────
    def fit(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
            extra_reserve: int = 0) -> List[Message]:
        """Return a message list guaranteed to fit the budget."""
        msgs = ensure_messages(messages)
        tool_tokens = min(self.count_tools(tools), self.tool_budget)
        reserve = self.output_budget + extra_reserve + tool_tokens
        budget = max(512, self.min_budget() - reserve)

        stats = ContextStats(budget=self.max_budget)
        stats.sections["tools"] = tool_tokens

        if not msgs:
            self.stats = stats
            return []

        # 1. system messages (merged + hard capped)
        systems = [m for m in msgs if normalise_role(m.role) == "system"]
        others = [m for m in msgs if normalise_role(m.role) != "system"]
        system = self._merge_systems(systems, cap=self.system_budget)
        stats.sections["system"] = self.count(system.content)

        # 2. the newest user turn always survives
        newest: Optional[Message] = None
        for msg in reversed(others):
            if normalise_role(msg.role) == "human":
                newest = msg
                break
        if newest is None and others:
            newest = others[-1]

        history = [m for m in others if m is not newest]
        history = self._compact_tool_results(history, stats)

        remaining = budget - stats.sections["system"] - (
            self.count_message(newest) if newest else 0)

        # 3. keep as much recent history as fits (newest first)
        kept: List[Message] = []
        dropped: List[Message] = []
        for msg in reversed(history):
            cost = self.count_message(msg)
            if cost <= remaining:
                kept.insert(0, msg)
                remaining -= cost
            else:
                dropped.insert(0, msg)

        if dropped:
            stats.dropped_messages = len(dropped)
            if newest is not None:
                dropped.append(newest)
            if self.auto_compact:
                summary_text = self.summarise(dropped)
                if summary_text:
                    summary = SystemMessage(content=f"[Earlier conversation summary]\n{summary_text}")
                    cost = self.count_message(summary)
                    if cost > remaining and kept:
                        # make room for the summary by dropping the oldest kept turn
                        while kept and cost > remaining:
                            oldest = kept.pop(0)
                            remaining += self.count_message(oldest)
                    if cost <= remaining or not kept:
                        kept.insert(0, summary)
                        stats.summarised_messages = len(dropped)
                        stats.compacted = True

        result: List[Message] = [system] + kept
        if newest is not None:
            result.append(newest)

        # 4. hard clamp — a prompt must never be bigger than the model's window,
        #    even if that means cutting the system block or the newest message.
        result = self._hard_clamp(result, budget)

        stats.input_tokens = self.count_messages(msgs)
        stats.output_tokens = self.count_messages(result)
        stats.sections["conversation"] = max(
            0, stats.output_tokens - stats.sections["system"])
        self.stats = stats

        if stats.output_tokens > self.max_budget:
            log.warning(f"still over budget after compaction: {stats.line()}")
        elif stats.compacted:
            log.info(f"context compacted — {stats.line()}")
        else:
            log.debug(stats.line())
        return result

    def _hard_clamp(self, messages: List[Message], budget: int) -> List[Message]:
        """Trim the tail of messages until the prompt fits the budget.

        The system prompt keeps priority (it holds the persona, the memory block
        and the rules), but it is still bounded to its own share so a runaway
        memory block cannot starve the conversation.
        """
        if not messages:
            return messages
        total = self.count_messages(messages)
        if total <= budget:
            return messages

        system_share = min(self.system_budget, max(600, int(budget * 0.55)))
        clamped: List[Message] = []
        for message in messages:
            role = normalise_role(message.role)
            if role == "system":
                text = message.content or ""
                allowed = system_share * self.chars_per_token
                if len(text) > allowed:
                    head = int(allowed * 0.72)
                    tail = int(allowed * 0.2)
                    text = (text[:head].rstrip()
                            + "\n…[system prompt trimmed to fit the context]…\n"
                            + text[-tail:].lstrip())
                    message = type(message)(content=text)
            clamped.append(message)

        # drop oldest conversation turns until it fits (never the system message)
        while len(clamped) > 1 and self.count_messages(clamped) > budget:
            drop_index = 1
            clamped.pop(drop_index)

        # last resort: trim the remaining non-system message(s)
        if len(clamped) > 1 and self.count_messages(clamped) > budget:
            budget_left = max(200, budget - self.count_message(clamped[0]))
            last = clamped[-1]
            text = last.content or ""
            allowed = budget_left * self.chars_per_token
            if len(text) > allowed:
                clamped[-1] = type(last)(content=text[:allowed].rstrip()
                                         + "\n…[trimmed]…")
        return clamped

    def min_budget(self) -> int:
        return self.max_budget

    def count_message(self, message: Any) -> int:
        return self.counter.count_message(message)

    def _merge_systems(self, systems: Sequence[Any], cap: int) -> Message:
        text = "\n\n".join(text_of(m) for m in systems if text_of(m).strip())
        if not text:
            text = "You are JARVIS."
        if self.count(text) > cap:
            text = self.counter.head_tail(text, cap, head_ratio=0.6)
        return SystemMessage(content=text)

    def _compact_tool_results(self, messages: Sequence[Any],
                              stats: ContextStats) -> List[Message]:
        """Trim giant tool outputs *before* they cost us the whole window."""
        out: List[Message] = []
        per_result_cap = max(
            300, int(self.conversation_budget / max(2, len(messages) or 2)))
        cap = min(self.max_tool_result_chars, per_result_cap * 2)
        for raw in messages:
            msg = ensure_message(raw)
            if normalise_role(msg.role) == "tool" and self.count(msg.content) > 300:
                trimmed = self.shrink_tool_result(msg.content, cap)
                if trimmed != msg.content:
                    stats.trimmed_tool_results += 1
                    msg = Message(content=trimmed, role=msg.role, name=msg.name,
                                  tool_call_id=msg.tool_call_id)
            out.append(msg)
        return out

    def shrink_tool_result(self, text: str, max_chars: Optional[int] = None) -> str:
        """Head+tail trim with sanitiser pass (web text is untrusted)."""
        limit = max_chars or self.max_tool_result_chars
        cleaned = sanitize_text(text, collapse_blank_lines=True)
        if len(cleaned) <= limit:
            return cleaned
        head = int(limit * 0.68)
        tail = limit - head - 60
        return (f"{cleaned[:head]}\n\n[…{len(cleaned) - limit} chars trimmed by "
                f"context manager…]\n\n{cleaned[-max(0, tail):]}")

    # ── summarisation ──────────────────────────────────────────────────────
    def summarise(self, messages: Sequence[Any], *, max_chars: int = 1200) -> str:
        """Extractive summary — fast, offline, deterministic.

        Keeps: the operator's requests verbatim (they are the most informative
        few words in a transcript), tool names used, and file/URL targets.
        """
        if not messages:
            return self._rolling_summary

        points: List[str] = []
        for raw in messages:
            msg = ensure_message(raw)
            role = normalise_role(msg.role)
            content = (msg.content or "").strip()
            if not content and not msg.tool_calls:
                continue
            if role == "human":
                points.append(f"• Heoster asked: {truncate(_one_line(content), 160)}")
            elif role == "tool":
                name = msg.name or "tool"
                points.append(f"• {name} → {truncate(_one_line(content), 110)}")
            elif role == "ai":
                if msg.tool_calls:
                    names = ", ".join(c.get("name", "?") for c in msg.tool_calls)
                    points.append(f"• JARVIS called: {names}")
                elif content:
                    points.append(f"• JARVIS said: {truncate(_one_line(content), 140)}")

        # keep the most recent points, most relevant first
        tail = points[-18:]
        body = "\n".join(tail)
        if self._rolling_summary:
            body = f"{self._rolling_summary}\n{body}"
        body = truncate(body, max_chars)
        self._rolling_summary = body[-max_chars:]
        return body

    async def summarise_with_llm(self, messages: Sequence[Any], brain: Any, *,
                                 max_tokens: int = 260) -> str:
        """Optional upgrade: ask a (cheap, local) model for a real summary."""
        text = self.summarise(messages, max_chars=6000)
        prompt = (
            "Compress this agent transcript into durable notes for future turns. "
            "Keep: what the operator wanted, what was tried, what worked, exact "
            "names/paths/URLs, and any preferences revealed. Drop pleasantries. "
            "Max 12 short lines.\n\n" + text
        )
        try:
            response = await brain.complete(
                [SystemMessage(content="You compress transcripts tersely."),
                 Message(content=prompt, role="human")],
                max_tokens=max_tokens, temperature=0.1)
            summary = (response.content or "").strip()
            if summary:
                self._rolling_summary = summary
                return summary
        except Exception as exc:
            log.debug(f"LLM summarisation failed, kept extractive summary: {exc}")
        return text

    def reset(self) -> None:
        self._rolling_summary = ""
        self.stats = ContextStats(budget=self.max_budget)

    # ── reporting ──────────────────────────────────────────────────────────
    def budget_report(self) -> str:
        s = self.stats
        rows = [
            ("system prompt", self.system_budget, s.sections.get("system", 0)),
            ("tool schemas", self.tool_budget, s.sections.get("tools", 0)),
            ("memory + RAG", self.memory_budget, s.sections.get("memory", 0)),
            ("conversation", self.conversation_budget, s.sections.get("conversation", 0)),
            ("output reserve", self.output_budget, 0),
        ]
        lines = []
        for name, cap, used in rows:
            pct = min(100.0, 100.0 * used / max(1, cap))
            bar = "█" * int(pct / 6.25) + "░" * (16 - int(pct / 6.25))
            lines.append(f"{name:<14} [{bar}] {used:>5}/{cap:<5} tokens")
        lines.append(f"{'TOTAL':<14} {s.output_tokens}/{self.max_budget} tokens "
                     f"({s.usage_percent}% used, {s.dropped_messages} dropped, "
                     f"{s.summarised_messages} summarised)")
        return "\n".join(lines)


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
