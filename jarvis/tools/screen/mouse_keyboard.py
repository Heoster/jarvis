"""Mouse + keyboard control with *human-like* cursor movement.

The cursor does not teleport.  JARVIS eases it along a slightly curved path
(quadratic bezier + optional jitter) with an ease-in-out velocity profile, the
same way a person moves a mouse.  Two reasons this matters:

1. hover menus, tooltips and drag targets fire correctly only when the pointer
   actually travels through the page;
2. perfectly straight, instant jumps are a classic bot signature.

Everything routes through the backend chain in :mod:`backends`, so the same code
drives pyautogui, xdotool, ydotool, PowerShell or cliclick.

The module also exposes the low-level `click_screen_point` used by the browser
harness for its DOM→cursor hybrid clicks.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Dict, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from ..base import failure, tool
from .backends import ScreenInfo, get_backend

log = get_logger("screen.input")

__all__ = [
    "move_cursor", "click_screen_point", "mouse_click", "mouse_double_click",
    "mouse_drag", "type_text", "press_key", "hotkey", "get_cursor_position",
    "get_screen_size", "scroll_screen", "bezier_path",
]


# ── geometry ────────────────────────────────────────────────────────────────
def bezier_path(start: Tuple[float, float], end: Tuple[float, float],
                steps: int = 24, *, curve: float = 0.18,
                jitter: bool = True) -> Sequence[Tuple[int, int]]:
    """Quadratic bezier from ``start`` to ``end`` with a mild bow."""
    (x0, y0), (x1, y1) = start, end
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)
    if distance < 4 or steps < 2:
        return [(int(round(x1)), int(round(y1)))]

    # control point: perpendicular offset, so the path bows like a hand would
    sign = random.choice((-1, 1)) if jitter else 1
    offset = min(120.0, distance * curve) * sign
    cx = (x0 + x1) / 2 - dy / (distance or 1) * offset
    cy = (y0 + y1) / 2 + dx / (distance or 1) * offset

    points: list[Tuple[int, int]] = []
    for step in range(1, steps + 1):
        t = step / steps
        # ease-in-out so the pointer accelerates then settles
        te = t * t * (3 - 2 * t)
        x = (1 - te) ** 2 * x0 + 2 * (1 - te) * te * cx + te ** 2 * x1
        y = (1 - te) ** 2 * y0 + 2 * (1 - te) * te * cy + te ** 2 * y1
        if jitter and step < steps:
            x += random.uniform(-0.8, 0.8)
            y += random.uniform(-0.8, 0.8)
        points.append((int(round(x)), int(round(y))))
    return points


def get_cursor_position() -> Tuple[int, int]:
    """Current cursor position (0,0 when no backend is available)."""
    return get_backend().position()


def get_screen_size() -> ScreenInfo:
    return get_backend().screen_size()


# ── mouse ───────────────────────────────────────────────────────────────────
def move_cursor(x: int, y: int, *, duration_ms: int = 260, smooth: bool = True,
                jitter: bool = True) -> bool:
    """Move the real cursor to absolute screen coordinates."""
    backend = get_backend()
    if not smooth or duration_ms <= 0:
        return backend.move(int(x), int(y))
    start = get_cursor_position()
    steps = max(6, min(48, int(duration_ms / 16)))
    path = bezier_path(start, (int(x), int(y)), steps, jitter=jitter)
    delay = (duration_ms / 1000.0) / max(1, len(path))
    for point in path:
        backend.move(point[0], point[1])
        time.sleep(delay)
    return backend.move(int(x), int(y))


def click_screen_point(x: int, y: int, *, button: str = "left", clicks: int = 1,
                       move_duration_ms: int = 240, jitter: bool = True) -> Dict[str, Any]:
    """Move the cursor like a human and click — the harness's hybrid primitive."""
    backend = get_backend()
    before = get_cursor_position()
    move_cursor(x, y, duration_ms=move_duration_ms, jitter=jitter)
    time.sleep(random.uniform(0.03, 0.10))
    ok = backend.click(int(x), int(y), button=button, clicks=int(clicks))
    return {"ok": bool(ok), "point": [int(x), int(y)], "from": list(before),
            "backend": backend.name}


def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1,
                smooth: bool = True) -> str:
    result = click_screen_point(x, y, button=button, clicks=clicks,
                               move_duration_ms=260 if smooth else 0)
    if not result["ok"]:
        return failure(f"Click failed at ({x}, {y}) — backend: {result['backend']}. "
                       "Install pyautogui/xdotool and make sure a display is running.")
    return (f"✅ {button} click ×{clicks} at ({x}, {y}) via {result['backend']} "
            f"(cursor travelled from {tuple(result['from'])})")


