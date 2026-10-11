"""Page screenshots — captured, never "seen" by a vision model.

JARVIS reads screens as *text* (DOM + OCR).  Screenshots exist so the operator
has a record, and so the OCR path can extract text from canvas-heavy pages where
the DOM has nothing to say.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from ...utils.logger import get_logger
from ..base import failure, tool

log = get_logger("browser.screenshot")

__all__ = ["screenshot_page", "screenshot_and_read", "cleanup_screenshots"]


@tool(category="browser", timeout=90)
async def screenshot_page(path: str = "", full_page: bool = False) -> str:
    """Capture a screenshot of the current browser page to a PNG file.

    Args:
        path: Where to save it (default: data/screenshots/page-<timestamp>.png)
        full_page: Capture the whole scrollable page instead of the viewport
    """
    from .harness import HarnessError, get_harness

    harness = get_harness()
    try:
        result = await harness.screenshot(full_page=full_page, name=Path(path).name if path else "")
    except HarnessError as exc:
        return failure(f"Screenshot failed: {exc}")
    if not result.get("ok"):
        return failure(f"Screenshot failed: {result.get('error')}")
    saved = result["path"]
    if path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            Path(saved).replace(target)
            saved = str(target)
        except Exception as exc:
            log.debug(f"could not move screenshot: {exc}")
    return f"✅ Saved page screenshot: {saved}"


@tool(category="browser", timeout=120)
async def screenshot_and_read(region: Optional[str] = None) -> str:
    """Screenshot the page and OCR it into text (for pages with no readable DOM).

    Args:
        region: Optional "x,y,width,height" screen region; default is the page area
    """
    from ..screen.ocr_reader import read_image_text
    from .harness import HarnessError, get_harness

    harness = get_harness()
    try:
        result = await harness.screenshot(full_page=False)
    except HarnessError as exc:
        return failure(f"Screenshot failed: {exc}")
    if not result.get("ok"):
        return failure(f"Screenshot failed: {result.get('error')}")
    text = read_image_text(result["path"], region=region)
    return (f"Screenshot: {result['path']}\n\n"
            f"**Text read from the image (OCR):**\n{text or '(no text detected)'}")


def cleanup_screenshots(*, keep: int = 40, directory: Optional[str] = None) -> int:
    """Delete old screenshots so the repo never bloats."""
    from ...config import load_config

    config = load_config()
    folder = Path(directory or (config.get("browser") or {}).get(
        "screenshot_dir") or (Path(__file__).resolve().parents[3] / "data" / "screenshots"))
    if not folder.exists():
        return 0
    files = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            continue
    return removed


SCREENSHOT_TOOLS = [screenshot_page, screenshot_and_read]
