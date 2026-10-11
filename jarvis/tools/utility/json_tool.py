"""JSON/YAML helpers + text utilities for the agent."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ...utils.logger import get_logger
from ...utils.sanitizer import safe_json, truncate
from ..base import tool

log = get_logger("utility.json")

__all__ = ["json_parse", "json_query", "text_stats", "JSON_TOOLS"]


def _load(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            return yaml.safe_load(text)
        except Exception as exc:
            raise ValueError(f"not valid JSON or YAML: {exc}") from exc


@tool(category="utility", timeout=20)
def json_parse(text: str, indent: int = 2) -> str:
    """Validate and pretty-print JSON (or YAML).

    Args:
        text: The JSON/YAML text to parse
        indent: Indentation for the formatted output
    """
    try:
        data = _load(text)
    except ValueError as exc:
        return f"❌ {exc}"
    formatted = safe_json(data, indent=indent)
    kind = type(data).__name__
    summary = f"✅ Valid {kind}"
    if isinstance(data, dict):
        summary += f" with {len(data)} keys: {', '.join(list(data)[:12])}"
    elif isinstance(data, list):
        summary += f" with {len(data)} items"
    return f"{summary}\n\n```json\n{truncate(formatted, 4000)}\n```"


@tool(category="utility", timeout=20)
def json_query(text: str, path: str) -> str:
    """Extract a value from JSON using a dotted path.

    Args:
        text: The JSON text
        path: Dotted path, e.g. "results.0.title" or "user.name"
    """
    try:
        data = _load(text)
    except ValueError as exc:
        return f"❌ {exc}"
    node = data
    for part in [p for p in path.split(".") if p]:
        if isinstance(node, list):
            if not part.lstrip("-").isdigit():
                return f"'{part}' indexes a list but is not a number."
            index = int(part)
            if not -len(node) <= index < len(node):
                return f"Index {index} out of range (list has {len(node)} items)."
            node = node[index]
        elif isinstance(node, dict):
            if part not in node:
                return f"Key '{part}' not found. Available: {', '.join(list(node)[:15])}"
            node = node[part]
        else:
            return f"Cannot descend into {type(node).__name__} at '{part}'."
    return f"{path} = {safe_json(node, indent=2, max_chars=3000)}"


@tool(category="utility", timeout=20)
def text_stats(text: str) -> str:
    """Count words, characters, lines and estimate reading time for a text.

    Args:
        text: The text to analyse
    """
    words = len(re.findall(r"\S+", text))
    lines = text.count("\n") + (1 if text else 0)
    sentences = len([s for s in re.split(r"[.!?]+", text) if s.strip()])
    reading_seconds = words / 3.5  # ~210 wpm
    return "\n".join([
        f"characters : {len(text):,}",
        f"words      : {words:,}",
        f"lines      : {lines:,}",
        f"sentences  : {sentences:,}",
        f"est. tokens: ~{max(1, len(text) // 4):,}",
        f"reading    : ~{reading_seconds / 60:.1f} min",
    ])


JSON_TOOLS = [json_parse, json_query, text_stats]
