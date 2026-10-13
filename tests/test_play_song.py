"""play_song: search URL → Jina read → first trusted video → browser.

Jina, the direct reader and Chrome are stubbed, so these run offline.  They pin
the exact order: the search URL is what gets read, the first trusted watch link
in rank order is what gets opened, and a rate-limited Jina falls through to
reading the page directly instead of failing the request.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jarvis.tools.browser import play_song as mod

RESULTS = """# Search results
[Pasuri - Onumula Pasurinen](https://www.youtube.com/watch?v=AAAAAAAAAAA&t=3)
[Onumula Channel](https://www.youtube.com/@onumula)
[Some ad](https://ads.example.com/watch?v=BBBBBBBBBBB)
[Pasuri live](https://youtu.be/CCCCCCCCCCC)
"""


def test_search_url_is_built_from_the_query():
    assert mod.youtube_search_url("pasuri onumula") == \
        "https://www.youtube.com/results?search_query=pasuri+onumula"


def test_only_trusted_video_links_are_taken_in_rank_order():
    links = mod.trusted_video_links(RESULTS)
    assert [url for _title, url in links] == [
        "https://www.youtube.com/watch?v=AAAAAAAAAAA",
        "https://www.youtube.com/watch?v=CCCCCCCCCCC",
    ]
    assert links[0][0] == "Pasuri - Onumula Pasurinen"
    assert mod.pick_first_trusted(RESULTS)[1] == "https://www.youtube.com/watch?v=AAAAAAAAAAA"


def test_no_trusted_link_means_no_pick():
    assert mod.pick_first_trusted("[x](https://example.com/watch?v=DDDDDDDDDDD)") is None


def test_play_song_reads_the_search_page_then_opens_the_first_trusted_video(monkeypatch):
    read_urls, opened = [], []

    def fake_jina(url, max_chars=0):
        read_urls.append(url)
        return RESULTS

    async def fake_browse(url, new_tab=False, read_page=True):
        opened.append((url, new_tab))
        return f"✅ Opened **video**\nURL: {url}"

    monkeypatch.setattr(mod, "jina_read", fake_jina)
    monkeypatch.setattr(mod.browse_open, "func", fake_browse)

    result = asyncio.run(mod.play_song.func(query="pasuri onumula"))
    assert read_urls == ["https://www.youtube.com/results?search_query=pasuri+onumula"]
    assert opened == [("https://www.youtube.com/watch?v=AAAAAAAAAAA", True)]
    assert "Pasuri - Onumula Pasurinen" in result and "watch?v=AAAAAAAAAAA" in result


def test_jina_failure_is_reported_not_hidden(monkeypatch):
    from jarvis.tools.crawl.jina_reader import CrawlError

    def broken(url, max_chars=0):
        raise CrawlError("rate limit hit")

    monkeypatch.setattr(mod, "jina_read", broken)
    result = asyncio.run(mod.play_song.func(query="anything"))
    assert "Jina could not fetch" in str(result) and "rate limit" in str(result)


def test_direct_reader_takes_over_when_jina_is_rate_limited(monkeypatch):
    """HTTP 429 from Jina (free tier) must not end the request: read the page."""
    from jarvis.tools.browser import youtube_results as yt
    from jarvis.tools.crawl.jina_reader import CrawlError

    def rate_limited(url, max_chars=0):
        raise CrawlError("jina reader rate limit hit (free tier: 20 req/min)")

    html = (Path(__file__).resolve().parent / "fixtures"
            / "youtube_search.html").read_text(encoding="utf-8")
    monkeypatch.setattr(mod, "jina_read", rate_limited)
    monkeypatch.setattr(yt, "http_get", lambda url, **kwargs: html)

    opened = []

    async def fake_browse(url, new_tab=False, read_page=True):
        opened.append(url)
        return f"✅ Opened **video**\nURL: {url}"

    monkeypatch.setattr(mod.browse_open, "func", fake_browse)

    result = asyncio.run(mod.play_song.func(query="tare farak"))
    assert opened == ["https://www.youtube.com/watch?v=U0EI7XFkkV4"]
    assert "Farak - Taare | Official Music Video" in result
    assert "results data" in result                 # it says how it found it


def test_no_trusted_result_is_a_clear_failure(monkeypatch):
    monkeypatch.setattr(mod, "jina_read", lambda url, max_chars=0: "[ad](https://ads.example.com/x)")
    result = asyncio.run(mod.play_song.func(query="zzz"))
    assert "no trusted video" in str(result)


def test_empty_query_is_refused():
    assert "needs a song" in str(asyncio.run(mod.play_song.func(query="   ")))


@pytest.mark.parametrize("text, expected", [
    ("play pasuri on youtube", "pasuri"),
    ("play Tarre", "Tarre"),
    ("bajao kesariya", "kesariya"),
    ("open yt and play a random song", "popular songs"),
    ("play some lofi music on youtube music", "lofi"),
    ("play my fav song", ""),          # needs memory, not a search
    ("what is the capital of France", ""),
])
def test_play_request_parsing(text, expected):
    from jarvis.core.orchestrator import forced_play_query

    assert forced_play_query(text) == expected


def test_prompt_explains_when_and_how_for_bound_play_song(config):
    from jarvis.core.system_prompt import build_universal_prompt

    text = build_universal_prompt(config=config, tools=[mod.play_song])
    assert "When to use each bound tool" in text
    assert "play_song: WHEN" in text and "query = the song or artist name" in text


def test_play_song_is_registered_and_bound_for_a_play_request(config):
    from jarvis.core.brain import Brain  # noqa: F401  (import check)
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    assert "play_song" in registry.names()
