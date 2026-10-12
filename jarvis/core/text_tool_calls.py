"""Recover tool calls that a model wrote as plain text instead of structured calls.

Small local models (hermes3, qwen2.5 and friends) often emit the call inside
the message content, e.g.::

    <tool_call>{"name": "media_play", "arguments": {"query": "pasuri"}}</tool_call>

or a bare JSON object.  Ollama and OpenAI-compatible servers only parse the
structured form, so without this the call is dropped and the raw text is shown
as the answer.

Only objects whose ``name`` is one of the tools bound for the turn are accepted.
Anything else stays in the text untouched, so an invented tool never runs.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Set, Tuple

__all__ = ["extract_text_tool_calls", "tool_names"]

_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def tool_names(tools: Iterable[Any] | None) -> Set[str]:
    """Names of the bound tools, whether they are tool objects or OpenAI schemas."""
    names: Set[str] = set()
    for tool in tools or []:
        if isinstance(tool, str):
            names.add(tool)
            continue
        name = getattr(tool, "name", None)
        if name is None and isinstance(tool, dict):
            function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            name = function.get("name") or tool.get("name")
        if name:
            names.add(str(name))
    return names


def _as_call(obj: Any, known: Set[str]) -> Dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    inner = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    name = inner.get("name")
    if not isinstance(name, str) or name not in known:
        return None
    args = inner.get("arguments", inner.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    return {"id": "", "name": name, "args": args}


def _decode_first_object(text: str) -> Tuple[Any, int]:
    """Decode the first JSON object in ``text``; return (obj, end) or (None, -1)."""
    start = text.find("{")
    if start < 0:
        return None, -1
    try:
        obj, end = json.JSONDecoder().raw_decode(text, start)
        return obj, end
    except ValueError:
        return None, -1


def extract_text_tool_calls(content: str, tools: Iterable[Any] | None
                            ) -> Tuple[List[Dict[str, Any]], str]:
    """Return ``(calls, remaining_text)``.  ``calls`` is empty when nothing matched."""
    known = tool_names(tools)
    if not content or not known:
        return [], content or ""

    calls: List[Dict[str, Any]] = []
    remaining = content

    # 1) explicit <tool_call>…</tool_call> blocks (hermes / qwen convention)
    def take_tag(match: "re.Match[str]") -> str:
        body = match.group(1)
        obj, _ = _decode_first_object(body)
        call = _as_call(obj, known)
        if call is None:
            return match.group(0)            # not ours — leave it in the text
        calls.append(call)
        return ""

    remaining = _TAG_RE.sub(take_tag, remaining)

    # 2) a fenced or bare JSON object naming a bound tool
    if not calls:
        for fence in _FENCE_RE.finditer(remaining):
            call = _as_call(_safe_json(fence.group(1)), known)
            if call is not None:
                calls.append(call)
                remaining = remaining.replace(fence.group(0), "", 1)
                break
    if not calls:
        obj, end = _decode_first_object(remaining)
        call = _as_call(obj, known)
        if call is not None:
            start = remaining.find("{")
            calls.append(call)
            remaining = (remaining[:start] + remaining[end:]).strip()

    return calls, remaining.strip()


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None
