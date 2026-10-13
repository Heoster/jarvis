"""Screen control tools (focus area #3).

No vision model is used anywhere in this package: the screen is read as text
(DOM plus OCR) and driven with genuine OS input events.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .actions import ACTION_TOOLS
from .mouse_keyboard import MOUSE_KEYBOARD_TOOLS
from .ocr_reader import OCR_TOOLS
from .screen_capture import CAPTURE_TOOLS
from .window_manager import WINDOW_TOOLS

__all__ = ["register_tools", "ALL_SCREEN_TOOLS", "desktop_report", "init_screen"]

ALL_SCREEN_TOOLS = (MOUSE_KEYBOARD_TOOLS + CAPTURE_TOOLS + OCR_TOOLS
                    + WINDOW_TOOLS + ACTION_TOOLS)


def init_screen(config: Optional[Mapping[str, Any]] = None) -> None:
    """Prime the screen subsystem from the loaded config.

    Call once at startup (the registry already does this via ``register_tools``).
    Idempotent — safe to call multiple times.
    """
    from .backends import get_backend

    # Force backend selection with the real config so screen.backend /
    # screen.failsafe are respected from the very first tool call.
    get_backend(dict(config or {}), refresh=True)

    # Pre-warm the tesseract path so the first OCR call doesn't stall.
    try:
        from .ocr_reader import _tesseract_path
        _tesseract_path()
    except Exception:
        pass


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    """Register every screen tool into ``registry`` and initialise the subsystem."""
    init_screen(config)
    return registry.register_all(ALL_SCREEN_TOOLS, "screen")


def desktop_report() -> str:
    from .actions import desktop_report as _report

    return _report()


def __getattr__(name: str) -> Any:  # lazy re-exports
    if name in ("click_screen_point", "move_cursor", "type_text", "press_key"):
        from . import mouse_keyboard

        return getattr(mouse_keyboard, name)
    if name in ("capture_desktop", "capture_region"):
        from . import screen_capture

        return getattr(screen_capture, name)
    if name in ("get_backend", "choose_backend", "list_backends"):
        from . import backends

        return getattr(backends, name)
    raise AttributeError(name)


def __getattr__(name: str) -> Any:  # lazy re-exports
    if name in ("click_screen_point", "move_cursor", "type_text", "press_key"):
        from . import mouse_keyboard

        return getattr(mouse_keyboard, name)
    if name in ("capture_desktop", "capture_region"):
        from . import screen_capture

        return getattr(screen_capture, name)
    if name in ("get_backend", "choose_backend", "list_backends"):
        from . import backends

        return getattr(backends, name)
    raise AttributeError(name)
