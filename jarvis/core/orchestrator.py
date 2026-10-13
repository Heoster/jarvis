"""The Orchestrator — plan, act, observe, answer.

Pipeline for one user turn:

::

    user message
      ├─ 1. RECALL      memory manager → budgeted memory/knowledge/skill blocks
      ├─ 2. BIND        brain selects 5–9 relevant tools (not all 100)
      ├─ 3. REASON      LLM call through the API manager's fallback chain
      ├─ 4. ACT         tool calls executed in parallel where independent
      ├─ 5. OBSERVE     results trimmed by the context manager, loop back to 3
      └─ 6. REMEMBER    importance scoring → episodic/semantic, profile, skills

Safety valves: an iteration cap, a wall-clock budget, and a "tool budget" so a
looping 3B model cannot spin forever on a 2-core CPU.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Mapping, Optional, Sequence, Tuple

from ..memory.memory_manager import MemoryManager, get_memory_manager
from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from ..tools.registry import ToolRegistry
from .brain import Brain
from .providers import LLMResponse
from .harness import ApprovalRequest, FailureBreaker, Harness, verify_result
from .messages import (
    AIMessage, HumanMessage, Message, SystemMessage, ToolMessage, ensure_messages,
    normalise_role, text_of,
)

log = get_logger("orchestrator")

__all__ = ["Orchestrator", "TurnEvent", "TurnResult"]


@dataclass
class TurnEvent:
    """One thing that happened during a turn (streamed to the UI)."""

    type: str                    # thinking | text | tool_call | tool_result | notice | done | error
    text: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "text": self.text, **self.data}


@dataclass
class TurnResult:
    answer: str = ""
    iterations: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    fallbacks: List[str] = field(default_factory=list)
    memory: Dict[str, Any] = field(default_factory=dict)
    context_line: str = ""
    error: str = ""

    def summary(self) -> str:
        parts = [f"{self.provider}/{self.model}" if self.provider else "unknown",
                 f"{self.iterations} iteration(s)",
                 f"{len(self.tool_calls)} tool call(s)",
                 f"{self.latency_ms} ms",
                 f"tokens {self.prompt_tokens}→{self.completion_tokens}"]
        if self.fallbacks:
            parts.append(f"fallbacks: {', '.join(self.fallbacks)}")
        return " · ".join(parts)


class Orchestrator:
    """The agent loop, wired to memory, tools, the brain and quota control."""

    def __init__(self, config: Mapping[str, Any], *,
                 brain: Optional[Brain] = None,
                 memory: Optional[MemoryManager] = None,
                 registry: Optional[ToolRegistry] = None):
        self.config = dict(config or {})
        self.ctx_cfg = dict(self.config.get("context") or {})
        self.max_iterations = int(self.config.get("agent", {}).get(
            "max_iterations", 12)) if isinstance(self.config.get("agent"), Mapping) else 12
        self.max_tool_calls_per_turn = int(
            self.config.get("agent", {}).get("max_tool_calls", 24)
            if isinstance(self.config.get("agent"), Mapping) else 24)
        self.turn_timeout = float(
            self.config.get("agent", {}).get("turn_timeout", 300)
            if isinstance(self.config.get("agent"), Mapping) else 300)
        self.parallel_tools = bool(
            self.config.get("agent", {}).get("parallel_tools", True)
            if isinstance(self.config.get("agent"), Mapping) else True)
        self.use_skills = bool(
            self.config.get("agent", {}).get("use_skills", True)
            if isinstance(self.config.get("agent"), Mapping) else True)

        self.registry = registry or ToolRegistry()
        from ..tools.pipelines import bind_registry as _bind_pipelines

        _bind_pipelines(self.registry)
        self.harness = Harness(self.config)
        self.brain = brain or Brain(self.config)
        self.memory = memory or get_memory_manager(self.config)
        self.session_started = False
        self.last_result: Optional[TurnResult] = None
        self.turn_count = 0

    def _session_context(self) -> str:
        """Live facts the model should know this turn (session, tools, memory)."""
        bits: List[str] = []
        try:
            session = getattr(self.memory.sessions, "current_session_id", "")
            if session:
                bits.append(f"- Session: {session} (turn {self.turn_count + 1})")
            stats = self.memory.stats()
            tiers = stats.get("tiers") or {}
            bits.append("- Memory now: " + ", ".join(f"{name} {count}"
                                                     for name, count in tiers.items())
                         + f" · {stats.get('sessions', {}).get('sessions', 0)} stored "
                           f"transcript(s)")
            recent = stats.get("recent_turns") or []
            if recent:
                bits.append("- Recent turns: " + " | ".join(
                    str(item)[:80] for item in recent[-3:]))
        except Exception as exc:                            # never break a turn
            log.debug(f"session context unavailable: {exc}")
        try:
            bits.append(f"- Tools bound: {len(self.brain.bound_tools)} of "
                        f"{len(self.registry.get_all_tools())}")
        except Exception:
            pass
        bits.append(f"- Now: {datetime.now().strftime('%Y-%m-%d %H:%M')} local "
                    f"(Asia/Kolkata) · server time {datetime.now().strftime('%H:%M')}")
        return "\n".join(bits)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def initialize(self, *, ingest_knowledge: bool = True,
                   resume_session: bool = True, verbose: bool = False) -> Dict[str, Any]:
        """Load tools, bind them, warm up memory and probe providers."""
        if not self.registry.get_all_tools():
            self.registry.load_all_default_tools(self.config, verbose=verbose)
        # start `ollama serve` in the background if it is not running (non-blocking)
        from .ollama_runtime import start_in_background

        start_in_background(self.config)
        self.brain.bind_tools(self.registry.get_all_tools())
        self.brain.attach_registry(self.registry)
        report = self.memory.bootstrap(ingest_knowledge=ingest_knowledge,
                                       resume_session=resume_session)
        self.session_started = True
        self.brain.set_session_context(self._session_context())
        try:
            self.brain.api.start_auto_heal()
        except Exception:
            pass
        report["tools"] = len(self.registry.get_all_tools())
        report["categories"] = self.registry.categories()
        log.info(f"orchestrator ready: {report['tools']} tools, "
                 f"memory {self.memory.long.count()} records")
        return report

    def shutdown(self) -> Dict[str, Any]:
        try:
            report = self.memory.end_session()
        except Exception as exc:
            report = {"error": str(exc)}
        try:
            self.brain.api.close()
        except Exception:
            pass
        return report

    # ── main loop ──────────────────────────────────────────────────────────
    async def process(self, user_input: str, *,
                      history: Optional[Sequence[Any]] = None,
                      stream: bool = True) -> AsyncGenerator[TurnEvent, None]:
        """Run one full turn, yielding events as they happen."""
        started = time.perf_counter()
        result = TurnResult()
        self.turn_count += 1

        # 1. recall ---------------------------------------------------------
        recall_blocks = self.memory.context_blocks(user_input)
        recall = recall_blocks["recall"]
        if not recall.is_empty():
            yield TurnEvent("notice",
                            f"🧠 memory: {recall.summary()}",
                            {"kind": "memory", "tokens": recall.tokens_used})
        memory_block = recall_blocks["memory"]
        knowledge_block = recall_blocks["knowledge"]
        system_extra = recall_blocks["extra"]

        # 2. history + messages --------------------------------------------
        messages: List[Message] = [SystemMessage(content=self.brain.system_prompt(
            memory=memory_block, knowledge=knowledge_block, extra=system_extra))]
        if history is None:
            history = self.memory.short.get_recent(include_summary=True)
        else:
            history = [m for m in ensure_messages(history)
                       if normalise_role(m.role) != "system"]
        messages.extend(ensure_messages(history))
        messages.append(HumanMessage(content=user_input))

        # 3. tool selection -------------------------------------------------
        available_tools = self.registry.get_all_tools()
        bound_tools = self.brain.select_relevant_tools(user_input, available_tools)
        if self.use_skills:
            hints = self.memory.skills.hints_for(user_input)
            if hints:
                messages[0].content = f"{messages[0].content}\n\n{hints}"
        yield TurnEvent("notice",
                        f"🔧 tools bound: {len(bound_tools)}/{len(available_tools)} "
                        f"({', '.join(sorted({getattr(t, 'category', '?') for t in bound_tools}))})",
                        {"kind": "tools", "bound": [t.name for t in bound_tools]})

        tool_call_log: List[Dict[str, Any]] = []
        seen_calls: Dict[str, int] = {}
        breaker = FailureBreaker(self.harness.max_failures)
        decided: Dict[int, Any] = {}
        deadline = time.monotonic() + self.turn_timeout
        final_text = ""
        action_request = needs_action(user_input) and bool(bound_tools)
        route = direct_route(user_input)
        direct_answer = False
        nudged = False

        for iteration in range(1, self.max_iterations + 1):
            result.iterations = iteration
            if time.monotonic() > deadline:
                yield TurnEvent("error", "⏰ turn time budget exceeded — stopping")
                result.error = "timeout"
                break

            yield TurnEvent("thinking", f"… reasoning (step {iteration})",
                            {"iteration": iteration})
            direct_now = iteration == 1 and route is not None and route[0] in self.registry
            try:
                if direct_now:
                    response = LLMResponse(
                        content="", tool_calls=[{"id": "route-1", "name": route[0],
                                                 "args": dict(route[1])}],
                        provider="router", model="direct-route")
                    direct_answer = True
                    yield TurnEvent("notice", f"➜ routed to {route[0]} (no model step needed)",
                                    {"kind": "route", "tool": route[0]})
                else:
                    response = await self.brain.athink(messages, tools=bound_tools)
            except Exception as exc:
                # the API manager already tried every provider
                log.error(f"LLM call failed: {exc}")
                yield TurnEvent("error", f"❌ no model could answer: {exc}")
                result.error = str(exc)
                break

            result.provider = response.provider
            result.model = response.model
            result.prompt_tokens += getattr(self.brain.context.stats, "output_tokens", 0)
            result.completion_tokens += response.completion_tokens
            result.fallbacks = [event.from_provider for event in
                                self.brain.api.events[-3:]]
            result.context_line = self.brain.trace.context_line
            if response.fallback_from:
                why = self._fallback_reason(response.fallback_from)
                yield TurnEvent(
                    "notice",
                    f"🔀 {response.fallback_from} failed{why} → answered with "
                    f"{response.provider} (automatic fallback)",
                    {"kind": "fallback", "from": response.fallback_from,
                     "to": response.provider})

            # Asked once and still no tool call on a play request: run play_song ourselves.
            if (nudged and action_request and not response.has_tool_calls
                    and not tool_call_log and "play_song" in self.registry):
                query = forced_play_query(user_input)
                if query:
                    response.tool_calls = [{"id": "direct-1", "name": "play_song",
                                            "args": {"query": query}}]
                    yield TurnEvent("notice", "↻ no tool call from the model — running "
                                    f"play_song for “{query}” directly",
                                    {"kind": "forced", "tool": "play_song"})

            # An action request answered in plain text: ask once more before showing it.
            if (action_request and not nudged and not response.has_tool_calls
                    and not tool_call_log):
                nudged = True
                messages.append(response.as_message())
                messages.append(HumanMessage(content=_NUDGE))
                yield TurnEvent("notice", "↻ no tool was called — asking the model to act",
                                {"kind": "nudge"})
                continue

            if response.content:
                final_text = response.content
                if not response.has_tool_calls:
                    yield TurnEvent("text", response.content)

            messages.append(response.as_message())

            if not response.has_tool_calls:
                break

            # 4. act ---------------------------------------------------------
            calls: List[Dict[str, Any]] = []
            for call in response.tool_calls:
                name = call.get("name", "")
                if not name or name not in self.registry:
                    suggestion = self._closest_tool(name)
                    yield TurnEvent("error",
                                    f"⚠ unknown tool '{name}'"
                                    + (f" — did you mean '{suggestion}'?" if suggestion else ""))
                    messages.append(ToolMessage(
                        content=f"Error: unknown tool '{name}'. "
                                f"{('Try ' + suggestion + ' instead.') if suggestion else ''}"
                                f" Use list_available_tools to see valid names.",
                        tool_call_id=call.get("id", ""), name=name or "unknown"))
                    continue
                signature = f"{name}:{json.dumps(call.get('args') or {}, sort_keys=True)}"
                seen_calls[signature] = seen_calls.get(signature, 0) + 1
                # also track by tool name alone so we catch variations with different args
                name_count = seen_calls.get(name, 0) + 1
                seen_calls[name] = name_count
                # If this tool already succeeded this turn, don't call it again.
                # Exception: search/crawl/browser/screen tools are legitimately
                # called multiple times (read → verify, search → refine).
                _re_callable = {"search", "crawl", "browser", "screen", "file"}
                tool_obj = self.registry.get(name)
                tool_category = getattr(tool_obj, "category", "") if tool_obj else ""
                already_succeeded = (name in successful_tools
                                     and tool_category not in _re_callable)
                if already_succeeded or seen_calls[signature] > 2 or name_count > 3:
                    messages.append(ToolMessage(
                        content=f"You already have the result for '{name}'. "
                                f"Stop calling tools and write your final answer now.",
                        tool_call_id=call.get("id", ""), name=name))
                    continue
                if len(tool_call_log) >= self.max_tool_calls_per_turn:
                    messages.append(ToolMessage(
                        content="Error: tool-call budget for this turn is used up. "
                                "Answer with what you have.",
                        tool_call_id=call.get("id", ""), name=name))
                    continue
                if breaker.blocked(name):
                    messages.append(ToolMessage(content=breaker.message(name),
                                                tool_call_id=call.get("id", ""), name=name))
                    yield TurnEvent("notice", f"⛔ {name} blocked: it failed "
                                    f"{breaker.failures.get(name, 0)} times this turn",
                                    {"kind": "breaker", "tool": name})
                    continue
                tool_obj = self.registry.get(name)
                decision = self.harness.decide(
                    name, call.get("args") or {},
                    category=getattr(tool_obj, "category", ""),
                    dangerous=bool(getattr(tool_obj, "dangerous", False)))
                if decision.action == "approve":
                    request = ApprovalRequest(name, decision.risk, dict(call.get("args") or {}),
                                              decision.reason)
                    yield TurnEvent("notice", f"🔐 approval needed: {name} ({decision.risk})",
                                    {"kind": "approval", "tool": name, "risk": decision.risk})
                    approved = await asyncio.to_thread(self.harness.ask, request)
                    self.harness.audit.record({"event": "approval", "tool": name,
                                               "risk": decision.risk, "approved": approved,
                                               "args": call.get("args") or {}})
                    if not approved:
                        decision.action = "deny"
                        decision.reason = "the operator did not approve"
                if decision.action == "deny":
                    yield TurnEvent("error", f"⛔ {name} not run: {decision.reason}")
                    self.harness.audit.record({"event": "denied", "tool": name,
                                               "risk": decision.risk, "reason": decision.reason})
                    messages.append(ToolMessage(
                        content=f"Denied: {name} was not run ({decision.reason}). "
                                "Do not retry it; tell the operator what is needed.",
                        tool_call_id=call.get("id", ""), name=name))
                    continue
                decided[id(call)] = decision
                calls.append(call)

            if not calls:
                # All proposed calls were deduplicated — the model has the answers.
                # Inject a final instruction and break to the summary step.
                if tool_call_log:
                    messages.append(HumanMessage(
                        content="You have all the tool results you need. "
                                "Write your final answer now. Do not call any more tools."
                    ))
                continue

            for call in calls:
                yield TurnEvent("tool_call",
                                f"🔧 {call['name']}({_format_args(call.get('args'))})",
                                {"tool": call["name"], "args": call.get("args") or {}})

            parallel = self.parallel_tools and len(calls) > 1 and \
                all(not self._is_stateful(call["name"]) for call in calls)
            tool_results = await self.registry.execute_many(calls, parallel=parallel)

            for call, tool_result in zip(calls, tool_results):
                tool_call_log.append({"name": call["name"],
                                      "args": call.get("args") or {},
                                      "ok": tool_result.ok})
                if tool_result.ok:
                    successful_tools.add(call["name"])
                preview = truncate(tool_result.content or tool_result.error, 400)
                yield TurnEvent(
                    "tool_result",
                    ("✅ " if tool_result.ok else "❌ ") +
                    f"{call['name']} → {preview}",
                    {"tool": call["name"], "ok": tool_result.ok,
                     "elapsed_ms": tool_result.elapsed_ms,
                     "preview": preview})
                content = tool_result.content if tool_result.ok else \
                    f"Error: {tool_result.error}"
                verdict = None
                if tool_result.ok and self.harness.verify_writes:
                    verdict = verify_result(call["name"], call.get("args") or {})
                    if verdict:
                        content = f"{content}\n[{verdict}]"
                failed_check = bool(verdict and verdict.startswith("VERIFICATION FAILED"))
                breaker.note(call["name"], tool_result.ok and not failed_check,
                             tool_result.error or verdict or "")
                decision = decided.get(id(call))
                self.harness.audit.record({
                    "event": "tool", "turn": self.turn_count, "tool": call["name"],
                    "risk": getattr(decision, "risk", ""), "decision": getattr(
                        decision, "action", ""), "ok": tool_result.ok,
                    "verified": verdict or "", "elapsed_ms": tool_result.elapsed_ms,
                    "args": call.get("args") or {}})
                messages.append(ToolMessage(
                    content=self.brain.context.shrink_tool_result(content),
                    tool_call_id=call.get("id", ""), name=call["name"]))

            result.tool_calls = tool_call_log
            if direct_answer and tool_results:
                first = tool_results[0]
                final_text = first.content if first.ok else f"Error: {first.error}"
                yield TurnEvent("text", final_text)
                break
            # refresh the element map / DOM view hints when the browser changed page
            if any(call["name"].startswith(("browse_", "page_")) for call in calls):
                bound_tools = self.brain.select_relevant_tools(
                    user_input + " browser page", available_tools)
        else:
            yield TurnEvent("notice", "⚠ reached the iteration limit for this turn")

        # 6. remember --------------------------------------------------------
        answer = final_text or "(no answer produced)"
        if not final_text and tool_call_log:
            # the loop ended (iteration limit, timeout) before the model wrote an answer
            last = tool_call_log[-1]
            answer = (f"I could not finish this: {len(tool_call_log)} tool call(s) ran, "
                      f"the last was {last['name']} ({'ok' if last['ok'] else 'failed'}), "
                      "and nothing was confirmed. Try a more specific request.")
        if action_request and not any(entry["ok"] for entry in tool_call_log):
            # the model may have *said* it did something; no tool confirmed it
            if tool_call_log:
                failed = ", ".join(sorted({entry["name"] for entry in tool_call_log}))
                answer = f"The action did not succeed ({failed} failed), so nothing was done."
                yield TurnEvent("error", f"⚠ action failed: {failed} — nothing was done")
            else:
                answer = _UNVERIFIED
                yield TurnEvent("error", "⚠ no tool ran for this request — nothing was done")
        result.answer = answer
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            result.memory = self.memory.record_turn(
                user_input, answer, tool_calls=tool_call_log,
                tokens_used=result.prompt_tokens + result.completion_tokens)
            if result.memory.get("stored"):
                yield TurnEvent("notice",
                                f"💾 stored in memory (importance "
                                f"{result.memory['importance']})",
                                {"kind": "memory-write"})
        except Exception as exc:
            log.warning(f"memory write failed: {exc}")

        self.last_result = result
        yield TurnEvent("done", "", {"result": result.summary(),
                                     "tool_calls": len(tool_call_log),
                                     "iterations": result.iterations,
                                     "latency_ms": result.latency_ms})

    async def ask(self, user_input: str, **kwargs: Any) -> TurnResult:
        """Convenience wrapper: run a turn and return the result (no streaming)."""
        async for event in self.process(user_input, **kwargs):
            if event.type == "error" and not self.last_result:
                log.warning(event.text)
        return self.last_result or TurnResult()

    def _fallback_reason(self, provider: str) -> str:
        """The latest recorded failure reason for ``provider``, for the fallback notice."""
        for event in reversed(list(self.brain.api.events)):
            if event.from_provider == provider:
                detail = (event.detail or event.reason or "").strip()
                return f" ({event.reason}: {detail[:160]})" if detail else f" ({event.reason})"
        return ""

    # ── helpers ────────────────────────────────────────────────────────────
    def _closest_tool(self, name: str) -> str:
        if not name:
            return ""
        candidates = self.registry.names()
        lowered = name.lower()
        best, best_score = "", 0.0
        for candidate in candidates:
            score = _similarity(lowered, candidate.lower())
            if score > best_score:
                best, best_score = candidate, score
        return best if best_score > 0.6 else ""

    @staticmethod
    def _is_stateful(tool_name: str) -> bool:
        """Tools that must run sequentially (they mutate browser/window/file/memory state)."""
        return tool_name.startswith((
            "page_", "browse_", "cursor_", "keyboard_",
            "screen_", "browser_", "mouse", "launch_",
            "obsidian_write", "obsidian_daily", "obsidian_flashcards_add",
            "obsidian_flashcards_review", "obsidian_tasks",
        )) or tool_name in (
            "run_bash", "run_python", "write_file", "append_file",
            "delete_path", "move_path", "copy_path",
            "send_email", "send_webhook", "send_notification", "speak",
            "remember", "forget", "download_file",
        )

    # ── reporting ──────────────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        return {
            "brain": self.brain.status(),
            "memory": self.memory.stats(),
            "tools": self.registry.stats(),
            "turns": self.turn_count,
            "last_turn": self.last_result.summary() if self.last_result else None,
        }

    def report(self) -> str:
        lines = [self.brain.health_report(), "", self.memory.report(), "",
                 self.registry.summary()]
        return "\n".join(lines)


# Requests that can only be satisfied by running a tool.  If the model answers
# these with plain text and no tool ran, the turn must say so instead of claiming it.
_ACTION_RE = re.compile(
    r"\b(play|open|launch|visit|browse|click|send|email|kholo|chalao|bajao|"
    r"remember|delete|download)\b", re.IGNORECASE)
_NUDGE = ("This request needs an action and no tool has run yet. Call the matching "
          "tool now, using one of the bound tool names. Do not describe steps and do "
          "not say the action is done before a tool confirms it.")
_UNVERIFIED = ("I did not run a tool for this, so nothing was actually done. "
               "Please ask again, or name the action explicitly.")


_PLAY_RE = re.compile(
    r"\b(?:play|bajao|chalao|sunao)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+)?(?P<q>[^?!.\n]+?)"
    r"(?:\s+(?:on|in|from)\s+(?:youtube\s+music|youtube|yt)\b.*)?\s*$", re.IGNORECASE)
_NOT_A_SONG_RE = re.compile(r"^(my|that|it|this|again|something|anything)\b", re.IGNORECASE)


def forced_play_query(user_input: str) -> str:
    """The song to search for when the operator asked to play one, else ``""``.

    "play pasuri on youtube" -> "pasuri"; "open yt and play a random song" ->
    "random song".  "play my fav song" is left alone: it needs memory, not a search.
    """
    match = _PLAY_RE.search(user_input or "")
    if not match:
        return ""
    query = re.sub(r"\s+(song|video|music|track)$", "", match.group("q").strip(),
                   flags=re.IGNORECASE).strip()
    if not query or _NOT_A_SONG_RE.match(query):
        return ""
    if query.lower() == "random":  # "play a random song": a random pick, not the word "random"
        return "popular songs"
    return query


_SEARCH_RE = re.compile(
    r"^\s*(?:please\s+|jarvis\s+)?(?:search(?:\s+for)?|google|look\s+up|lookup)\s+"
    r"(?P<q>.+?)[\s.!?]*$", re.IGNORECASE)
_NOT_A_WEB_SEARCH = re.compile(
    r"^(my|the|in|a\s+file|files?|folder|memory|notes?|history|for\s+my)\b", re.IGNORECASE)


def forced_search_query(user_input: str) -> str:
    """The web search words for "search X" / "google X" / "look up X", else ``""``.

    "search my notes for exams" is not a web search and returns ``""``.
    """
    match = _SEARCH_RE.match(user_input or "")
    if not match:
        return ""
    query = match.group("q").strip()
    if not query or _NOT_A_WEB_SEARCH.match(query):
        return ""
    return query


def direct_route(user_input: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """A (tool, args) to run without asking the model, for clear play and search requests."""
    song = forced_play_query(user_input)
    if song:
        return "play_song", {"query": song}
    search = forced_search_query(user_input)
    if search:
        return "run_pipeline", {"name": "web_search", "query": search}
    return None


def needs_action(user_input: str) -> bool:
    """True when the request can only be fulfilled by calling a tool."""
    return bool(_ACTION_RE.search(user_input or ""))


def _format_args(args: Optional[Mapping[str, Any]], *, limit: int = 120) -> str:
    if not args:
        return ""
    parts = []
    for key, value in list(args.items())[:6]:
        parts.append(f"{key}={truncate(str(value), 40)}")
    return truncate(", ".join(parts), limit)


def _similarity(a: str, b: str) -> float:
    """Cheap token-overlap similarity for tool-name suggestions."""
    if a == b:
        return 1.0
    tokens_a, tokens_b = set(a.replace("_", " ").split()), set(b.replace("_", " ").split())
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b)
    return overlap / max(len(tokens_a), len(tokens_b))
