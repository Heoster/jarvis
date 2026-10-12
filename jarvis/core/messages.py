"""Lightweight, LangChain-compatible message types.

JARVIS talks to LangChain when it is installed (``langchain_core.messages``
objects work everywhere in this codebase) but must also run on a bare Python
install.  These classes mirror the attribute surface the rest of the code uses —
``content``, ``tool_calls``, ``tool_call_id``, ``type`` — so both worlds are
interchangeable.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = [
    "Message", "SystemMessage", "HumanMessage", "AIMessage", "ToolMessage",
    "AIMessageChunk", "message_from_dict", "to_openai_messages", "ensure_message",
    "ensure_messages", "normalise_role", "text_of",
]

_ROLE_TO_CLASS = {
    "system": "SystemMessage",
    "human": "HumanMessage",
    "user": "HumanMessage",
    "ai": "AIMessage",
    "assistant": "AIMessage",
    "tool": "ToolMessage",
    "function": "ToolMessage",
}


def _new_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


@dataclass
class Message:
    """Base message.  ``content`` is always a string; tools get their own field."""

    content: str = ""
    role: str = "human"
    name: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # langchain parity helpers ------------------------------------------------
    @property
    def type(self) -> str:  # noqa: A003 - parity with langchain
        return self.role

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload

    def pretty(self, limit: int = 400) -> str:
        text = self.content if len(self.content) <= limit else f"{self.content[:limit]}…"
        extra = f" [{len(self.tool_calls)} tool call(s)]" if self.tool_calls else ""
        return f"{self.role}: {text}{extra}"

    def __str__(self) -> str:  # pragma: no cover - debugging convenience
        return self.pretty()


@dataclass
class SystemMessage(Message):
    role: str = "system"
    content: str = ""


@dataclass
class HumanMessage(Message):
    role: str = "human"
    content: str = ""


@dataclass
class AIMessage(Message):
    role: str = "ai"
    content: str = ""


@dataclass
class AIMessageChunk(Message):
    role: str = "ai"
    content: str = ""


@dataclass
class ToolMessage(Message):
    role: str = "tool"
    content: str = ""
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


def normalise_role(role: str) -> str:
    return {
        "user": "human", "human": "human",
        "assistant": "ai", "ai": "ai", "model": "ai",
        "system": "system",
        "tool": "tool", "function": "tool", "tool_result": "tool",
    }.get(str(role).lower(), "human")


def text_of(message: Any) -> str:
    """Content of any message-ish object as plain text."""
    content = getattr(message, "content", message)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(content)


def ensure_message(message: Any) -> Message:
    """Coerce dicts / langchain messages / plain strings into our Message."""
    if isinstance(message, Message):
        return message
    if isinstance(message, str):
        return HumanMessage(content=message)
    if isinstance(message, Mapping):
        return message_from_dict(dict(message))
    role = getattr(message, "type", None) or getattr(message, "role", "human")
    cls = globals().get(_ROLE_TO_CLASS.get(normalise_role(str(role)), "HumanMessage"),
                        HumanMessage)
    tool_calls = []
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, Mapping):
            tool_calls.append({
                "id": call.get("id") or _new_id(),
                "name": call.get("name", ""),
                "args": dict(call.get("args") or call.get("arguments") or {}),
            })
    return cls(  # type: ignore[misc]
        content=text_of(message),
        name=getattr(message, "name", None),
        tool_calls=tool_calls,
        tool_call_id=getattr(message, "tool_call_id", None),
    )


def ensure_messages(messages: Iterable[Any]) -> List[Message]:
    return [ensure_message(m) for m in messages]


def message_from_dict(data: Mapping[str, Any]) -> Message:
    role = normalise_role(str(data.get("role", "human")))
    cls = globals().get(_ROLE_TO_CLASS.get(role, "HumanMessage"), HumanMessage)
    tool_calls: List[Dict[str, Any]] = []
    for call in data.get("tool_calls") or []:
        if isinstance(call, Mapping):
            fn = call.get("function") if isinstance(call.get("function"), Mapping) else {}
            args = call.get("args", fn.get("arguments", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            tool_calls.append({
                "id": call.get("id") or _new_id(),
                "name": call.get("name") or fn.get("name", ""),
                "args": dict(args or {}),
            })
    content = data.get("content", "")
    if isinstance(content, (list, tuple, dict)):
        content = text_of(content)
    return cls(  # type: ignore[misc]
        content=content if isinstance(content, str) else str(content),
        name=data.get("name"),
        tool_calls=tool_calls,
        tool_call_id=data.get("tool_call_id") or data.get("tool_call_id "),
        metadata=dict(data.get("metadata") or {}),
    )


def to_openai_messages(messages: Sequence[Any]) -> List[Dict[str, Any]]:
    """Serialise to the OpenAI ``/chat/completions`` shape (shared by Groq,
    OpenRouter, Ollama and the API server)."""
    out: List[Dict[str, Any]] = []
    for raw in messages:
        msg = ensure_message(raw)
        role = normalise_role(msg.role)
        # OpenAI's wire format uses user/assistant; our internal names are
        # human/ai.  Sending "human" makes most providers reject the request.
        payload: Dict[str, Any] = {
            "role": {"ai": "assistant", "human": "user"}.get(role, role)
        }
        payload["content"] = msg.content or ""
        if msg.name:
            payload["name"] = msg.name
        if role == "tool":
            payload["tool_call_id"] = msg.tool_call_id or _new_id()
            if msg.name:
                payload["name"] = msg.name
        if msg.tool_calls and role == "ai":
            payload["tool_calls"] = [
                {
                    "id": call.get("id") or _new_id(),
                    "type": "function",
                    "function": {
                        "name": call.get("name"),
                        "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                    },
                }
                for call in msg.tool_calls
            ]
            if not msg.content:
                payload["content"] = None
        out.append(payload)
    return out


def merge_summary(parts: Iterable[str], max_chars: int = 4000) -> str:
    text = "\n".join(p for p in parts if p)
    if len(text) <= max_chars:
        return text
    head = text[: int(max_chars * 0.6)]
    tail = text[-int(max_chars * 0.35):]
    return f"{head}\n[…]\n{tail}"
