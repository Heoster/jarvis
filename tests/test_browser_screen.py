"""Browser + screen: DOM parsing, coordinate maths, engine fallback, OCR contract.

None of these need a real display — they test the logic that makes the focus
areas work, plus the honesty of the capability reports when nothing is installed.
"""

from __future__ import annotations

import pytest

from jarvis.tools.browser.dom_parser import (
    DOMElement, DOMSnapshot, parse_html, render_listing, selector_for,
)
from jarvis.tools.browser.harness import BrowserHarness, compute_screen_point, get_harness

SAMPLE_HTML = """
<html><head><title>JARVIS test page</title>
<meta name="description" content="a test page"></head>
<body>
  <h1>Welcome, Heoster</h1>
  <p>This page tests the DOM parser used for browser control.</p>
  <form action="/login" method="post">
    <input type="text" name="username" placeholder="Username">
    <input type="password" name="password" placeholder="Password">
    <button type="submit" id="login-btn">Sign in</button>
  </form>
  <a href="https://codeex-ai.vercel.app">CODEEX-AI</a>
  <a href="/about">About</a>
  <script>window.junk = "should not appear";</script>
  <style>.x { color: red }</style>
</body></html>
"""


# ── DOM parsing: this is how JARVIS "sees" a page without a vision model ──
def test_dom_parser_finds_interactive_elements():
    snapshot = parse_html(SAMPLE_HTML, url="https://example.com")
    assert snapshot.title == "JARVIS test page"
    kinds = {element.kind for element in snapshot.elements}
    assert {"input", "button", "link"} <= kinds

    buttons = snapshot.find("Sign in")
    assert buttons and buttons[0].kind == "button"
    assert selector_for(buttons[0]) == "#login-btn"

    links = snapshot.find("CODEEX-AI")
    assert links and links[0].href.endswith("codeex-ai.vercel.app")


def test_dom_parser_strips_script_and_style():
    snapshot = parse_html(SAMPLE_HTML)
    assert "window.junk" not in snapshot.text, "script content must never leak in"
    assert "should not appear" not in snapshot.text
    assert "color: red" not in snapshot.text


def test_dom_snapshot_is_indexable_and_renderable():
    snapshot = parse_html(SAMPLE_HTML, url="https://example.com")
    assert snapshot.elements, "elements must be indexed"
    text = snapshot.as_text(max_chars=2000)
    assert "Welcome, Heoster" in text
    assert "[1]" in text or "[0]" in text, "listing must be addressable by index"

    listing = render_listing(snapshot.elements)
    assert "Sign in" in listing
    assert len(listing) < 4000, "the listing must stay prompt-sized"


def test_selector_for_prefers_stable_attributes():
    by_id = DOMElement(index=1, tag="button", selector="#go", text="Go", element_id="go")
    assert selector_for(by_id) == "#go"
    testid = DOMElement(index=2, tag="button", selector="button:nth-of-type(2)",
                        text="Save", attrs={"data-testid": "save-btn"})
    assert selector_for(testid) == '[data-testid="save-btn"]'
    generic = DOMElement(index=3, tag="a", selector="a:nth-of-type(1)", text="Read more")
    assert selector_for(generic), "every element must get *some* selector"


# ── screen coordinates: DOM box → real cursor position ────────────────────
def test_screen_point_maps_viewport_to_desktop():
    box = {"x": 100, "y": 50, "width": 200, "height": 40}
    # window at (200, 100), chrome UI eating 80px of vertical space, no offset
    x, y = compute_screen_point(box, window_left=200, window_top=100,
                                viewport_offset=(0, 80))
    assert x == 200 + 100 + 100          # window + box centre
    assert y == 100 + 80 + 50 + 20       # window + chrome UI + box centre


def test_screen_point_handles_scaled_monitors():
    box = {"x": 0, "y": 0, "width": 100, "height": 100}
    x, y = compute_screen_point(box, window_left=10, window_top=20,
                                viewport_offset=(5, 95))
    assert (x, y) == (10 + 5 + 50, 20 + 95 + 50)


# ── engine fallback chain ─────────────────────────────────────────────────
def test_harness_engine_chain_ends_with_screen_control():
    harness = BrowserHarness({"browser": {"engine": "auto"}})
    assert harness.engine_chain[-1] == "screen", \
        "screen control must always be the last resort"
    assert "playwright" in harness.engine_chain


def test_harness_reports_every_engine_truthfully():
    harness = BrowserHarness({"browser": {"engine": "auto"}})
    reports = harness.engine_reports()
    assert {report.name for report in reports} >= {"playwright", "selenium", "screen"}
    for report in reports:
        assert isinstance(report.available, bool)
        assert report.detail, "an unavailable engine must say why"
        assert report.line()


def test_harness_verify_explains_what_to_install():
    """In this sandbox nothing is installed — the report must say so clearly."""
    harness = BrowserHarness({"browser": {"engine": "auto"}})
    report = harness.verify()
    assert "verification" in report.lower()
    if "no browser engine works" in report:
        assert "playwright install" in report


def test_harness_launch_fails_gracefully_without_chrome():
    harness = BrowserHarness({"browser": {"engine": "auto"}})
    outcome = harness.launch(url="about:blank")
    assert outcome["ok"] is False
    assert outcome["detail"], "the failure must be explained, not swallowed"
    assert isinstance(outcome.get("url"), str)


def test_get_harness_is_a_singleton(config):
    first = get_harness(config)
    assert get_harness(config) is first, "one browser, one element map"


# ── screen control: capability reporting without a display ────────────────
def test_screen_tools_report_no_display_honestly():
    from jarvis.tools.screen.screen_capture import screen_tools_report

    report = screen_tools_report()
    assert "display" in report.lower() or "backend" in report.lower()


def test_backend_chain_is_ordered_and_explained():
    from jarvis.tools.screen.backends import choose_backend, list_backends

    backend = choose_backend("auto")
    assert backend.available()[0] is False or backend.name
    rows = list_backends()
    assert rows and all(len(row) == 3 for row in rows)
    names = [row[0] for row in rows]
    assert "pyautogui" in names and "xdotool" in names


def test_null_backend_never_pretends_to_work():
    from jarvis.tools.screen.backends import NullBackend

    backend = NullBackend("no display")
    assert backend.available() == (False, "no display")
    assert backend.move(10, 10) is False
    assert backend.click(10, 10) is False
    assert backend.type_text("hi") is False


def test_bezier_path_produces_smooth_human_like_motion():
    from jarvis.tools.screen.mouse_keyboard import bezier_path

    path = bezier_path((0, 0), (400, 200), steps=24)
    assert 20 <= len(path) <= 25, "a 24-step move must produce ~24 cursor points"
    assert path[-1] == (400, 200), "the cursor must land exactly on target"
    xs = [point[0] for point in path]
    assert xs == sorted(xs), "the path must move monotonically toward the target"
    # a straight line would have no vertical variation in the middle steps
    assert len({point[1] for point in path[1:-1]}) > 1, "path must curve, not jump"


def test_ocr_reports_what_is_missing():
    from jarvis.tools.screen.ocr_reader import ocr_available

    available, detail = ocr_available()
    assert isinstance(available, bool)
    assert detail, "OCR availability must be explained (no vision model is used)"


def test_screen_click_text_needs_ocr_and_says_so():
    from jarvis.tools.screen import actions

    result = actions.screen_click_text.invoke({"text": "Sign in"})
    message = (result.content or result.error).lower()
    assert "ocr" in message or "display" in message or "install" in message
