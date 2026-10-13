"""Media: "play my favourite song" — resolution, URL injection, verification, memory.

No network and no real browser: a fake harness answers the same calls the real
engines do (``goto``, ``evaluate``, ``press``, ``switch_tab``, ``close_tab``), so
the whole flow — search → score → open → verify → press play → remember — is
exercised deterministically, including the autoplay-blocked path.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.tools.browser import media as media_module


# ── fake browser ────────────────────────────────────────────────────────────
class FakeHarness:
    """Answers the browser calls the media tools make, in order."""

    def __init__(self, results, states, *, engine="playwright", ready="complete"):
        self.results = results                 # what the results page returns
        self.states = list(states)             # player states, one per check
        self.engine = engine
        self.ready = ready                     # document.readyState the page reports
        self.visits: list = []
        self.keys: list = []
        self.switched: list = []
        self.closed: list = []
        self.tabs = [{"index": "0", "title": "YouTube", "url": "https://youtube.com"}]
        self.evaluated: list = []

    async def goto(self, url, *, new_tab=False, snapshot=True):
        self.visits.append((url, new_tab))
        return {"ok": True, "engine": self.engine, "url": url, "title": "YouTube"}

    async def evaluate(self, expression):
        self.evaluated.append(expression)
        if expression is media_module.RESULTS_JS:
            return self.results
        if expression is media_module.RESULTS_READY_JS:
            rows = self.results if isinstance(self.results, list) else []
            return {"consent": False, "renderers": len(rows), "titles": len(rows),
                    "ready": self.ready, "url": "https://www.youtube.com/results"}
        if expression is media_module.CONSENT_JS:
            return {"clicked": False}
        if expression is media_module.PLAYER_JS:
            return self.states.pop(0) if self.states else {"hasVideo": False}
        if expression is media_module.PLAY_JS:
            return {"ok": True, "paused": False, "currentTime": 0.0}
        return {"hasVideo": False}

    async def press(self, key):
        self.keys.append(key)
        return True

    async def snapshot_page(self, **_kwargs):
        class Snap:
            elements = []
        return Snap()

    async def switch_tab(self, target):
        self.switched.append(target)
        return {"index": 2, "title": "Video", "url": "https://youtube.com/watch?v=1"}

    async def close_tab(self, target):
        self.closed.append(target)
        return {"closed": 2, "remaining": 1, "active_url": "https://youtube.com"}


@pytest.fixture()
def history(tmp_path, monkeypatch):
    store = media_module.MediaHistory(path=tmp_path / "media_history.json")
    monkeypatch.setattr(media_module, "_history", lambda config=None: store)
    return store


@pytest.fixture(autouse=True)
def _quick_polling(monkeypatch):
    """Keep the results-page polling snappy — the waits are not what we test."""
    monkeypatch.setattr(media_module, "RESULTS_POLL_ATTEMPTS", 3)
    monkeypatch.setattr(media_module, "RESULTS_POLL_DELAY", 0.01)


@pytest.fixture()
def youtube_html():
    """A saved YouTube results page (``ytInitialData`` trimmed)."""
    return (Path(__file__).resolve().parent / "fixtures"
            / "youtube_search.html").read_text(encoding="utf-8")


# ── helpers (pure functions) ────────────────────────────────────────────────
def test_search_url_is_injected_not_typed():
    url = media_module.build_search_url("Kesariya Arijit Singh")
    assert url.startswith("https://www.youtube.com/results?search_query=")
    assert "Kesariya+Arijit+Singh" in url or "Kesariya%20Arijit%20Singh" in url
    assert media_module.build_search_url("a b", engine="google").startswith(
        "https://www.google.com/search?q=")
    assert "open.spotify.com/search" in media_module.build_search_url("x", engine="spotify")
    with pytest.raises(ValueError):
        media_module.build_search_url("")
    with pytest.raises(ValueError):
        media_module.build_search_url("x", engine="napster")


def test_youtube_watch_url_and_url_detection():
    assert media_module.youtube_watch_url("dQw4w9WgXcQ") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert media_module.looks_like_url("https://youtu.be/x")
    assert not media_module.looks_like_url("Kesariya")


def test_candidate_scoring_prefers_the_real_song():
    query = "kesariya arijit singh"
    candidates = [
        {"title": "Kesariya – Official Video | Arijit Singh", "duration": "4:28",
         "channel": "T-Series", "id": "a"},
        {"title": "Kesariya (slowed + reverb) 8D audio", "duration": "4:30",
         "channel": "random", "id": "b"},
        {"title": "Kesariya REACTION", "duration": "12:30", "channel": "reacts", "id": "c"},
        {"title": "Funny cats compilation", "duration": "0:42", "channel": "cats", "id": "d"},
    ]
    scores = [media_module.score_candidate(entry, query) for entry in candidates]
    assert scores[0] == max(scores)
    assert scores[0] > scores[2] and scores[0] > scores[3]
    chosen = media_module.choose_candidate(candidates, query)
    assert chosen["id"] == "a"
    assert media_module.choose_candidate([], query) is None


def test_player_state_verdicts_are_honest():
    playing = media_module.parse_player_state(
        {"hasVideo": True, "paused": False, "currentTime": 12.5, "duration": 250.0,
         "title": "Kesariya", "url": "https://youtube.com/watch?v=a"})
    assert playing["playing"] and "playing at 12.5s" in playing["reason"]

    paused = media_module.parse_player_state({"hasVideo": True, "paused": True,
                                              "currentTime": 0})
    assert not paused["playing"] and "autoplay was blocked" in paused["reason"]

    no_video = media_module.parse_player_state({"hasVideo": False})
    assert not no_video["playing"] and "no <video> element" in no_video["reason"]

    ended = media_module.parse_player_state({"hasVideo": True, "paused": True,
                                             "currentTime": 250, "duration": 250})
    assert "already at the end" in ended["reason"]


# ── history ─────────────────────────────────────────────────────────────────
def test_history_records_lists_and_finds_yesterday(tmp_path):
    store = media_module.MediaHistory(path=tmp_path / "h.json")
    yesterday = datetime.now() - timedelta(days=1)
    store.record("Kesariya – Arijit Singh", "https://youtu.be/1", query="kesariya",
                 channel="T-Series", when=yesterday)
    store.record("Tum Hi Ho", "https://youtu.be/2", query="tum hi ho")

    assert store.counts()["total"] == 2 and store.counts()["yesterday"] == 1
    old = store.plays(day=date.today() - timedelta(days=1))
    assert [play.title for play in old] == ["Kesariya – Arijit Singh"]
    assert store.last().title == "Tum Hi Ho"                 # newest first
    assert store.last(query="kesariya").title == "Kesariya – Arijit Singh"
    assert "Kesariya" in store.plays(query="arijit")[0].title
    assert store.plays(limit=1)[0].title == "Tum Hi Ho"
    # it survives a reload — that is the point of the file
    assert media_module.MediaHistory(path=tmp_path / "h.json").counts()["total"] == 2


def test_resolve_request_prefers_favourites_then_history(tmp_path):
    store = media_module.MediaHistory(path=tmp_path / "h.json")
    store.record("Tum Hi Ho", "https://youtu.be/2", query="tum hi ho")
    # no favourites yet: "my favourite song" falls back to the last play
    assert media_module.resolve_request("my favourite song", store) == ("Tum Hi Ho",
                                                                       "last play")
    store.set_favourite("Kesariya – Arijit Singh")
    assert media_module.resolve_request("my favourite song", store) == \
        ("Kesariya – Arijit Singh", "favourites")
    assert media_module.resolve_request("kesariya", store) == \
        ("Kesariya – Arijit Singh", "favourites")     # a fragment resolves to the full title
    assert media_module.resolve_request("shape of you", store) == ("shape of you",
                                                                  "argument")
    empty = media_module.MediaHistory(path=tmp_path / "e.json")
    assert media_module.resolve_request("", empty) == ("", "")


def test_history_favourites(tmp_path):
    store = media_module.MediaHistory(path=tmp_path / "h.json")
    assert store.favourites() == []
    store.set_favourite("Kesariya – Arijit Singh")
    store.set_favourite("Tum Hi Ho")
    store.set_favourite("kesariya – arijit singh")            # duplicate, different case
    assert store.favourites() == ["Tum Hi Ho", "Kesariya – Arijit Singh"]
    store.set_favourite("Tum Hi Ho", remove=True)
    assert store.favourites() == ["Kesariya – Arijit Singh"]


# ── tools ───────────────────────────────────────────────────────────────────
def test_media_play_injects_the_url_verifies_and_remembers(history, monkeypatch):
    results = [
        {"title": "Other song", "id": "zzz", "href": "https://www.youtube.com/watch?v=zzz",
         "channel": "x", "duration": "3:10"},
        {"title": "Kesariya – Arijit Singh (Official)", "id": "abc123",
         "href": "https://www.youtube.com/watch?v=abc123", "channel": "T-Series",
         "duration": "4:28"},
    ]
    harness = FakeHarness(results, [{"hasVideo": True, "paused": False, "currentTime": 3.2,
                                     "duration": 268.0, "title": "Kesariya",
                                     "url": "https://www.youtube.com/watch?v=abc123"}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)

    result = media_module.media_play.invoke({"query": "kesariya arijit singh"})
    assert result.ok, result.error
    assert "Kesariya" in result.content and "▶️" in result.content
    assert "match" in result.content and "results" in result.content
    assert "remembered" in result.content

    # URL injection: a results page, then the watch page — both in new tabs
    assert harness.visits[0][0].startswith("https://www.youtube.com/results?search_query=")
    assert harness.visits[1][0] == "https://www.youtube.com/watch?v=abc123"
    assert all(new_tab for _url, new_tab in harness.visits)

    play = history.last()
    assert play.title == "Kesariya" and "abc123" in play.url
    assert play.query == "kesariya arijit singh"


def test_media_play_presses_play_when_autoplay_is_blocked(history, monkeypatch):
    harness = FakeHarness(
        [{"title": "Song", "id": "v1", "href": "https://www.youtube.com/watch?v=v1",
          "channel": "", "duration": "3:00"}],
        [{"hasVideo": True, "paused": True, "currentTime": 0, "duration": 180},
         {"hasVideo": True, "paused": False, "currentTime": 1.4, "duration": 180,
          "title": "Song", "url": "https://www.youtube.com/watch?v=v1"}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)

    result = media_module.media_play.invoke({"query": "song"})
    assert result.ok, result.error
    assert harness.keys == ["k"]                       # pressed YouTube's play key
    assert "pressed" in result.content and "▶️" in result.content


def test_media_play_falls_back_to_video_play_js(history, monkeypatch):
    class Stubborn(FakeHarness):
        def __init__(self):
            super().__init__(
                [{"title": "Song", "id": "v1", "href": "https://www.youtube.com/watch?v=v1"}],
                [{"hasVideo": True, "paused": True},                 # initial check
                 {"hasVideo": True, "paused": True},                 # after pressing "k"
                 {"hasVideo": True, "paused": False, "currentTime": 0.8, "duration": 200,
                  "title": "Song", "url": "https://www.youtube.com/watch?v=v1"}])

    harness = Stubborn()
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)
    result = media_module.media_play.invoke({"query": "song"})
    assert result.ok, result.error
    assert harness.keys == ["k"]                                  # tried the key first
    assert media_module.PLAY_JS in harness.evaluated               # then the element API
    assert "▶️" in result.content and "called video.play()" in result.content


def test_media_play_reports_when_it_could_not_start(history, monkeypatch):
    harness = FakeHarness([{"title": "Song", "id": "v1",
                            "href": "https://www.youtube.com/watch?v=v1"}],
                          [{"hasVideo": True, "paused": True},
                           {"hasVideo": True, "paused": True},
                           {"hasVideo": True, "paused": True}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)
    result = media_module.media_play.invoke({"query": "song"})
    assert result.ok, result.error
    assert "⏸" in result.content or "not playing" in result.content
    assert "pressed" in result.content                 # it tried, and says so


def test_media_play_replays_the_last_song_with_no_arguments(history, monkeypatch):
    history.record("Tum Hi Ho", "https://www.youtube.com/watch?v=tum1", query="tum hi ho")
    harness = FakeHarness([], [{"hasVideo": True, "paused": False, "currentTime": 5,
                                "duration": 250, "title": "Tum Hi Ho",
                                "url": "https://www.youtube.com/watch?v=tum1"}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)

    result = media_module.media_play.invoke({})
    assert result.ok, result.error
    assert harness.visits == [("https://www.youtube.com/watch?v=tum1", True)]
    assert "Tum Hi Ho" in result.content


def test_media_play_uses_a_favourite_for_the_favourite_phrase(history, monkeypatch):
    history.set_favourite("Kesariya – Arijit Singh")
    harness = FakeHarness([{"title": "Kesariya – Arijit Singh", "id": "k1",
                            "href": "https://www.youtube.com/watch?v=k1"}],
                          [{"hasVideo": True, "paused": False, "currentTime": 1,
                            "duration": 260, "title": "Kesariya",
                            "url": "https://www.youtube.com/watch?v=k1"}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)

    result = media_module.media_play.invoke({"query": "my favourite song"})
    assert result.ok, result.error
    url, _new_tab = harness.visits[0]
    assert "search_query=Kesariya" in url or "search_query=Kesariya+" in url
    assert "Kesariya" in url                     # the favourite was resolved, not the phrase


def test_media_play_reports_an_empty_history_honestly(history, monkeypatch):
    monkeypatch.setattr(media_module, "_harness",
                        lambda config=None: pytest.fail("no browser should be opened"))
    result = media_module.media_play.invoke({})
    assert result.ok is False
    assert "media_favourite" in result.error
    assert "no favourite or previous play" in result.error


def test_media_play_without_results_says_so(history, monkeypatch):
    harness = FakeHarness([], [{"hasVideo": False}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)
    result = media_module.media_play.invoke({"query": "something obscure"})
    assert result.ok is False
    assert "no video results" in result.error
    # the reader that failed is named, so "no results" is never a guess
    assert "dom:" in result.error and ("http" in result.error or "reader" in result.error)


def test_media_play_falls_back_to_reading_the_page_over_http(history, monkeypatch,
                                                             youtube_html):
    """The page is open but unreadable (engine degraded) — ytInitialData saves it."""
    from jarvis.tools.browser import youtube_results

    monkeypatch.setattr(youtube_results, "http_get", lambda url, **kw: youtube_html)
    harness = FakeHarness([], [{"hasVideo": True, "paused": False, "currentTime": 4.0,
                                "duration": 182.0, "title": "Farak - Taare",
                                "url": "https://www.youtube.com/watch?v=U0EI7XFkkV4"}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)

    result = media_module.media_play.invoke({"query": "tare farak"})
    assert result.ok, result.error
    assert harness.visits[-1][0] == "https://www.youtube.com/watch?v=U0EI7XFkkV4"
    assert "Farak - Taare" in result.content
    assert "read from" in result.content and "ytInitialData" in result.content


def test_media_play_falls_back_to_the_reader_when_http_is_blocked(history, monkeypatch):
    """Direct GET refused (proxy/firewall): r.jina.ai still lists the results."""
    from jarvis.tools.browser import youtube_results
    from jarvis.tools.crawl import jina_reader

    markdown = (Path(__file__).resolve().parent / "fixtures"
                / "youtube_search.md").read_text(encoding="utf-8")

    def blocked(url, **kwargs):
        raise RuntimeError("proxy refused the direct GET")

    monkeypatch.setattr(youtube_results, "http_get", blocked)
    monkeypatch.setattr(jina_reader, "jina_read", lambda url, **kwargs: markdown)
    harness = FakeHarness([], [{"hasVideo": True, "paused": False, "currentTime": 1.0,
                                "duration": 182.0, "title": "Farak - Taare",
                                "url": "https://www.youtube.com/watch?v=U0EI7XFkkV4"}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)

    result = media_module.media_play.invoke({"query": "tare farak"})
    assert result.ok, result.error
    assert harness.visits[-1][0] == "https://www.youtube.com/watch?v=U0EI7XFkkV4"
    assert "Jina" in result.content and "read from" in result.content


def test_every_reader_failing_reports_the_reason_not_a_guess(history, monkeypatch):
    """No DOM, no HTTP, no reader: the error must name what was tried."""
    from jarvis.tools.browser import youtube_results

    def blocked(url, **kwargs):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(youtube_results, "http_get", blocked)
    harness = FakeHarness([], [{"hasVideo": False}])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)
    result = media_module.media_play.invoke({"query": "tare farak"})
    assert result.ok is False
    assert "no video results" in result.error
    assert "connect timeout" in result.error


def test_media_play_dismisses_the_consent_wall_then_reads(history, monkeypatch):
    """A consent wall is clicked through instead of being read as 'no results'."""
    class ConsentHarness(FakeHarness):
        def __init__(self):
            super().__init__([], [{"hasVideo": True, "paused": False, "currentTime": 2.0,
                                   "duration": 200.0, "title": "Taare",
                                   "url": "https://www.youtube.com/watch?v=abc123def45"}],
                             ready="loading")
            self.clicked = False

        async def evaluate(self, expression):
            self.evaluated.append(expression)
            if expression is media_module.CONSENT_JS:
                self.clicked = True
                return {"clicked": True}
            return await FakeHarness.evaluate(self, expression)

    harness = ConsentHarness()
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)
    monkeypatch.setattr(media_module, "RESULTS_POLL_DELAY", 0.01)
    result = media_module.media_play.invoke({"query": "taare"})
    assert harness.clicked is True
    assert result.ok is False                 # offline: no reader can list results
    assert media_module.CONSENT_JS in harness.evaluated


def test_results_are_read_from_the_page_when_the_engine_is_slow(history, monkeypatch):
    """Results that appear a second later are still found (the original bug)."""
    class SlowHarness(FakeHarness):
        def __init__(self):
            super().__init__([], [{"hasVideo": True, "paused": False, "currentTime": 3.0,
                                   "duration": 240.0, "title": "Kesariya",
                                   "url": "https://www.youtube.com/watch?v=kesariya01"}],
                             ready="loading")
            self.reads = 0

        async def evaluate(self, expression):
            if expression is media_module.RESULTS_JS:
                self.reads += 1
                if self.reads < 3:                     # YouTube has not hydrated yet
                    return []
                return [{"title": "Kesariya – Arijit Singh", "id": "kesariya01",
                         "href": "https://www.youtube.com/watch?v=kesariya01",
                         "channel": "T-Series", "duration": "4:28"}]
            return await super().evaluate(expression)

    harness = SlowHarness()
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)
    result = media_module.media_play.invoke({"query": "kesariya"})
    assert result.ok, result.error
    assert harness.reads == 3                           # it waited instead of giving up
    assert harness.visits[-1][0] == "https://www.youtube.com/watch?v=kesariya01"


# ── matching: what "tare farak" should actually pick ────────────────────────
def test_scoring_matches_romanised_spellings_and_skips_loops():
    entries = [
        {"title": "Farak - Taare | Official Music Video |", "duration": "3:02",
         "channel": "Farak and Mashooriya Originals", "id": "official"},
        {"title": "Taare - Farak | (Lyrics)", "duration": "2:35", "channel": "x",
         "id": "lyrics"},
        {"title": "Farak - Taare | Official Music Video | 1 HOUR LOOP",
         "duration": "1:03:31", "channel": "x", "id": "loop"},
        {"title": "Taare - Farak | lyrics. #shorts", "duration": "0:41", "channel": "x",
         "id": "short"},
        {"title": "Completely unrelated video", "duration": "5:00", "channel": "x",
         "id": "other"},
    ]
    assert media_module.fold_word("taare") == "tare"      # "tare" vs "taare"
    chosen = media_module.choose_candidate(entries, "tare farak")
    assert chosen["id"] == "official"
    assert (media_module.score_candidate(entries[0], "tare farak")
            > media_module.score_candidate(entries[2], "tare farak")
            > media_module.score_candidate(entries[4], "tare farak"))


def test_entries_are_coerced_from_whatever_the_browser_returned():
    assert media_module._coerce_entries(None) == []
    assert media_module._coerce_entries("junk") == []
    rows = media_module._coerce_entries(
        [{"title": "A", "videoId": "abc12345678"},
         {"title": "B", "href": "https://www.youtube.com/watch?v=xyz98765432"},
         {"title": "no id at all"}, "not a dict"])
    assert [row["id"] for row in rows] == ["abc12345678", "xyz98765432"]
    assert rows[0]["href"] == "https://www.youtube.com/watch?v=abc12345678"
    # a json-stringified reply (some engines wrap the result)
    assert media_module._coerce_entries(
        {"result": [{"title": "A", "id": "abc12345678"}]})[0]["id"] == "abc12345678"


def test_media_history_tool_answers_the_yesterday_question(history):
    yesterday = datetime.now() - timedelta(days=1)
    history.record("Kesariya – Arijit Singh", "https://youtu.be/1", query="kesariya",
                   when=yesterday)
    history.record("Tum Hi Ho", "https://youtu.be/2")

    result = media_module.media_history.invoke({"day": "yesterday"})
    assert result.ok, result.error
    assert "Kesariya" in result.content and date.today().isoformat() not in result.content
    assert "yesterday 1" in result.content

    today = media_module.media_history.invoke({"query": "tum"})
    assert "Tum Hi Ho" in today.content and "Kesariya" not in today.content

    bad = media_module.media_history.invoke({"day": "someday"})
    assert bad.ok is False and "not a date" in bad.error

    empty = media_module.media_history.invoke({"since_days": 30})
    assert empty.ok and "2 of 2 play(s)" in empty.content


def test_media_favourite_tool_sets_lists_and_removes(history):
    listing = media_module.media_favourite.invoke({})
    assert listing.ok and "No favourites saved" in listing.content

    saved = media_module.media_favourite.invoke({"song": "Kesariya – Arijit Singh"})
    assert saved.ok and "saved" in saved.content
    assert media_module.media_history.invoke({}).content.count("Favourites") == 1

    removed = media_module.media_favourite.invoke({"song": "kesariya – arijit singh",
                                                   "remove": True})
    assert removed.ok and "removed" in removed.content


def test_tab_tools_switch_and_close(monkeypatch):
    harness = FakeHarness([], [])
    monkeypatch.setattr(media_module, "_harness", lambda config=None: harness)

    switched = media_module.page_tab_switch.invoke({"target": "youtube"})
    assert switched.ok and "switched to tab 2" in switched.content
    assert harness.switched == ["youtube"]

    closed = media_module.page_tab_close.invoke({"target": "youtube"})
    assert closed.ok and "closed tab 2" in closed.content and "1 left" in closed.content
    assert harness.closed == ["youtube"]


def test_tab_tools_surface_engine_errors(monkeypatch):
    class Broken(FakeHarness):
        async def switch_tab(self, target):
            from jarvis.tools.browser.harness import HarnessError

            raise HarnessError("there is no tab 9 (open tabs: 2)")

    monkeypatch.setattr(media_module, "_harness", lambda config=None: Broken([], []))
    result = media_module.page_tab_switch.invoke({"target": "9"})
    assert result.ok is False and "no tab 9" in result.error


# ── registration ────────────────────────────────────────────────────────────
def test_media_tools_are_registered(registry):
    names = {tool.name for tool in registry.get_by_category("browser")}
    assert {"media_play", "media_history", "media_favourite",
            "page_tab_switch", "page_tab_close"} <= names
    assert len(names) >= 20
