"""Specialist sub-agents and the dispatcher that routes work to them."""

from __future__ import annotations

from .agent_factory import AGENT_TRIGGERS, AgentFactory, RouteDecision
from .base_agent import AgentResult, AgentSpec, BaseAgent
from .specialists import (
    AGENT_CLASSES, BrowserAgent, CodeAgent, FileAgent, MemoryAgent,
    ResearchAgent, ScreenAgent, get_agent,
)

__all__ = [
    "AgentFactory", "RouteDecision", "AGENT_TRIGGERS",
    "BaseAgent", "AgentResult", "AgentSpec",
    "BrowserAgent", "ResearchAgent", "CodeAgent", "FileAgent", "ScreenAgent",
    "MemoryAgent", "AGENT_CLASSES", "get_agent",
]
