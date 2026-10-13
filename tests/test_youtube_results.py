"""Reading YouTube results without a browser: ytInitialData + reader markdown.

The fixtures are saved pages (a trimmed ``ytInitialData`` blob and the real
markdown ``r.jina.ai`` returns for ``tare farak``), so the whole fallback path
— the one that used to end in "found no video results" — is exercised offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.tools.browser import youtube_results as yt

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HTML = (FIXTURES / "youtube_search.html").read_text(encoding="utf-8")
MARKDOWN = (FIXTURES / "youtube_search.md").read_text(encoding="utf-8")


# ── ytInitialData ───────────────────────────────────────────────────────────
def test_initial_data_is_found_in_both_shapes():
    data = yt.extract_initial_data(HTML)
    assert isinstance(data, dict)
    assert "contents" in data

    quoted = "<script>var ytInitialData = '{\"a\": 1}';</script>"
    assert yt.extract_initial_data(quoted) == {"a": 1}

    assert yt.extract_initial_data("") is None
    assert yt.extract_initial_data("<html>no data here</html>") is None
    assert yt.extract_initial_data("<script>var ytInitialData = {broken;</script>") is None


def test_videos_are_walked_out_of_the_nested_blob():
    entries = yt.videos_from_initial_data(yt.extract_initial_data(HTML))
    titles = [entry["title"] for entry in entries]
    assert titles[0] == "Farak - Taare | Official Music Video |"
    assert len(entries) == 5                       # the empty-titled one is dropped

    first = entries[0]
    assert first["id"] == "U0EI7XFkkV4"
    assert first["href"] == "https://www.youtube.com/watch?v=U0EI7XFkkV4"
    assert first["channel"] == "Farak and Mashooriya Originals"
    assert first["duration"] == "3:02"
    assert "24,031,112" in first["views"]
    assert entries[1]["channel"] == "THE LYRICIST"


def test_limit_and_junk_are_respected():
    assert len(yt.videos_from_initial_data(yt.extract_initial_data(HTML), limit=2)) == 2
    assert yt.videos_from_initial_data(None) == []
    assert yt.videos_from_initial_data({"nonsense": [1, 2, 3]}) == []


# ── reader markdown ─────────────────────────────────────────────────────────
def test_reader_markdown_keeps_youtube_order_and_metadata():
    entries = yt.videos_from_reader_text(MARKDOWN)
    assert [entry["id"] for entry in entries] == [
        "U0EI7XFkkV4", "TX4AbxBSjG8", "085XkY0OKTU", "y9t_rgs7SHM", "sh5kQQ9eYZM"]

    first = entries[0]
    assert first["title"] == "Farak - Taare | Official Music Video |"
    assert first["duration"] == "3:02"                  # from the thumbnail line above
    assert first["channel"] == "Farak and Mashooriya Originals"
    assert entries[3]["duration"] == "1:03:31"          # h:mm:ss survives


def test_reader_markdown_ignores_thumbnails_and_ads():
    entries = yt.videos_from_reader_text(MARKDOWN)
    assert all(not entry["title"].startswith("!") for entry in entries)
    assert all(entry["id"] != "" for entry in entries)
    assert yt.videos_from_reader_text("") == []
    assert yt.videos_from_reader_text("no links at all, just prose") == []


def test_reader_markdown_catches_a_bare_watch_url():
    text = "some page\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
    entries = yt.videos_from_reader_text(text)
    assert [entry["id"] for entry in entries] == ["dQw4w9WgXcQ"]


def test_duration_seconds():
    assert yt.duration_seconds("3:02") == 182.0
    assert yt.duration_seconds("1:03:31") == 3811.0
    assert yt.duration_seconds("no duration") == 0.0


# ── the readers + the fallback chain ────────────────────────────────────────
def test_http_reader_uses_the_fixture(monkeypatch):
    monkeypatch.setattr(yt, "http_get", lambda url, **kwargs: HTML)
    entries = yt.search_via_http("tare farak")
    assert entries and entries[0]["id"] == "U0EI7XFkkV4"
    assert "gl=US" in yt.build_search_url("tare farak")


def test_http_reader_reports_a_wall_instead_of_pretending(monkeypatch):
    monkeypatch.setattr(yt, "http_get", lambda url, **kwargs: "<html>consent wall</html>")
    with pytest.raises(ValueError, match="ytInitialData"):
        yt.search_via_http("tare farak")


def test_reader_fallback_uses_the_markdown(monkeypatch):
    entries = yt.search_via_reader("tare farak", reader=lambda url: MARKDOWN)
    assert entries[0]["title"] == "Farak - Taare | Official Music Video |"


def test_chain_falls_through_and_reports_why(monkeypatch):
    def broken_http(url, **kwargs):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(yt, "http_get", broken_http)
    entries, source, error = yt.search("tare farak", reader=lambda url: MARKDOWN)
    assert source == "reader" and entries[0]["id"] == "U0EI7XFkkV4"

    entries, source, error = yt.search("tare farak", reader=lambda url: "nothing here")
    assert entries == [] and source == ""
    assert "http: connect timeout" in error      # the real reason, not "no results"