def mouse_drag(from_x: int, from_y: int, to_x: int, to_y: int, *,
               duration_ms: int = 600, button: str = "left") -> str:
    """Press at one point, travel to another, release — a real drag."""
    backend = get_backend()
    move_cursor(from_x, from_y, duration_ms=220)
    time.sleep(0.08)
    if not backend.button_down(button):
        return failure(f"Drag unsupported by backend '{backend.name}'. Use two "
                       "clicks instead, or install pyautogui/xdotool.")
    path = bezier_path((from_x, from_y), (to_x, to_y),
                       steps=max(10, min(60, duration_ms // 14)), curve=0.10)
    delay = (duration_ms / 1000.0) / max(1, len(path))
    for point in path:
        backend.move(point[0], point[1])
        time.sleep(delay)
    backend.move(int(to_x), int(to_y))
    time.sleep(0.08)
    backend.button_up(button)
    return f"✅ Dragged {button} from ({from_x}, {from_y}) to ({to_x}, {to_y})"


# ── keyboard ────────────────────────────────────────────────────────────────
def type_text(text: str, *, interval: float = 0.012,
              wpm: Optional[float] = None) -> str:
    """Type text into the focused window at a human-ish cadence."""
    if wpm:
        interval = max(0.0, 60.0 / max(1.0, wpm * 5))  # ~5 chars per word
    ok = get_backend().type_text(text, interval=interval)
    if not ok:
        return failure(f"Typing failed (backend: {get_backend().name}). "
                       "Is any window focused? (install xdotool/pyautogui on Linux)")
    preview = text if len(text) <= 60 else f"{text[:60]}…"
    return f"✅ Typed {len(text)} characters: {preview}"


def press_key(key: str) -> str:
    ok = get_backend().press_key(key)
    if not ok:
        return failure(f"Key press failed: {key} (backend: {get_backend().name})")
    return f"✅ Pressed {key}"


def hotkey(*keys: str) -> str:
    combo = "+".join(k.strip() for k in keys if k)
    return press_key(combo)


def scroll_screen(amount: int = -500, *, x: Optional[int] = None,
                  y: Optional[int] = None) -> str:
    if x is not None and y is not None:
        move_cursor(x, y, duration_ms=180)
    ok = get_backend().scroll(int(amount), x=x, y=y)
    if not ok:
        return failure("Scroll failed: no input backend could scroll "
                       "(install pyautogui or xdotool).")
    direction = "up" if amount > 0 else "down"
    return f"✅ Scrolled {direction} {abs(int(amount))}px"


# ── tools exposed to the LLM ────────────────────────────────────────────────
@tool(category="screen", timeout=30)
def cursor_move(x: int, y: int, smooth: bool = True) -> str:
    """Move the mouse cursor to absolute screen coordinates.

    Use this when you need to hover over something, or before clicking a spot
    that has no DOM element (canvas, image map, game, native dialog).

    Args:
        x: X coordinate in screen pixels (from the left edge)
        y: Y coordinate in screen pixels (from the top edge)
        smooth: Animate the movement like a human (recommended)
    """
    ok = move_cursor(x, y, duration_ms=280 if smooth else 0)
    if not ok:
        return failure(f"Cursor move failed (backend: {get_backend().name}). "
                       "Install pyautogui/xdotool and make sure a display is running.")
    return f"✅ Cursor at {get_cursor_position()}"


@tool(category="screen", timeout=30)
def cursor_click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Click at absolute screen coordinates with the real mouse cursor.

    Use for anything the DOM cannot click: canvas buttons, PDF viewers, native
    dialogs, game windows, or when the browser reports the element is covered.

    Args:
        x: X coordinate in screen pixels
        y: Y coordinate in screen pixels
        button: "left", "right" or "middle"
        clicks: 1 for single, 2 for double click
    """
    return mouse_click(x, y, button, clicks)


@tool(category="screen", timeout=60)
def keyboard_type(text: str, wpm: float = 220) -> str:
    """Type text with the keyboard into whatever window has focus.

    Args:
        text: The text to type
        wpm: Typing speed in words per minute (lower looks more human)
    """
    return type_text(text, wpm=wpm)


@tool(category="screen", timeout=30)
def keyboard_press(key: str) -> str:
    """Press a key or key combination in the focused window.

    Args:
        key: "enter", "escape", "tab", "ctrl+c", "ctrl+v", "alt+tab",
             "ctrl+shift+t", "win", "f5", "pagedown" …
    """
    return press_key(key)


@tool(category="screen", timeout=30)
def screen_cursor_position() -> str:
    """Report the current cursor position and screen resolution."""
    x, y = get_cursor_position()
    size = get_screen_size()
    return (f"Cursor: ({x}, {y})\nScreen: {size.width}×{size.height}\n"
            f"Backend: {get_backend().name}")


@tool(category="screen", timeout=30)
def screen_scroll(amount: int = -500, x: int = -1, y: int = -1) -> str:
    """Scroll the focused window (or the window under the cursor).

    Args:
        amount: Negative scrolls down, positive scrolls up (pixels)
        x: Optional X to move the cursor to first (-1 = leave it)
        y: Optional Y to move the cursor to first (-1 = leave it)
    """
    return scroll_screen(amount, x=None if x < 0 else x, y=None if y < 0 else y)


MOUSE_KEYBOARD_TOOLS = [
    cursor_move, cursor_click, keyboard_type, keyboard_press,
    screen_cursor_position, screen_scroll,
]
