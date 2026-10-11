"""Communication tools: email, webhooks, notifications, speech."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .email_tool import EMAIL_TOOLS
from .notification import NOTIFICATION_TOOLS
from .webhook_tool import WEBHOOK_TOOLS

__all__ = ["register_tools"]

ALL_COMMS_TOOLS = EMAIL_TOOLS + WEBHOOK_TOOLS + NOTIFICATION_TOOLS


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    return registry.register_all(ALL_COMMS_TOOLS, "communication")
