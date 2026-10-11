"""Image utilities: resize, convert, describe, OCR.

"Describe" here means *text extraction and metadata*, never a vision model — the
project constraint is explicit.  For anything semantic about an image, JARVIS
says so plainly instead of hallucinating a caption.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from ...utils.logger import get_logger
from ..base import tool

log = get_logger("utility.image")

__all__ = ["image_info", "resize_image", "convert_image", "IMAGE_TOOLS"]


def _open(path: str):
    from PIL import Image  # type: ignore

    return Image.open(Path(path).expanduser())


@tool(category="utility", timeout=45)
def image_info(path: str) -> str:
    """Show an image's dimensions, format and EXIF summary (no vision model used).

    Args:
        path: Path to the image file
    """
    target = Path(path).expanduser()
    if not target.exists():
        return f"File not found: {target}"
    try:
        with _open(str(target)) as image:
            lines = [f"**{target.name}**",
                     f"format     : {image.format or 'unknown'}",
                     f"size       : {image.width}×{image.height} px",
                     f"mode       : {image.mode}",
                     f"bytes      : {target.stat().st_size:,}"]
            exif = getattr(image, "getexif", lambda: {})()
            if exif:
                interesting = {36867: "taken at", 272: "camera", 305: "software",
                               34853: "gps"}
                for key, label in interesting.items():
                    value = exif.get(key)
                    if value:
                        lines.append(f"{label:<11}: {value}")
    except Exception as exc:
        return f"Cannot read image: {exc}"
    # a little OCR goes a long way
    try:
        from ..screen.ocr_reader import ocr_available, read_image_text

        available, _detail = ocr_available()
        if available:
            text = read_image_text(str(target))
            if text and len(text.strip()) > 3:
                lines.append("\ntext found in image (OCR):\n" + text[:1200])
    except Exception:
        pass
    lines.append("\n_(no vision model is used — this is metadata + OCR only)_")
    return "\n".join(lines)


@tool(category="utility", timeout=60)
def resize_image(path: str, width: int = 0, height: int = 0,
                 output: str = "", percent: float = 0) -> str:
    """Resize an image (keeps aspect ratio when only one dimension is given).

    Args:
        path: Source image
        width: Target width in pixels (0 = derive from height)
        height: Target height in pixels (0 = derive from width)
        output: Output path (defaults to <name>_resized.<ext>)
        percent: Scale by percentage instead (e.g. 50 for half size)
    """
    source = Path(path).expanduser()
    if not source.exists():
        return f"File not found: {source}"
    try:
        with _open(str(source)) as image:
            original = image.size
            if percent:
                size = (max(1, int(image.width * percent / 100)),
                        max(1, int(image.height * percent / 100)))
            elif width and height:
                size = (width, height)
            elif width:
                size = (width, max(1, int(image.height * width / image.width)))
            elif height:
                size = (max(1, int(image.width * height / image.height)), height)
            else:
                return "Give width, height or percent."
            resized = image.resize(size)
            target = Path(output).expanduser() if output else \
                source.with_name(f"{source.stem}_resized{source.suffix}")
            resized.save(target)
    except Exception as exc:
        return f"Resize failed: {exc}"
    return f"✅ {original} → {size} saved to {target}"


@tool(category="utility", timeout=60)
def convert_image(path: str, format: str = "png", output: str = "") -> str:
    """Convert an image to another format (png, jpg, webp, bmp, pdf).

    Args:
        path: Source image
        format: Target format
        output: Optional output path
    """
    source = Path(path).expanduser()
    if not source.exists():
        return f"File not found: {source}"
    fmt = format.lower().lstrip(".")
    try:
        with _open(str(source)) as image:
            target = Path(output).expanduser() if output else \
                source.with_suffix(f".{fmt}")
            if fmt in ("jpg", "jpeg") and image.mode in ("RGBA", "P"):
                image = image.convert("RGB")
            image.save(target, format=fmt.upper() if fmt != "jpg" else "JPEG")
    except Exception as exc:
        return f"Conversion failed: {exc}"
    return f"✅ Converted to {target}"


IMAGE_TOOLS = [image_info, resize_image, convert_image]
