"""Media: play music/video on the web, and remember what was played.

This is the concrete "play my favourite song" workflow, and it is deliberately
built out of *verifiable* steps rather than wishful clicking:

1. **Resolve what to play.** A concrete title/URL wins. Otherwise the operator's
   favourites and the play history are consulted (``media_history``), so
   "play my favourite song" or "play yesterday's song" works without repeating
   the name. Nothing found → the tool says so instead of guessing.
2. **Search by URL injection.** ``https://www.youtube.com/results?search_query=…``
   is opened directly — no typing needed, which makes it reliable on slow
   machines — and typing into the search box remains the fallback path
   (``page_type`` / ``page_press``) when a site has no query parameter.
3. **Verify the result before clicking.** The results page is parsed with a
   plain-JS reader; the chosen entry is reported (title, channel, duration,
   href) so the log shows *why* that item was picked.
4. **Play and verify playback.** The video is opened, then the page's own
   ``<video>`` element is inspected (``paused``, ``currentTime``, ``duration``,
   ``muted``, ``readyState``). If it is paused — autoplay policies are real —
   JARVIS presses ``k`` (YouTube's play/pause key) and re-checks. The tool
   reports the true state; it never claims "playing" if the element says paused.
5. **Remember it.** Every successful play is written to ``data/media_history.json``
   and mirrored into long-term memory, which is what makes "what did I play
   yesterday?" answerable next session.

All of it free: no API key, no download, just the browser JARVIS already drives.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...config import get as cfg_get
from ...utils.logger import get_logger
from ...utils.sanitizer import truncate
from ..base import failure, tool

log = get_logger("tools.browser.media")

HISTORY_FILE = "media_history.json"
YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query={query}"
YOUTUBE_HOME = "https://www.youtube.com/"
MAX_HISTORY = 500

#: How the results were read — reported back, so "it picked the wrong video"
#: is always traceable to a reader rather than a guess.
READER_LABELS = {"dom": "the open page (DOM)", "http": "the results page over HTTP "
                 "(ytInitialData, no browser needed)",
                 "reader": "Jina Reader (r.jina.ai)", "snapshot": "the DOM snapshot"}

#: Polling the results page: YouTube hydrates ``ytd-video-renderer`` *after*
#: ``domcontentloaded``, so one read taken straight after ``goto`` usually sees
#: an empty page.  Both knobs are module-level so tests can shorten them.
RESULTS_POLL_ATTEMPTS = 6
RESULTS_POLL_DELAY = 0.7

#: JS that turns a results (or watch) page into plain data — no vision needed
RESULTS_JS = """(() => {
  const out = [];
  const seen = new Set();
  for (const node of document.querySelectorAll('a#video-title, a#video-title-link, ytd-video-renderer a#thumbnail, a.yt-simple-endpoint[href*="watch"]')) {
    const href = node.href || node.getAttribute('href') || '';
    const m = href.match(/v=([\\w-]{6,})/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const holder = node.closest('ytd-video-renderer, ytd-rich-item-renderer, ytd-compact-video-renderer') || node;
    const title = (node.getAttribute('title') || node.textContent || '').trim();
    const channel = (holder.querySelector('ytd-channel-name a, #channel-name a') || {}).textContent || '';
    const dur = (holder.querySelector('span.ytd-thumbnail-overlay-time-status-renderer, #text.ytd-thumbnail-overlay-time-status-renderer') || {}).textContent || '';
    out.push({title: title.slice(0, 200), id: m[1], href: 'https://www.youtube.com/watch?v=' + m[1],
              channel: channel.trim().slice(0, 80), duration: dur.trim().slice(0, 12)});
    if (out.length >= 25) break;
  }
  return out;
})()"""

#: Is the page even a results page yet?  Used to decide whether waiting can
#: still help (page loading) or is hopeless (consent wall, error page).
RESULTS_READY_JS = """(() => {
  const host = location.hostname || '';
  return {
    consent: host.includes('consent.youtube.com'),
    renderers: document.querySelectorAll('ytd-video-renderer').length,
    titles: document.querySelectorAll('a#video-title').length,
    ready: document.readyState,
    url: location.href
  };
})()"""

#: Click through YouTube's "Before you continue" wall if we landed on it.
CONSENT_JS = """(() => {
  const accept = /^(accept all|i agree|agree|accept)/i;
  const nodes = Array.from(document.querySelectorAll('button, a, .VfPpkd-LgbsSe'));
  const hit = nodes.find(node => accept.test((node.textContent || node.value || '').trim()));
  if (!hit) return {clicked: false};
  hit.click();
  return {clicked: true};
})()"""

PLAY_JS = """(() => {
  const v = document.querySelector('video');
  if (!v) return {ok: false, reason: 'no video element'};
  try { v.play(); } catch (e) { return {ok: false, reason: String(e)}; }
  return {ok: true, paused: v.paused, currentTime: v.currentTime};
})()"""

PLAYER_JS = """(() => {
  const v = document.querySelector('video');
  const playing = document.querySelector('#movie_player.playing-mode, .html5-video-player.playing-mode');
  let state = 'none';
  try {
    const el = document.querySelector('#movie_player') || document.querySelector('ytd-player');
    if (el && typeof el.getPlayerState === 'function') state = el.getPlayerState();
  } catch (e) { state = 'unknown'; }
  return {
    title: (document.querySelector('h1.ytd-watch-metadata yt-formatted-string') || document.querySelector('h1') || {}).textContent || document.title,
    hasVideo: !!v, paused: v ? v.paused : null, muted: v ? v.muted : null,
    currentTime: v ? v.currentTime : null, duration: v ? v.duration : null,
    readyState: v ? v.readyState : null, playerState: state,
    playing: playing ? true : (v ? !v.paused : false),
    url: location.href
  };
})()"""


# ── history ─────────────────────────────────────────────────────────────────
@dataclass
class Play:
    """One thing that was played."""

    title: str
    url: str
    at: str
    query: str = ""
    source: str = "youtube"
    channel: str = ""
    seconds: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "url": self.url, "at": self.at, "query": self.query,
                "source": self.source, "channel": self.channel,
                "seconds": round(self.seconds, 1)}

    def line(self) -> str:
        when = self.at[:16].replace("T", " ")
        bits = [f"{when} · {truncate(self.title, 70)}"]
        if self.channel:
            bits.append(f"({self.channel})")
        if self.query:
            bits.append(f"· for “{truncate(self.query, 40)}”")
        return " ".join(bits)


class MediaHistory:
    """``data/media_history.json`` — plays and favourites, plain JSON."""

    def __init__(self, path: Optional[str | Path] = None,
                 config: Optional[Mapping[str, Any]] = None):
        from ...config import get as cfg_get
        from ...config import load_config

        config = config or load_config()
        default = Path(__file__).resolve().parents[3] / "data" / HISTORY_FILE
        target = path or cfg_get(config, "browser.media.history_file", "") or default
        self.path = Path(str(target)).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = {"plays": [], "favourites": []}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._data.update(data)
        except Exception as exc:
            log.warning(f"could not read media history {self.path}: {exc}")

    def save(self) -> None:
        self._data["plays"] = list(self._data.get("plays") or [])[-MAX_HISTORY:]
        self.path.write_text(json.dumps(self._data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    # ── api ────────────────────────────────────────────────────────────────
    def record(self, title: str, url: str, *, query: str = "", channel: str = "",
               source: str = "youtube", seconds: float = 0.0,
               when: Optional[datetime] = None) -> Play:
        play = Play(title=str(title).strip()[:200], url=str(url)[:500],
                    at=(when or datetime.now()).isoformat(timespec="seconds"),
                    query=str(query)[:200], source=source, channel=str(channel)[:80],
                    seconds=float(seconds or 0.0))
        self._data.setdefault("plays", []).append(play.as_dict())
        self.save()
        return play

    def plays(self, *, limit: int = 20, day: Optional[date] = None,
              since: Optional[date] = None, query: str = "",
              source: str = "") -> List[Play]:
        rows: List[Play] = []
        needle = query.strip().lower()
        for entry in reversed(self._data.get("plays") or []):
            stamp = str(entry.get("at") or "")
            if day is not None and not stamp.startswith(day.isoformat()):
                continue
            if since is not None and stamp[:10] < since.isoformat():
                continue
            if source and str(entry.get("source") or "") != source:
                continue
            if needle:
                haystack = " ".join(str(entry.get(key) or "") for key in
                                    ("title", "query", "channel", "url")).lower()
                if needle not in haystack:
                    continue
            rows.append(Play(title=str(entry.get("title") or ""),
                             url=str(entry.get("url") or ""), at=stamp,
                             query=str(entry.get("query") or ""),
                             source=str(entry.get("source") or ""),
                             channel=str(entry.get("channel") or ""),
                             seconds=float(entry.get("seconds") or 0.0)))
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def last(self, *, day: Optional[date] = None, query: str = "",
             exclude: str = "") -> Optional[Play]:
        for play in self.plays(limit=50, day=day, query=query):
            if exclude and play.title.strip().lower() == exclude.strip().lower():
                continue
            return play
        return None

    def counts(self) -> Dict[str, Any]:
        plays = list(self._data.get("plays") or [])
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        return {"total": len(plays), "today": sum(1 for p in plays
                                                  if str(p.get("at") or "").startswith(today)),
                "yesterday": sum(1 for p in plays
                                 if str(p.get("at") or "").startswith(yesterday)),
                "favourites": len(self.favourites()), "path": str(self.path)}

    # ── favourites ─────────────────────────────────────────────────────────
    def favourites(self) -> List[str]:
        return [str(item) for item in (self._data.get("favourites") or [])]

    def set_favourite(self, song: str, *, remove: bool = False) -> List[str]:
        song = str(song).strip()
        current = self.favourites()
        if remove:
            current = [item for item in current if item.lower() != song.lower()]
        elif song and song.lower() not in [item.lower() for item in current]:
            current.insert(0, song)
        self._data["favourites"] = current[:20]
        self.save()
        return self._data["favourites"]


# ── choosing what to play ───────────────────────────────────────────────────
def build_search_url(query: str, *, engine: str = "youtube") -> str:
    """URL injection: turn a query into a results URL (no typing required)."""
    query = (query or "").strip()
    if not query:
        raise ValueError("a search needs a query")
    if engine == "youtube":
        return YOUTUBE_SEARCH.format(query=urllib.parse.quote_plus(query))
    if engine == "google":
        return ("https://www.google.com/search?q="
                + urllib.parse.quote_plus(query))
    if engine == "spotify":
        return "https://open.spotify.com/search/" + urllib.parse.quote(query)
    raise ValueError(f"unknown media engine {engine!r}")


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


FAVOURITE_PHRASES = ("favourite", "favorite", "fav song", "my fav", "pasandida",
                     "pasandeeda", "pasand")


def wants_a_favourite(text: str) -> bool:
    """“play my favourite song” — a *request for* a favourite, not a title."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in FAVOURITE_PHRASES)


def resolve_request(requested: str, history: "MediaHistory") -> Tuple[str, str]:
    """Decide what to actually search for; returns ``(query, where_it_came_from)``.

    Order: an explicit title/URL → a favourite the text matches → a favourite
    asked for by intent (“my favourite song”) → the last play → nothing.
    """
    requested = (requested or "").strip()
    favourites = history.favourites()
    if requested:
        lowered = requested.lower()
        for favourite in favourites:                     # “play kesariya” → full title
            if lowered in favourite.lower() or favourite.lower() in lowered:
                if lowered != favourite.lower():
                    return favourite, "favourites"
        if wants_a_favourite(requested):
            if favourites:
                return favourites[0], "favourites"
            last = history.last()                    # no favourites yet → yesterday's song
            if last is not None:
                return last.title, "last play"
        return requested, "argument"                 # an explicit title wins as-is
    if favourites:
        return favourites[0], "favourites"
    last = history.last()
    if last is not None:
        return last.title, "last play"
    return "", ""


def looks_like_url(text: str) -> bool:
    return bool(re.match(r"^https?://", (text or "").strip(), re.I))


def fold_word(word: str) -> str:
    """Transliteration-tolerant folding: ``taare`` → ``tare``, ``kyaaa`` → ``kya``.

    Song names reach JARVIS as they were *said*, so Hindi/Urdu titles rarely
    match letter for letter ("tare" vs "taare", "pasuri" vs "pasoori").
    Collapsing repeated letters makes those match without any fuzzy library.
    """
    folded: List[str] = []
    for char in str(word or "").lower():
        if not folded or folded[-1] != char:
            folded.append(char)
    return "".join(folded)


def duration_penalty(entry: Mapping[str, Any]) -> float:
    """Loops, compilations and 40-second clips are almost never the real song."""
    from .youtube_results import duration_seconds

    seconds = duration_seconds(str(entry.get("duration") or ""))
    title = str(entry.get("title") or "").lower()
    penalty = 0.0
    if seconds:
        if seconds < 60:                                  # a clip, a teaser, a short
            penalty -= 10
        elif seconds < 120:
            penalty -= 6
        elif seconds > 900:                               # 15 min+: loop or compilation
            penalty -= 22
    if any(word in title for word in ("loop", "hour", "compilation", "full album",
                                      "jukebox", "nonstop", "1 hour")):
        penalty -= 15
    return penalty


def score_candidate(entry: Mapping[str, Any], query: str) -> float:
    """How well a result matches what was asked for (0-100)."""
    title = str(entry.get("title") or "")
    want = {word for word in re.findall(r"[a-z0-9]+", (query or "").lower())
            if len(word) > 2}
    have = {word for word in re.findall(r"[a-z0-9]+", title.lower()) if len(word) > 2}
    if not want:
        return 0.0
    overlap = len(want & have) / len(want)
    if overlap < 1.0:                       # retry with folded spellings
        folded_want = {fold_word(word) for word in want}
        folded_have = {fold_word(word) for word in have}
        if folded_want:
            overlap = max(overlap, len(folded_want & folded_have) / len(folded_want))
    score = overlap * 80.0
    lowered = title.lower()
    if "official" in lowered:
        score += 6
    if any(word in lowered for word in ("lyrics", "audio", "song", "video")):
        score += 4
    if any(word in lowered for word in ("reaction", "cover", "remix", "live at",
                                        "slowed", "8d audio")) and not \
            any(word in (query or "").lower() for word in ("cover", "remix", "live")):
        score -= 25
    duration = str(entry.get("duration") or "")
    match = re.match(r"^(\d+):(\d{2})$", duration or "")
    if match and int(match.group(1)) < 2:                 # 1-2 minute clips are rarely songs
        score -= 8
    score += duration_penalty(entry)
    return max(0.0, round(score, 2))


def choose_candidate(entries: Sequence[Mapping[str, Any]], query: str) -> Optional[Mapping[str, Any]]:
    """Pick the result to play: best match wins, ties go to YouTube's order."""
    if not entries:
        return None
    scored = [(score_candidate(entry, query), index, entry)
              for index, entry in enumerate(entries)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    best = scored[0]
    log.info(f"media: chose {truncate(str(best[2].get('title')), 60)!r} "
             f"score={best[0]} of {len(entries)} candidates")
    return best[2]


def parse_player_state(state: Mapping[str, Any] | None) -> Dict[str, Any]:
    """Normalise the browser's report into a verdict JARVIS can trust."""
    state = dict(state or {})
    has_video = bool(state.get("hasVideo"))
    paused = state.get("paused")
    current = float(state.get("currentTime") or 0.0)
    duration = float(state.get("duration") or 0.0)
    playing = bool(state.get("playing")) or (has_video and paused is False)
    verdict: Dict[str, Any] = {
        "has_video": has_video, "playing": playing, "paused": paused,
        "title": str(state.get("title") or "")[:200],
        "url": str(state.get("url") or ""),
        "position": round(current, 1), "duration": round(duration, 1),
        "muted": state.get("muted"), "ready_state": state.get("readyState"),
    }
    if not has_video:
        verdict["reason"] = "no <video> element on the page (a preview, an ad, or not a player)"
    elif duration and current >= duration - 1:
        verdict["reason"] = "the video is already at the end"
    elif playing:
        verdict["reason"] = f"playing at {verdict['position']}s"
    else:
        verdict["reason"] = "the player is paused (autoplay was blocked)"
    return verdict


# ── tools ───────────────────────────────────────────────────────────────────
def _harness(config: Optional[Mapping[str, Any]] = None):
    from ...config import load_config
    from .harness import get_harness

    return get_harness(config or load_config())


def _history(config: Optional[Mapping[str, Any]] = None) -> MediaHistory:
    return MediaHistory(config=config)


def _coerce_entries(data: Any) -> List[Dict[str, Any]]:
    """Normalise whatever the browser returned into result rows."""
    if isinstance(data, dict) and "result" in data:        # json-stringified reply
        data = data.get("result")
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        data = data.get("entries")
    if not isinstance(data, list):
        return []
    rows: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        video_id = str(row.get("id") or row.get("videoId") or "").strip()
        if not video_id:
            match = re.search(r"v=([\w-]{6,})", str(row.get("href") or ""))
            video_id = match.group(1) if match else ""
        if not video_id:
            continue
        rows.append({"title": str(row.get("title") or "")[:200], "id": video_id,
                     "href": str(row.get("href") or youtube_watch_url(video_id)),
                     "channel": str(row.get("channel") or "")[:80],
                     "duration": str(row.get("duration") or "")[:16],
                     "views": str(row.get("views") or "")[:40]})
    return rows


async def _dismiss_consent(harness) -> bool:
    """Click YouTube's consent wall if the page is stuck on it."""
    try:
        result = await harness.evaluate(CONSENT_JS)
    except Exception:
        return False
    if isinstance(result, dict) and result.get("clicked"):
        log.info("media: dismissed the YouTube consent wall")
        await asyncio.sleep(1.5)
        return True
    return False


async def _page_ready(harness) -> Dict[str, Any]:
    """What the page looks like right now (used to decide whether to wait)."""
    try:
        raw = await harness.evaluate(RESULTS_READY_JS)
    except Exception:
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("result"), dict):
        return raw["result"]
    return raw if isinstance(raw, dict) else {}


async def _read_results(harness, *, query: str = "", wait: float = 6.0,
                        allow_network: bool = True, limit: int = 25,
                        readers: Sequence[str] = ("http", "reader")) -> Tuple[
                            List[Dict[str, Any]], str, str]:
    """Read the results page, and fall back to reading it *without* the browser.

    Order of attempts:

    1. **the DOM** — polled up to ``wait`` seconds, because YouTube renders its
       results after ``domcontentloaded`` (one immediate read is the bug that
       produced "opened … but found no video results");
    2. **the consent wall** — clicked through once if the page is stuck on it;
    3. **``ytInitialData`` over HTTP** — the results are server-rendered into
       the HTML, so a plain GET reads them even when no engine can see the DOM
       (this is what saves the run when Playwright and Selenium both degrade);
    4. **Jina Reader** — same page, fetched as markdown;
    5. **the DOM snapshot** — the old fallback, kept for engines that can read
       the page but not run JS.

    Returns ``(entries, reader, detail)``: ``reader`` is one of ``dom``,
    ``http``, ``reader``, ``snapshot`` or ``""`` and ``detail`` explains why
    every attempt failed, so the tool can report the real cause.
    """
    deadline = time.monotonic() + max(0.0, float(wait or 0.0))
    attempts = max(1, int(RESULTS_POLL_ATTEMPTS))
    delay = max(0.2, float(RESULTS_POLL_DELAY))
    notes: List[str] = []

    for attempt in range(1, attempts + 1):
        try:
            data = await harness.evaluate(RESULTS_JS)
        except Exception as exc:                    # engine cannot run JS at all
            notes.append(f"dom: {truncate(str(exc), 90)}")
            data = None
            if attempt == 1 and await _dismiss_consent(harness):
                continue
            break
        entries = _coerce_entries(data)
        if entries:
            return entries[:limit], "dom", ""
        if attempt == 1 and await _dismiss_consent(harness):
            continue                                # a wall, not a slow page
        if attempt >= 2:
            # A finished page with no result nodes is an error page or a dead
            # engine: waiting cannot help, so go straight to the network readers.
            ready = await _page_ready(harness)
            page_done = ready.get("ready") == "complete" and not ready.get("renderers")
            if ready and page_done and not ready.get("consent"):
                notes.append("dom: the page finished loading with no results on it")
                break
        if time.monotonic() >= deadline or attempt >= attempts:
            notes.append(f"dom: no video results after {attempt} read(s)")
            break
        await asyncio.sleep(delay)

    if not allow_network:
        return [], "", " | ".join(notes) or "no results on the page"

    from . import youtube_results

    try:
        entries, source, why = await asyncio.to_thread(
            youtube_results.search, query, limit=limit, readers=readers)
    except Exception as exc:                        # pragma: no cover - defensive
        notes.append(f"network: {truncate(str(exc), 90)}")
        return [], "", " | ".join(notes)
    if entries:
        return entries[:limit], source, ""
    if why:
        notes.append(why)

    # last resort: engines that can read the page but not run JS
    snapshot_entries = await _results_from_snapshot(harness)
    if snapshot_entries:
        return snapshot_entries[:limit], "snapshot", ""
    return [], "", " | ".join(notes) or "nothing found"


async def _results_from_snapshot(harness) -> List[Dict[str, Any]]:
    """Pull ``watch?v=`` links out of the harness's DOM snapshot."""
    try:
        snapshot = await harness.snapshot_page()
    except Exception as exc:
        log.debug(f"media: DOM snapshot unavailable ({exc})")
        return []
    rows: List[Dict[str, Any]] = []
    for element in getattr(snapshot, "elements", []) or []:
        href = getattr(element, "href", "") or ""
        match = re.search(r"v=([\w-]{6,})", href)
        if match:
            rows.append({"title": (getattr(element, "text", "") or "")[:200],
                         "id": match.group(1), "href": youtube_watch_url(match.group(1)),
                         "channel": "", "duration": "", "views": ""})
    return rows[:25]


async def _player_state(harness) -> Dict[str, Any]:
    try:
        raw = await harness.evaluate(PLAYER_JS)
    except Exception as exc:
        return {"error": str(exc)}
    if isinstance(raw, dict) and isinstance(raw.get("result"), dict):
        return raw["result"]
    return raw if isinstance(raw, dict) else {"raw": raw}


@tool(category="browser", timeout=180)
async def media_play(query: str = "", url: str = "", *, new_tab: bool = True,
                     pick: int = 0, press_play: bool = True,
                     remember: bool = True) -> str:
    """Play a song or video on the web, verify it is actually playing, and remember it.

    The workflow, in order:

    1. **decide what to play** — an explicit ``url`` wins; otherwise ``query``
       (a song/video name) or, when both are empty, the most recent play from
       the media history (so "play my favourite song" / "play that again" work);
    2. **URL injection** — open the YouTube results page for the query in a new
       tab instead of typing into the search box (typing is only needed when a
       site has no query parameter — use ``page_type`` + ``page_press`` then);
    3. **read and score the results** — the open page first (polled, because
       YouTube hydrates after load), then, if the page cannot be read at all,
       the same page fetched over HTTP (``ytInitialData``) or through Jina
       Reader, so a dead browser engine no longer means "no results"; the log
       shows which reader won and why each entry scored what it did;
    4. **open the video and verify playback** through the player element
       (``paused``/``currentTime``/``duration``); if autoplay was blocked JARVIS
       presses ``k`` and checks again — the reply always states the real state;
    5. **record the play** in ``data/media_history.json`` (and long-term memory),
       which is what makes "what did I play yesterday?" answerable.

    Args:
        query: What to play, e.g. "Kesariya Arijit Singh" (song, artist, or both)
        url: A direct video/watch URL — skips the search
        new_tab: Open the video in its own tab (default true, as asked for)
        pick: 0 plays the best-scoring match; 1, 2, … play that result as listed
        press_play: Press "k" if the player is paused (autoplay blocked)
        remember: Write the play to the media history
    """
    from ...config import load_config
    from .harness import HarnessError

    config = load_config()
    history = _history(config)
    resolved_from = "argument"
    requested = (query or "").strip()

    if not url:
        requested, resolved_from = resolve_request(requested, history)
        if not requested:
            return failure("Nothing to play: no song was given and there is no favourite "
                           "or previous play to fall back on. Ask for a title, or set one "
                           "with media_favourite(song=…).")
        if resolved_from == "last play":
            url = (history.last().url if history.last() else "") or ""

    target_url = url.strip() if url else build_search_url(requested)
    search_page = not url
    try:
        harness = _harness(config)
    except Exception as exc:
        return failure(f"browser unavailable: {exc}")

    try:
        opened = await harness.goto(target_url, new_tab=new_tab)
    except HarnessError as exc:
        return failure(f"could not open {target_url}: {exc}")

    engine = opened.get("engine") or "?"
    if search_page:
        wait_seconds = float(cfg_get(config, "browser.media.results_wait_seconds", 6.0))
        fallback = bool(cfg_get(config, "browser.media.results_fallback", True))
        readers = cfg_get(config, "browser.media.results_readers", ["http", "reader"])
        entries, reader, detail = await _read_results(
            harness, query=requested, wait=wait_seconds, allow_network=fallback,
            readers=tuple(readers) if isinstance(readers, (list, tuple)) else ("http", "reader"))
        if not entries:
            return failure(f"no video results for “{requested}” — opened {target_url} on "
                           f"{engine} and tried every reader ({detail or 'nothing found'}). "
                           "Check the network, or pass a direct url=…")
        # pick=0 → the best match (scored); pick=n → that result, as listed
        if pick and 0 < pick < len(entries):
            chosen = entries[pick]
        else:
            chosen = choose_candidate(entries, requested)
        if chosen is None:
            return failure("no usable result to play")
        watch_url = str(chosen.get("href") or youtube_watch_url(str(chosen.get("id") or "")))
        try:
            await harness.goto(watch_url, new_tab=new_tab)
        except HarnessError as exc:
            return failure(f"found “{chosen.get('title')}” but could not open it: {exc}")
        picked_line = (f"🎯 {truncate(str(chosen.get('title')), 80)}"
                       + (f" · {chosen.get('channel')}" if chosen.get("channel") else "")
                       + (f" · {chosen.get('duration')}" if chosen.get("duration") else "")
                       + f"  (match {score_candidate(chosen, requested):.0f}/100 of "
                         f"{len(entries)} results)")
        if reader:
            picked_line += f" · read from {READER_LABELS.get(reader, reader)}"
    else:
        chosen = {"title": "", "channel": "", "href": target_url}
        picked_line = f"🔗 direct url · {truncate(target_url, 90)}"

    state = await _player_state(harness)
    verdict = parse_player_state(state if isinstance(state, dict) else {})

    pressed = ""
    if press_play and verdict.get("has_video") and not verdict.get("playing") \
            and "at the end" not in str(verdict.get("reason", "")):
        import asyncio

        for attempt, how in ((1, "key"), (2, "js")):
            try:
                if how == "key":
                    await harness.press("k")          # YouTube's play/pause shortcut
                    pressed = " · pressed “k”"
                else:
                    await harness.evaluate(PLAY_JS)   # last resort: the element's own play()
                    pressed += " · called video.play()"
            except Exception as exc:
                pressed += f" · {how} failed ({exc})"
                continue
            await asyncio.sleep(1.5)
            state = await _player_state(harness)
            if not state.get("hasVideo"):
                continue                            # the fake ran out of states — retry
            verdict = parse_player_state(state)
            if verdict.get("playing"):
                break

    title = verdict.get("title") or chosen.get("title") or requested or target_url
    play = None
    if remember and (verdict.get("playing") or not verdict.get("has_video")):
        play = history.record(str(title), str(verdict.get("url") or chosen.get("href") or ""),
                              query=requested if search_page else "", channel=str(
                                  chosen.get("channel") or ""))
        _mirror_to_memory(play, config)

    icon = "▶️" if verdict.get("playing") else "⏸"
    lines = [f"{icon} {truncate(str(title), 90)}",
             f"   {picked_line}",
             f"   asked for “{truncate(requested or target_url, 60)}” ({resolved_from}) · "
             f"playing “{truncate(str(title), 60)}”" if search_page else
             f"   opened directly ({resolved_from})",
             f"   engine {engine} · tab {'new' if new_tab else 'current'} · "
             f"{verdict.get('reason', '')}{pressed}"]
    if play is not None:
        lines.append(f"   remembered · {history.counts()['total']} play(s) on record "
                     f"(data/{HISTORY_FILE})")
    if not verdict.get("playing"):
        lines.append("   ↳ the video is open but still paused (the site is blocking "
                     "autoplay) — click ▶ on the page, or call page_js with "
                     "'document.querySelector(\"video\").play()'")
    return "\n".join(lines)


def _mirror_to_memory(play: "Play", config: Optional[Mapping[str, Any]] = None) -> None:
    """Also put the play into long-term memory, so recall() finds it later."""
    try:
        from ..utility.memory_tools import remember

        result = remember.invoke({"fact": f"Played “{play.title}” "
                                         f"({play.url}) at {play.at[:16]}"
                                         + (f" after asking for “{play.query}”"
                                            if play.query else ""),
                                  "importance": 0.5})
        if not result.ok:
            log.debug(f"memory mirror skipped: {result.error}")
    except Exception as exc:                                # pragma: no cover - optional
        log.debug(f"memory mirror unavailable: {exc}")


@tool(category="browser", timeout=30)
def media_history(limit: int = 10, day: str = "", query: str = "",
                  since_days: int = 0) -> str:
    """What was played recently — answers "what did I play yesterday?".

    Args:
        limit: How many plays to list
        day: Exact day (YYYY-MM-DD); use "yesterday" or "today" too
        query: Filter by title, artist or the request that started it
        since_days: Only plays from the last N days
    """
    from ...config import load_config

    history = _history(load_config())
    target = None
    if day:
        lowered = day.strip().lower()
        if lowered == "yesterday":
            target = date.today() - timedelta(days=1)
        elif lowered == "today":
            target = date.today()
        else:
            try:
                target = date.fromisoformat(lowered)
            except ValueError:
                return failure(f"{day!r} is not a date like 2026-10-10 (or 'yesterday')")
    since = (date.today() - timedelta(days=int(since_days))) if since_days else None
    plays = history.plays(limit=limit, day=target, since=since, query=query)
    counts = history.counts()

    header = "**Media history**"
    if target:
        header += f" — {target.isoformat()}"
    elif since:
        header += f" — last {since_days} day(s)"
    elif query:
        header += f" — matching “{query}”"
    lines = [f"{header} ({len(plays)} of {counts['total']} play(s)):"]
    if not plays:
        lines.append("- nothing recorded yet" if counts["total"] == 0
                     else "- no plays matched")
    for play in plays:
        lines.append(f"- {play.line()}")
        if play.url:
            lines.append(f"  ↳ {truncate(play.url, 90)}")
    favourites = history.favourites()
    if favourites:
        lines.append(f"**Favourites:** {', '.join(truncate(f, 40) for f in favourites[:5])}")
    lines.append(f"_today {counts['today']} · yesterday {counts['yesterday']} · "
                 f"storage {counts['path']}_")
    return "\n".join(lines)


@tool(category="browser", timeout=30)
def media_favourite(song: str = "", remove: bool = False) -> str:
    """Set, list or clear the operator's favourite song(s).

    With a song name it is remembered as a favourite, so "play my favourite
    song" works even with an empty history.

    Args:
        song: Song/video (with artist if possible). Empty lists the favourites.
        remove: Remove that song instead of adding it
    """
    from ...config import load_config

    history = _history(load_config())
    if not song:
        favourites = history.favourites()
        if not favourites:
            return ("No favourites saved yet — say something like "
                    "media_favourite(song=\"Kesariya – Arijit Singh\").")
        return "**Favourites:**\n" + "\n".join(f"- {item}" for item in favourites)
    favourites = history.set_favourite(song, remove=remove)
    verb = "removed" if remove else "saved"
    return (f"✅ {verb} “{truncate(song, 60)}” — favourites: "
            f"{', '.join(truncate(f, 40) for f in favourites[:5])}")


@tool(category="browser", timeout=60)
async def page_tab_switch(target: str) -> str:
    """Switch to another open tab, by index or by a fragment of its title/URL.

    Args:
        target: Tab index ("0", "2") or text that appears in the title or URL
    """
    from .harness import HarnessError

    try:
        harness = _harness()
        result = await harness.switch_tab(target)
    except HarnessError as exc:
        return failure(str(exc))
    except Exception as exc:
        return failure(f"could not switch tabs: {exc}")
    return (f"🪟 switched to tab {result.get('index')} — "
            f"{truncate(str(result.get('title') or ''), 60)} · {result.get('url')}")


@tool(category="browser", timeout=60)
async def page_tab_close(target: str) -> str:
    """Close a tab, by index or by a fragment of its title/URL.

    Args:
        target: Tab index ("0", "2") or text that appears in the title or URL
    """
    from .harness import HarnessError

    try:
        harness = _harness()
        result = await harness.close_tab(target)
    except HarnessError as exc:
        return failure(str(exc))
    except Exception as exc:
        return failure(f"could not close the tab: {exc}")
    return (f"🪟 closed tab {result.get('closed')} — {result.get('remaining')} left "
            f"(now on {truncate(str(result.get('active_url') or 'about:blank'), 60)})")


MEDIA_TOOLS = [media_play, media_history, media_favourite,
               page_tab_switch, page_tab_close]

__all__ = ["MEDIA_TOOLS", "MediaHistory", "Play", "build_search_url", "choose_candidate",
           "parse_player_state", "youtube_watch_url", "RESULTS_JS", "PLAYER_JS",
           "score_candidate", "fold_word", "duration_penalty"]
