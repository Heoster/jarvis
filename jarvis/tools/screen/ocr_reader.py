"""OCR — how JARVIS reads a screen without any vision model.

The project constraint is explicit: pixels never go to an LLM.  Instead the
screen is converted into **text plus coordinates** with OCR, and that is more
useful than a caption anyway — it tells the agent *where* the words are, so it
can move the real cursor onto them.

Two engines:

* **tesseract** (via ``pytesseract``) — default, tiny, CPU-friendly;
* **easyocr** — optional, heavier, better on stylised fonts.

``find_on_screen(text)`` returns clickable coordinates, which makes the
"look at the screen, then click what I see" loop possible with zero GPU.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from ..base import failure, tool

log = get_logger("screen.ocr")

__all__ = ["ocr_available", "read_image_text", "find_text_in_image",
           "read_screen_text", "find_on_screen", "ScreenWord", "ocr_report"]


@dataclass
class ScreenWord:
    text: str
    x: int
    y: int
    width: int = 0
    height: int = 0
    confidence: float = 0.0

    @property
    def center(self) -> Tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def line(self) -> str:
        cx, cy = self.center
        return f"'{self.text}' at ({cx}, {cy}) [conf {self.confidence:.0f}%]"


def _tesseract_path() -> Optional[str]:
    """Locate the tesseract binary.

    Priority order:
    1. ``screen.tesseract_cmd`` in config.yaml (explicit operator setting)
    2. ``JARVIS_TESSERACT_PATH`` / ``TESSERACT_CMD`` env vars
    3. ``tesseract`` on PATH (shutil.which)
    4. Known install locations (Windows Program Files, Linux/macOS standard paths)
    """
    # 1. config
    try:
        from ...config import load_config

        cfg_cmd = ((load_config().get("screen") or {}).get("tesseract_cmd") or "")
        if cfg_cmd and Path(cfg_cmd).exists():
            return str(cfg_cmd)
    except Exception:
        pass

    # 2. env vars
    for env_var in ("JARVIS_TESSERACT_PATH", "TESSERACT_CMD"):
        env_val = os.environ.get(env_var, "")
        if env_val and Path(env_val).exists():
            return env_val

    # 3. PATH
    if shutil.which("tesseract"):
        return "tesseract"

    # 4. well-known install locations
    for candidate in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",    # macOS Apple Silicon
        "/opt/local/bin/tesseract",       # macOS MacPorts
    ):
        if Path(candidate).exists():
            return candidate
    return None


def ocr_available() -> Tuple[bool, str]:
    """``(available, detail)`` for the configured OCR engine."""
    try:
        import pytesseract  # noqa: F401
    except Exception:
        pass
    else:
        binary = _tesseract_path()
        if binary:
            try:
                import pytesseract

                pytesseract.pytesseract.tesseract_cmd = binary
                version = str(pytesseract.get_tesseract_version())
                return True, f"tesseract {version}"
            except Exception as exc:
                return False, f"pytesseract present but tesseract failed: {exc}"
        return False, ("pytesseract installed but the tesseract binary is missing "
                       "(apt-get install tesseract-ocr)")
    try:
        import easyocr  # noqa: F401

        return True, "easyocr (heavier, no binary needed)"
    except Exception:
        pass
    return False, "pip install pytesseract + apt-get install tesseract-ocr"


def _scale_for(screenshot_path: str) -> Tuple[float, float]:
    """Coordinate scale between screenshot pixels and cursor pixels."""
    try:
        from PIL import Image  # type: ignore

        with Image.open(screenshot_path) as image:
            shot_w, shot_h = image.width, image.height
    except Exception:
        return 1.0, 1.0
    try:
        from .backends import get_backend

        info = get_backend().screen_size()
        if info.width and info.height:
            return shot_w / info.width, shot_h / info.height
    except Exception:
        pass
    return 1.0, 1.0


def _crop(image: Any, region: Optional[str]) -> Tuple[Any, Tuple[int, int]]:
    """Crop ``"x,y,w,h"`` out of the image; returns (image, (x_offset, y_offset))."""
    if not region:
        return image, (0, 0)
    try:
        parts = [int(float(p.strip())) for p in str(region).split(",")]
        if len(parts) != 4:
            raise ValueError
        x, y, width, height = parts
        return image.crop((x, y, x + width, y + height)), (x, y)
    except Exception:
        log.warning(f"bad region '{region}', using the whole image")
        return image, (0, 0)


def read_image_text(path: str, *, region: Optional[str] = None,
                    engine: str = "auto", preprocess: bool = True) -> str:
    """Extract text from an image file."""
    engine = (engine or "auto").lower()
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return "(Pillow not installed — pip install pillow)"

    try:
        with Image.open(path) as raw:
            image = raw.convert("RGB")
    except Exception as exc:
        return f"(cannot open {path}: {exc})"

    image, _offset = _crop(image, region)
    if preprocess:
        try:
            from PIL import ImageOps

            gray = ImageOps.grayscale(image)
            image = gray.point(lambda value: 255 if value > 150 else 0)
        except Exception:
            pass

    if engine in ("auto", "tesseract"):
        try:
            import pytesseract

            binary = _tesseract_path()
            if binary:
                pytesseract.pytesseract.tesseract_cmd = binary
            text = pytesseract.image_to_string(image, lang="eng")
            return re.sub(r"\n{3,}", "\n\n", text).strip()
        except Exception as exc:
            if engine == "tesseract":
                return f"(tesseract failed: {exc})"

    try:
        import easyocr  # type: ignore

        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        lines = reader.readtext(str(path), detail=0)
        return "\n".join(lines)
    except Exception as exc:
        return f"(no OCR engine available: {exc})"


def find_text_in_image(path: str, needle: str, *, region: Optional[str] = None,
                       min_confidence: float = 45.0,
                       fuzzy: bool = True) -> List[ScreenWord]:
    """Locate ``needle`` on the image and return screen-space coordinates."""
    try:
        import pytesseract
        from pytesseract import Output
        from PIL import Image  # type: ignore
    except Exception:
        return []

    binary = _tesseract_path()
    if binary:
        pytesseract.pytesseract.tesseract_cmd = binary

    try:
        with Image.open(path) as raw:
            image = raw.convert("RGB")
    except Exception:
        return []

    image, (offset_x, offset_y) = _crop(image, region)
    try:
        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except Exception as exc:
        log.debug(f"OCR data extraction failed: {exc}")
        return []

    scale_x, scale_y = _scale_for(path)
    target = needle.lower().strip()
    words: List[ScreenWord] = []

    for index, text in enumerate(data.get("text", [])):
        text = (text or "").strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < min_confidence:
            continue
        match = target in text.lower()
        if not match and fuzzy and len(target) > 3:
            match = _fuzzy(target, text.lower())
        if not match:
            continue
        left = int(data["left"][index] * scale_x) + offset_x
        top = int(data["top"][index] * scale_y) + offset_y
        width = int(data["width"][index] * scale_x)
        height = int(data["height"][index] * scale_y)
        words.append(ScreenWord(text=text, x=left, y=top, width=width,
                                height=height, confidence=confidence))
    words.sort(key=lambda word: (word.y, word.x))
    return words


def _fuzzy(needle: str, haystack: str, *, threshold: float = 0.8) -> bool:
    """Cheap similarity so OCR slips ('0'/'O', 'l'/'I') still match."""
    if not needle or not haystack:
        return False
    if abs(len(needle) - len(haystack)) > max(2, len(needle) // 3):
        return False
    common = sum(1 for a, b in zip(needle, haystack) if a == b)
    return common / max(1, max(len(needle), len(haystack))) >= threshold


# ── tools ───────────────────────────────────────────────────────────────────
@tool(category="screen", timeout=120)
def read_screen_text(monitor: int = 1, region: str = "", ocr_engine: str = "") -> str:
    """Read the text currently visible on screen using OCR (no vision model).

    Use this to find out what an app or page is showing when the DOM is not
    available — a PDF viewer, an image, a native dialog, a game.

    Args:
        monitor: Which monitor to read (1 = primary)
        region: Optional "x,y,width,height" crop, in screen pixels
        ocr_engine: "auto", "tesseract" or "easyocr" (default: from config)
    """
    from ...config import load_config
    from .screen_capture import capture_desktop

    cfg = load_config()
    screen_cfg = cfg.get("screen") or {}
    if not screen_cfg.get("ocr_enabled", True):
        return failure("OCR is disabled in config (screen.ocr_enabled: false). "
                       "Set it to true to enable `read_screen_text`.")
    engine = ocr_engine or str(screen_cfg.get("ocr_engine") or "auto")
    try:
        path = capture_desktop(monitor=monitor)
    except Exception as exc:
        return failure(f"Cannot capture the screen: {exc}")
    text = read_image_text(path, region=region or None, engine=engine)
    if not text:
        return failure(f"No text detected on screen (OCR found nothing). Screenshot "
                       f"saved at {path} — if the screen does show text, install "
                       "tesseract-ocr + pytesseract.")
    return f"**Screen text (OCR from {path}):**\n\n{text}"


@tool(category="screen", timeout=120)
def find_on_screen(text: str, monitor: int = 1, min_confidence: float = 45.0) -> str:
    """Find text on the screen and report the coordinates to click.

    This is the screen "vision" loop without any vision model: OCR gives the
    words and their pixel positions, you then click them with `cursor_click`.

    Args:
        text: Text to look for on screen (partial words are fine)
        monitor: Which monitor to search (1 = primary)
        min_confidence: OCR confidence threshold 0-100
    """
    from .screen_capture import capture_desktop

    try:
        path = capture_desktop(monitor=monitor)
    except Exception as exc:
        return failure(f"Cannot capture the screen: {exc}")
    words = find_text_in_image(path, text, min_confidence=min_confidence)
    if not words:
        return failure(f"'{text}' was not found on screen. Try `read_screen_text` "
                       f"to see what is actually there. (screenshot: {path})")
    lines = [f"Found {len(words)} match(es) for '{text}':"]
    for word in words[:12]:
        cx, cy = word.center
        lines.append(f"  • {word.line()} → `cursor_click({cx}, {cy})`")
    return "\n".join(lines)


@tool(category="screen", timeout=60)
def ocr_report() -> str:
    """Report which OCR engine is available and how to enable it if not."""
    ok, detail = ocr_available()
    return (f"OCR engine : {'available' if ok else 'unavailable'}\n"
            f"detail     : {detail}\n"
            f"vision model in use: none — JARVIS reads screens as text (DOM + OCR)")


OCR_TOOLS = [read_screen_text, find_on_screen, ocr_report]
