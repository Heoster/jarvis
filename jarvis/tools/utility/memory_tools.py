"""Memory tools — the agent's own interface to the smart memory manager.

These exist so the *model* can choose to remember, recall or search proactively
instead of relying only on automatic recall at the start of a turn.
"""

from __future__ import annotations

from typing import List, Optional

from ...utils.logger import get_logger
from ..base import tool

log = get_logger("memory.tools")

__all__ = ["remember", "recall", "search_memory", "forget", "memory_stats",
           "MEMORY_TOOLS"]


def _manager():
    from ...memory.memory_manager import get_memory_manager

    return get_memory_manager()


@tool(category="memory", timeout=30)
def remember(fact: str, importance: float = 0.85) -> str:
    """Store something in long-term memory so it survives restarts.

    Use it when Heoster says "remember…", shares a preference, a project detail,
    a deadline, a decision, or anything that should still be true next month.

    Args:
        fact: The fact, written as a standalone sentence (include the who/what)
        importance: 0–1, how important this is to keep (default 0.85)
    """
    record = _manager().remember(fact, importance=importance, tags=["explicit"],
                                 source="tool:remember")
    if record is None:
        return "That is already in memory (reinforced it instead of duplicating)."
    return f"🧠 Remembered: {fact}\n_(id {record.id}, importance {importance})_"


@tool(category="memory", timeout=30)
def recall(query: str, k: int = 5) -> str:
    """Search long-term memory for anything relevant to a query.

    Args:
        query: What to look for (a topic, a name, a decision)
        k: How many memories to return
    """
    manager = _manager()
    recall_result = manager.recall(query, k=k, include_knowledge=False,
                                   include_skills=False)
    if not recall_result.memories:
        return f"Nothing in memory about '{query}'."
    lines = [f"🧠 {len(recall_result.memories)} memory item(s) for '{query}':"]
    for record, score in recall_result.memories:
        lines.append(f"  [{record.tier.value} · {score:.2f}] {record.preview(200)}")
    return "\n".join(lines)


@tool(category="memory", timeout=45)
def search_memory(query: str, limit: int = 8) -> str:
    """Search across all memory tiers AND past session transcripts.

    Args:
        query: Text to search for
        limit: Maximum hits
    """
    results = _manager().search(query, limit=limit)
    if not results:
        return f"Nothing found for '{query}'."
    lines = [f"{len(results)} hit(s) for '{query}':"]
    for item in results:
        score = f"{item['score']:.2f}" if item.get("score") is not None else "—"
        lines.append(f"  {item['where']:<16} {score:>5}  {item['content']}")
    return "\n".join(lines)


@tool(category="memory", timeout=30)
def forget(memory_id: str) -> str:
    """Delete a specific memory by its id (get ids from `recall`).

    Args:
        memory_id: The memory id, e.g. "epi_3f9a1c2b"
    """
    if _manager().long.forget(memory_id):
        return f"🗑️ Forgot memory {memory_id}"
    return f"No memory with id {memory_id}"


@tool(category="memory", timeout=30)
def memory_stats() -> str:
    """Report memory usage: tiers, sizes, knowledge base, skills and budget."""
    return _manager().report()


MEMORY_TOOLS = [remember, recall, search_memory, forget, memory_stats]
