"""Screen capture — grab the desktop, a window, or a region.

Backends, in preference order:

1. **mss** — fastest, cross-platform, no dependencies beyond itself;
2. **PIL.ImageGrab** — works on Windows/macOS and X11;
3. **scrot / gnome-screenshot / grim** — CLI fallback on Linux.

Everything returns a file path.  Nothing here feeds pixels to a model: images
exist for the operator, for OCR, and for debugging.  JARVIS reads screens as
*text* by design (no vision model, per the project constraints).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...utils.logger import get_logger
from ...utils.platform_probe import has_display, probe_platform
from ..base import failure, tool

log = get_logger("screen.capture")

__all__ = ["capture_region", "capture_desktop", "list_monitors", "screen_tools_report"]


def _output_path(name: str = "", prefix: str = "desktop") -> Path:
    from ...config import load_config

    config = load_config()
    folder = Path((config.get("screen") or {}).get(
        "screenshot_dir") or (Path(__file__).resolve().parents[3] / "data" / "screenshots"))
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = name or f"{prefix}-{stamp}.png"
    return folder / filename


def list_monitors() -> List[Dict[str, int]]:
    """Enumerate monitors as ``[{'left','top','width','height'}, …]``."""
    try:
        import mss  # type: ignore

        with mss.mss() as sct:
            return [dict(monitor) for monitor in sct.monitors]
    except Exception:
        pass
    size = None
    try:
        from .backends import get_backend

        info = get_backend().screen_size()
        size = {"left": 0, "top": 0, "width": info.width, "height": info.height}
    except Exception:
        size = None
    if size and size["width"]:
        return [{"left": 0, "top": 0, "width": size["width"], "height": size["height"]},
                size]
    return []


def capture_region(region: Optional[Tuple[int, int, int, int]] = None,
                   save_path: str = "", *, monitor: int = 0) -> str:
    """Capture the screen (or a region) to a PNG and return its path.

    ``region`` is ``(left, top, width, height)``; ``None`` grabs the full monitor.
    """
    target = Path(save_path) if save_path else _output_path(prefix="region")

    # 1. mss
    try:
        import mss  # type: ignore

        with mss.mss() as sct:
            monitors = sct.monitors
            if region is None:
                box = monitors[min(monitor, len(monitors) - 1)]
            else:
                left, top, width, height = region
                box = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(box)
            try:
                from PIL import Image  # type: ignore

                Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").save(target)
            except Exception:
                with open(target, "wb") as handle:
                    handle.write(sct.tools.to_png(shot.rgb, shot.size))
        return str(target)
    except Exception as exc:
        log.debug(f"mss capture failed: {exc}")

    # 2. PIL
    try:
        from PIL import ImageGrab  # type: ignore

        box = None if region is None else (region[0], region[1],
                                           region[0] + region[2], region[1] + region[3])
        image = ImageGrab.grab(bbox=box, all_screens=True)
        image.save(target)
        return str(target)
    except Exception as exc:
        log.debug(f"PIL capture failed: {exc}")

    # 3. CLI tools
    for tool_name, argv in (
        ("scrot", ["scrot", "-o", str(target)]),
        ("gnome-screenshot", ["gnome-screenshot", "-f", str(target)]),
        ("import", ["import", "-window", "root", str(target)]),
        ("grim", ["grim", str(target)]),
        ("screencapture", ["screencapture", "-x", str(target)]),
    ):
        if shutil.which(tool_name):
            try:
                subprocess.run(argv, check=True, capture_output=True, timeout=15)
                return str(target)
            except Exception as exc:
                log.debug(f"{tool_name} capture failed: {exc}")

    raise RuntimeError(
        "cannot capture the screen: install one of mss/pillow (pip), or "
        "scrot/grim/gnome-screenshot (apt). "
        + ("" if has_display() else "No DISPLAY detected — start Xvfb first."))


def capture_desktop(*, monitor: int = 0, save_path: str = "") -> str:
    """Capture a full monitor (``monitor=0`` means "all monitors")."""
    return capture_region(None, save_path, monitor=monitor)


@tool(category="screen", timeout=60)
def take_desktop_screenshot(monitor: int = 1, save_path: str = "") -> str:
    """Take a screenshot of the whole screen and save it as a PNG.

    Use this to check what is currently visible, or as evidence of what happened.
    The image is NOT analysed by a vision model — use `read_screen_text` (OCR)
    if you need to know what the screen says.

    Args:
        monitor: 0 = all monitors, 1 = primary, 2 = secondary …
        save_path: Optional explicit path for the PNG
    """
    try:
        path = capture_desktop(monitor=monitor, save_path=save_path)
    except Exception as exc:
        return failure(f"Screenshot failed: {exc}")
    size = ""
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            size = f" ({image.width}×{image.height})"
    except Exception:
        pass
    return f"✅ Desktop screenshot saved: {path}{size}"


@tool(category="screen", timeout=60)
def list_screen_monitors() -> str:
    """List the available monitors with their geometry."""
    monitors = list_monitors()
    if not monitors:
        return failure("No monitor information available (headless environment). "
                       "Install mss (`pip install mss`) and/or set DISPLAY.")
    lines = []
    for index, monitor in enumerate(monitors):
        label = "all monitors" if index == 0 else f"monitor {index}"
        lines.append(f"{label}: {monitor['width']}×{monitor['height']} "
                     f"at ({monitor['left']}, {monitor['top']})")
    return "\n".join(lines)


def screen_tools_report() -> str:
    """Human-readable capability report used by ``jarvis doctor``."""
    from .backends import list_backends

    platform_info = probe_platform()
    lines = [f"display      : {platform_info.display or '(none)'} "
             f"[{platform_info.session_type or 'headless'}]",
             f"desktop      : {platform_info.desktop or 'unknown'}"]
    lines.append("input backends:")
    for name, ok, detail in list_backends():
        lines.append(f"  {'✓' if ok else '·'} {name:<11} {detail}")
    lines.append("capture:")
    for module, label in (("mss", "mss (fast)"), ("PIL", "Pillow ImageGrab")):
        try:
            __import__(module)

            lines.append(f"  ✓ {label}")
        except Exception:
            lines.append(f"  · {label} (not installed)")
    for tool_name in ("scrot", "grim", "gnome-screenshot", "screencapture"):
        if shutil.which(tool_name):
            lines.append(f"  ✓ {tool_name} CLI")
    try:
        from .ocr_reader import ocr_available

        ok, detail = ocr_available()
        lines.append(f"OCR          : {'✓' if ok else '·'} {detail}")
    except Exception as exc:
        lines.append(f"OCR          : · {exc}")
    lines.append(f"vision model : none (by design — OCR + DOM only)")
    return "\n".join(lines)


CAPTURE_TOOLS = [take_desktop_screenshot, list_screen_monitors]
