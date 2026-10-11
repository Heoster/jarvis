"""Local reader: stdlib HTML → text, upgraded to trafilatura when installed.

This is the "no network service" leg of the crawl chain, and it is also what
keeps JARVIS usable offline: ``html.parser`` is always present, trafilatura is
a bonus that removes boilerplate much better.
"""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser
from typing import List, Optional

import httpx

from ..base import tool
from ...utils.logger import get_logger
from .jina_reader import CrawlError

log = get_logger("tools.crawl.local")

_SKIP = {"script", "style", "noscript", "svg", "path", "template", "head"}
_BLOCK = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3",
          "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol"}
_WS = re.compile(r"[ \t]{2,}")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self.parts: List[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            data = dict(attrs)
            key = data.get("name") or data.get("property")
            if key and data.get("content"):
                self.meta[key] = data["content"]
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        text = html_lib.unescape(data)
        if self._in_title:
            self.title += text.strip() + " "
            return
        if text.strip():
            self.parts.append(text)

    # ── public ────────────────────────────────────────────────────────────
    def text(self) -> str:
        body = "".join(self.parts)
        body = _WS.sub(" ", body)
        body = _BLANKS.sub("\n\n", body)
        return body.strip()


def extract_local(html: str, *, max_chars: int = 12000) -> str:
    """Pure-stdlib readable-text extraction (works with zero dependencies)."""
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as exc:  # malformed markup must not kill the turn
        log.debug(f"local extractor hiccup: {exc}")
    return parser.text()[:max_chars]


def extract_best(html: str, *, url: str = "", max_chars: int = 12000) -> str:
    """trafilatura when available, stdlib fallback otherwise."""
    try:
        import trafilatura  # noqa: WPS433

        out = trafilatura.extract(html, url=url or None, include_comments=False,
                                  include_tables=True, favor_precision=True)
        if out and len(out.strip()) >= 120:
            return out.strip()[:max_chars]
    except ImportError:
        pass
    except Exception as exc:
        log.debug(f"trafilatura failed ({exc}); falling back to stdlib")
    return extract_local(html, max_chars=max_chars)


def fetch_and_extract(url: str, *, timeout: float = 20.0,
                      max_chars: int = 12000) -> str:
    """Plain HTTP GET + best-effort extraction (no external reader service)."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; "
                                                    "JARVIS/1.0; +local)"})
    except Exception as exc:
        raise CrawlError(f"direct fetch failed: {exc}") from exc
    if response.status_code >= 400:
        raise CrawlError(f"direct fetch HTTP {response.status_code}")
    text = extract_best(response.text, url=url, max_chars=max_chars)
    if len(text) < 120:
        raise CrawlError("page had almost no readable text (JS-only page?)")
    return text


# ── tool ────────────────────────────────────────────────────────────────
@tool(category="crawl", timeout=45)
def local_read_url(url: str, max_chars: int = 12000) -> str:
    """Fetch a page directly and extract text locally (no third-party service).

    Uses trafilatura when installed, otherwise a dependency-free stdlib
    extractor — this is the reader that still works with no API keys at all.

    Args:
        url: Page to read
        max_chars: Truncate at this many characters
    """
    try:
        text = fetch_and_extract(url, max_chars=max_chars)
    except CrawlError as exc:
        return (f"Local reader could not fetch {url}: {exc}\n"
                "This is expected for JavaScript-only pages — try `jina_read_url` "
                "or `browse_open` (real browser) instead.")
    engine = "trafilatura" if _has_trafilatura() else "stdlib extractor"
    return f"**{url}**\n\n{text}\n\n_(via {engine} · {len(text)} chars)_"


def _has_trafilatura() -> bool:
    try:
        import trafilatura  # noqa: F401,WPS433

        return True
    except Exception:
        return False
