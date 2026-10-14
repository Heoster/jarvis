"""Reading YouTube search results **without trusting the browser**.

``media_play`` used to depend entirely on the browser to see the results page:
it injected ``https://www.youtube.com/results?search_query=…``, ran a JS
snippet over the DOM and hoped the ``ytd-video-renderer`` nodes had hydrated
already.  Three things go wrong with that in the real world, and all three were
seen in the field:

* **the engine dies** — Playwright/Selenium fail to attach, and the harness
  degrades to an engine that can navigate but not read the page (or to
  screen-only, which has no DOM at all);
* **the page is not ready** — YouTube hydrates its results after
  ``domcontentloaded``, so reading once, immediately, finds nothing;
* **a consent interstitial** replaces the results entirely.

Search *results* however are plain data, and they can be read three other
ways that need no working DOM at all:

1. **``ytInitialData``** — the results are server-rendered into the HTML as one
   JSON blob.  A single HTTP GET with browser-ish headers (plus the consent
   cookie, so the interstitial never appears) plus a small recursive walk gives
   title, channel, duration and view count for every result.  Free, fast, no
   API key, no dependency beyond ``httpx`` (``urllib`` is used if httpx is
   missing).
2. **Reader text (Jina)** — ``r.jina.ai`` turns the same page into markdown.
   Already used by ``play_song``, and it works from behind proxies that block
   direct scraping, so it is the second fallback.
3. **The DOM** — still the first choice when a browser is alive, because the
   page is already open.  ``media.py`` polls it (see ``results_wait_seconds``)
   before falling back, and this module supplies the two network readers it
   falls back to.

Everything here is pure: strings in, list-of-dicts out, no I/O besides the two
explicit fetchers — which is what makes the whole path testable offline with
saved fixtures (see ``tests/test_youtube_results.py``).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote_plus

from ...utils.logger import get_logger

log = get_logger("tools.browser.youtube")

__all__ = [
    "YOUTUBE_SEARCH", "build_search_url", "http_get", "extract_initial_data",
    "videos_from_initial_data", "videos_from_reader_text", "search_via_http",
    "search_via_reader", "search", "duration_seconds",
]

YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query={query}&hl=en&gl=US"

#: A desktop Chrome UA plus the consent cookie: together they make YouTube
#: answer with server-rendered results instead of the "Before you continue"
#: interstitial (which has no ``ytInitialData`` with videos in it).
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CONSENT_COOKIE = "CONSENT=YES+cb.20220301-11-p0.en+FX+111; SOCS=CAISEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cookie": CONSENT_COOKIE,
}

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DURATION_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")


# ── http ────────────────────────────────────────────────────────────────────
def http_get(url: str, *, timeout: float = 12.0,
             headers: Optional[Mapping[str, str]] = None) -> str:
    """Fetch ``url`` and return its body as text (httpx, else stdlib urllib).

    Raises whatever the client raises — callers turn that into a readable
    message, because "the network is down" is a legitimate, reportable answer.
    """
    merged = {**HEADERS, **(headers or {})}
    try:
        import httpx
    except Exception:                                   # pragma: no cover - optional
        from urllib.request import Request, urlopen

        request = Request(url, headers=merged)
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - https
            return response.read().decode("utf-8", "replace")
    response = httpx.get(url, headers=merged, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.text


def build_search_url(query: str) -> str:
    """The results URL used for *scraping* (``hl``/``gl`` keep it English)."""
    return YOUTUBE_SEARCH.format(query=quote_plus((query or "").strip()))


# ── ytInitialData parsing ───────────────────────────────────────────────────
def _slice_js_string(text: str, start: int) -> Tuple[str, int]:
    """Return (unescaped body, index after) for a JS single-quoted string."""
    out: List[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            out.append(text[index:index + 2])
            index += 2
            continue
        if char == "'":
            break
        out.append(char)
        index += 1
    body = "".join(out)
    return (body.replace("\\'", "'").replace('\\"', '"')
                .replace("\\n", "\n").replace("\\\\", "\\")), index + 1


def extract_initial_data(html: str) -> Optional[Any]:
    """Pull YouTube's ``ytInitialData`` JSON blob out of a page's HTML.

    Handles both shapes YouTube ships (``var ytInitialData = {...};`` and
    ``ytInitialData = '{...}';``).  ``json.JSONDecoder.raw_decode`` does the
    heavy lifting, so string escaping inside the blob is handled properly
    instead of by brace counting.
    """
    if not html:
        return None
    match = re.search(r"ytInitialData\"?\s*[:=]\s*", html)
    if not match:
        return None
    position = match.end()
    try:
        if position < len(html) and html[position] == "'":
            body, _ = _slice_js_string(html, position)
            return json.loads(body)
        if position < len(html) and html[position] in "{[":
            return json.JSONDecoder().raw_decode(html, position)[0]
    except Exception as exc:
        log.debug(f"ytInitialData could not be decoded: {exc}")
        return None
    return None


def _text(node: Any) -> str:
    """YouTube titles are either ``{"simpleText": …}`` or ``{"runs": […]}``."""
    if isinstance(node, str):
        return node
    if isinstance(node, Mapping):
        if "simpleText" in node:
            return str(node.get("simpleText") or "")
        if "content" in node and isinstance(node.get("content"), str):
            return node["content"]
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(str(run.get("text") or "") for run in runs
                           if isinstance(run, Mapping))
    return ""


def _entry_from_renderer(renderer: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    video_id = str(renderer.get("videoId") or "").strip()
    if not _VIDEO_ID_RE.match(video_id):
        return None
    title = _text(renderer.get("title"))
    if not title:
        return None
    channel = (_text(renderer.get("ownerText"))
               or _text(renderer.get("longBylineText"))
               or _text(renderer.get("shortBylineText")))
    duration = (_text(renderer.get("lengthText"))
                or _text(renderer.get("thumbnailOverlayTimeStatusRenderer")))
    views = _text(renderer.get("viewCountText")) or _text(renderer.get("shortViewCountText"))
    published = _text(renderer.get("publishedTimeText"))
    return {"title": title.strip()[:200], "id": video_id,
            "href": f"https://www.youtube.com/watch?v={video_id}",
            "channel": channel.strip()[:80], "duration": duration.strip()[:16],
            "views": views.strip()[:40], "published": published.strip()[:40]}


#: node keys that hold one search result
_RENDERER_KEYS = ("videoRenderer", "compactVideoRenderer", "gridVideoRenderer",
                  "playlistVideoRenderer")


def videos_from_initial_data(data: Any, *, limit: int = 25) -> List[Dict[str, Any]]:
    """Walk the blob (it is deeply nested) and collect every video result."""
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, Mapping):
            for key in _RENDERER_KEYS:
                renderer = node.get(key)
                if isinstance(renderer, Mapping):
                    entry = _entry_from_renderer(renderer)
                    if entry and entry["id"] not in seen:
                        seen.add(entry["id"])
                        found.append(entry)
                        if len(found) >= limit:
                            return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
                if len(found) >= limit:
                    return

    walk(data)
    return found


# ── reader (markdown) parsing ───────────────────────────────────────────────
#: ``[Title](url)`` and the markdown form with a title attribute: ``[Title](url "Title")``
_MD_LINK_RE = re.compile(r"\[([^\]\n]{0,300})\]\((https?://[^\s)]+)[^)]*\)")


def _video_id_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        path = parsed.path.rstrip("/")
        if path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif path.startswith("/shorts/"):
            candidate = path.split("/")[2] if len(path.split("/")) > 2 else ""
        elif path.startswith("/embed/"):
            candidate = path.split("/")[2] if len(path.split("/")) > 2 else ""
        else:
            candidate = ""
    else:
        candidate = ""
    return candidate if _VIDEO_ID_RE.match(candidate) else ""


def duration_seconds(text: str) -> float:
    """``"1:03:31"`` → 3811.0, ``"3:02"`` → 182.0, junk → 0.0."""
    match = _DURATION_RE.search(text or "")
    if not match:
        return 0.0
    parts = [int(part) for part in match.group(1).split(":")]
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total


def videos_from_reader_text(text: str, *, limit: int = 25) -> List[Dict[str, Any]]:
    """Parse reader/Jina markdown of a results page into the same entries.

    The markdown keeps YouTube's own ranking order, links look like
    ``### [Title](https://www.youtube.com/watch?v=ID "Title")`` and the duration
    sits on the thumbnail line just above, so the parse is line-local: title
    from the link text, duration from the nearest ``M:SS`` token, channel from
    the line below.  Anything it cannot find is simply left empty — the entry
    is still playable, which is the whole point.
    """
    if not text:
        return []
    lines = (text or "").splitlines()
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()
    pending_duration = ""
    for index, line in enumerate(lines):
        for title, url in _MD_LINK_RE.findall(line):
            if len(found) >= limit:
                return found
            video_id = _video_id_from_url(url)
            if not video_id or video_id in seen:
                continue
            label = title.strip()
            if label.startswith("!") or label.lower().startswith("image"):
                continue                                  # thumbnail link, not a title
            window = "\n".join(lines[max(0, index - 2):index + 1])
            duration = ""
            match = _DURATION_RE.search(window)
            if match:
                duration = match.group(1)
            elif pending_duration:
                duration = pending_duration
            channel = ""
            for follower in lines[index + 1:index + 6]:
                follower = follower.strip().strip("•·|")
                if not follower or follower.startswith(("[", "![", "#", "-")):
                    continue
                if _DURATION_RE.fullmatch(follower) or re.fullmatch(
                        r"[\d.,]+[KMB]? views?", follower, re.I):
                    continue
                if len(follower) > 80:
                    continue
                channel = follower
                break
            seen.add(video_id)
            found.append({"title": label[:200], "id": video_id,
                          "href": f"https://www.youtube.com/watch?v={video_id}",
                          "channel": channel[:80], "duration": duration[:16],
                          "views": "", "published": ""})
        if not _MD_LINK_RE.search(line):
            match = _DURATION_RE.search(line)
            if match:
                pending_duration = match.group(1)
    return _add_bare_links(found, text, limit=limit, seen=seen)


_BARE_URL_RE = re.compile(r"https?://[^\s)\]}\"'<>]+")


def _add_bare_links(found: List[Dict[str, Any]], text: str, *, limit: int,
                    seen: set) -> List[Dict[str, Any]]:
    """Second pass: reader markdown nests the watch URL inside a thumbnail
    link, so catch any video that only appears as a bare URL (untitled)."""
    for url in _BARE_URL_RE.findall(text or ""):
        if len(found) >= limit:
            break
        video_id = _video_id_from_url(url.rstrip(".,;"))
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        found.append({"title": "", "id": video_id,
                      "href": f"https://www.youtube.com/watch?v={video_id}",
                      "channel": "", "duration": "", "views": "", "published": ""})
    return found


# ── the two network readers ─────────────────────────────────────────────────
def search_via_http(query: str, *, limit: int = 25, timeout: float = 12.0) -> List[Dict[str, Any]]:
    """GET the results page and read ``ytInitialData`` — no browser, no key."""
    html = http_get(build_search_url(query), timeout=timeout)
    data = extract_initial_data(html)
    if data is None:
        raise ValueError("no ytInitialData in the results page "
                         "(consent wall, bot check or empty response)")
    entries = videos_from_initial_data(data, limit=limit)
    if not entries:
        raise ValueError("ytInitialData parsed but held no video results")
    return entries


def search_via_reader(query: str, *, limit: int = 25, timeout: float = 25.0,
                      reader: Any = None) -> List[Dict[str, Any]]:
    """Read the results page as markdown (Jina by default) and parse it."""
    if reader is None:
        from ..crawl.jina_reader import jina_read

        text = jina_read(build_search_url(query), timeout=timeout, max_chars=120000)
    else:
        text = reader(build_search_url(query))
    entries = videos_from_reader_text(text, limit=limit)
    if not entries:
        raise ValueError("the reader returned the page but no video links were in it")
    return entries


def search(query: str, *, limit: int = 25, timeout: float = 12.0,
           readers: Sequence[str] = ("http", "reader"),
           reader: Any = None) -> Tuple[List[Dict[str, Any]], str, str]:
    """Run the readers in order: ``(entries, reader_that_worked, why_they_failed)``.

    ``reader`` injects a callable for tests (and for ``read_url``-style chains);
    production leaves it ``None`` and Jina is used.  On total failure the second
    element is ``""`` and the third explains what each reader said — that string
    is what ends up in the tool's error, so a dead network is never mistaken
    for "no results".
    """
    errors: List[str] = []
    for name in readers:
        try:
            if name == "http":
                entries = search_via_http(query, limit=limit, timeout=timeout)
            elif name == "reader":
                entries = search_via_reader(query, limit=limit,
                                            timeout=max(timeout, 20.0), reader=reader)
            else:                                        # pragma: no cover - misconfig
                raise ValueError(f"unknown results reader {name!r}")
        except Exception as exc:
            errors.append(f"{name}: {truncate(str(exc), 140)}")
            log.info(f"youtube results via {name} failed: {exc}")
            continue
        if entries:
            return entries, name, ""
    return [], "", " | ".join(errors) or "no reader was tried"


def truncate(text: str, size: int) -> str:
    text = str(text or "")
    return text if len(text) <= size else text[:size - 1] + "…"
