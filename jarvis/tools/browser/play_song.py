"""play_song — find a song, read the results with Jina, open the first trusted video.

The pipeline is fixed in code, so a small model only has to pass the song name:

1. build the YouTube search URL from the query;
2. fetch that results page as text through Jina Reader (no browser needed);
3. take the first link, in search-rank order, that is a real video on a trusted host;
4. open that video in Chrome with ``browse_open`` and report exactly what was opened.

Jina is the first choice because it always works from behind a proxy, but it is
rate limited on the free tier (HTTP 429), so if it fails JARVIS reads the very
same results page directly and parses YouTube's ``ytInitialData`` — the pick is
then scored the way ``media_play`` scores it, instead of "first link wins".

It never claims the song is playing: it reports the URL it opened. Use
``media_play`` when you need the player state checked.
"""

from __future__ import annotations

import asyncio
import re
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlparse

from ..base import failure, tool
from ..crawl.jina_reader import CrawlError, jina_read
from .actions import browse_open

__all__ = ["PLAY_TOOLS", "youtube_search_url", "trusted_video_links", "pick_first_trusted"]

#: hosts whose watch pages count as a trusted video for a song request
TRUSTED_VIDEO_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

_URL_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")


def youtube_search_url(query: str) -> str:
    """The YouTube results page for ``query``."""
    return "https://www.youtube.com/results?search_query=" + quote_plus(query.strip())


def _canonical_video(url: str) -> Optional[str]:
    """``https://www.youtube.com/watch?v=ID`` for a trusted watch URL, else None."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {h.removeprefix("www.") for h in TRUSTED_VIDEO_HOSTS}:
        return None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif parsed.path.rstrip("/") == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        return None  # channels, playlists, shorts-less pages, ads and search pages
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id or ""):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def trusted_video_links(text: str) -> List[Tuple[str, str]]:
    """Every trusted video in ``text`` as (title, url), in document order, de-duplicated."""
    titles = {}
    for title, url in _URL_RE.findall(text or ""):
        titles.setdefault(url, title.strip())
    found: List[Tuple[str, str]] = []
    seen = set()
    for raw in _BARE_URL_RE.findall(text or ""):
        canonical = _canonical_video(raw.rstrip(".,;"))
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        title = next((t for u, t in titles.items() if _canonical_video(u) == canonical), "")
        found.append((title or canonical, canonical))
    return found


def pick_first_trusted(text: str) -> Optional[Tuple[str, str]]:
    """The first trusted video (title, url) in search-rank order, or None."""
    links = trusted_video_links(text)
    return links[0] if links else None


@tool(category="browser", timeout=150)
async def play_song(query: str, new_tab: bool = True) -> str:
    """Play a song or video: search YouTube, fetch the results with Jina, open the first trusted video.

    WHEN: the operator asks to play, listen to, bajao/chalao/sunao a song, a
    video or a music track ("play pasuri", "play a random song", "song X on
    youtube"). Use this FIRST for playing music. Do not type into YouTube.
    HOW: pass only the song or artist name as ``query`` (e.g. "pasuri onumula").
    Do not add "on youtube" or "play" to it. The result names the video that
    was opened; report that title and URL, not a guess.

    Args:
        query: Song, artist or video name to search for
        new_tab: Open the video in a new tab (default true)
    """
    from .media import choose_candidate
    from .youtube_results import search as network_search

    query = (query or "").strip()
    if not query:
        return failure("play_song needs a song or video name in `query`.")
    search_url = youtube_search_url(query)
    via = f"Jina Reader ({search_url})"
    picked: Optional[Tuple[str, str]] = None
    try:
        results_text = await asyncio.to_thread(jina_read, search_url, max_chars=60000)
    except CrawlError as exc:
        # Jina is rate limited on the free tier: read the same page ourselves
        # (ytInitialData is server-rendered, so a plain GET is enough).
        entries, source, why = await asyncio.to_thread(
            network_search, query, limit=25, readers=("http",))
        if not entries:
            return failure(f"Jina could not fetch the results for “{query}”: {exc}; "
                           f"the direct reader failed too ({why}). "
                           f"Open {search_url} with browse_open instead.")
        best = choose_candidate(entries, query) or entries[0]
        picked = (str(best.get("title") or best.get("id") or ""),
                  str(best.get("href") or ""))
        via = f"YouTube's own results data ({source})"
    if picked is None:
        picked = pick_first_trusted(results_text)
    if picked is None:
        return failure(f"Jina fetched the results for “{query}” but found no trusted video "
                       "link. Try a more specific name, or browse_open the search page.")
    title, url = picked
    if not url:
        return failure(f"the results for “{query}” had no playable video link.")
    opened = await browse_open.func(url=url, new_tab=new_tab, read_page=True)
    if isinstance(opened, tuple):  # `failure` helper result
        return failure(f"found “{title}” at {url} but could not open it: {opened[1]}")
    return (f"🎯 First trusted result for “{query}”: {title}\n"
            f"🔗 {url}\n"
            f"(search read via {via})\n\n{opened}")


PLAY_TOOLS = [play_song]
