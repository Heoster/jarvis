"""Browser tools exposed to the LLM.

Two families:

* **Page tools** (``browse_open``, ``page_click``, ``page_type``…) — DOM-level
  control through the harness.  Cheap and precise.
* **Cursor tools** (``cursor_click``, ``cursor_type``, ``cursor_move``) — real
  mouse/keyboard at screen coordinates, for anything the DOM cannot reach.

The task descriptions are written for a small model: they say *when* to use the
tool, not just what it does, because that is what keeps a 3B model on track.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ...utils.logger import get_logger
from ...utils.sanitizer import truncate
from ..base import ToolResult, failure, tool
from .dom_parser import render_listing
from .harness import BrowserHarness, HarnessError, get_harness

log = get_logger("browser.tools")


def _harness(config: Optional[Dict[str, Any]] = None) -> BrowserHarness:
    return get_harness(config)


@tool(category="browser", timeout=120)
async def browse_open(url: str, new_tab: bool = False, read_page: bool = True) -> str:
    """Open a URL in the real Chrome window and read the page back as text.

    Use this whenever the operator asks to open, visit, check or look at a
    website (YouTube, Google, a docs page, a dashboard).  Returns the page title,
    the numbered list of clickable elements, and the visible text — so you can
    act on an element by its [index] afterwards.

    Args:
        url: Full URL or domain, e.g. "https://youtube.com" or "google.com"
        new_tab: Open in a new tab instead of the current one
        read_page: Include the page text and element list in the result
    """
    harness = _harness()
    try:
        result = await harness.goto(url, new_tab=new_tab, snapshot=read_page)
    except HarnessError as exc:
        return failure(f"Could not open the browser: {exc}\n"
                       "Hint: install Chrome, or run `pip install playwright && "
                       "playwright install chromium`.")
    snapshot = result.pop("snapshot", None)
    header = (f"✅ Opened **{result.get('title') or url}**\n"
              f"URL: {result.get('url') or url}\n"
              f"Engine: {result.get('engine')}")
    if read_page and snapshot is not None:
        return f"{header}\n\n{snapshot.as_text(max_chars=5200)}"
    return header


@tool(category="browser", timeout=60)
async def page_read(max_chars: int = 5000) -> str:
    """Re-read the current page (after it changed, loaded more, or scrolled).

    Args:
        max_chars: How much page text to return
    """
    harness = _harness()
    try:
        return await harness.text_snapshot(max_chars=max_chars)
    except HarnessError as exc:
        return failure(f"No page to read: {exc}")


@tool(category="browser", timeout=60)
async def page_click(target: str, use_real_cursor: bool = False) -> str:
    """Click something on the current page.

    Accepts an element index from the listing (preferred: "12" or "[12]"), the
    visible text of a link/button, or a CSS selector. Clicks are DOM-located but
    executed with a real cursor movement by default, which works on pages that
    reject synthetic clicks.

    Args:
        target: Element index like "12", visible text like "Sign in", or CSS like "#submit"
        use_real_cursor: Force OS-level cursor clicking even if DOM click is available
    """
    harness = _harness()
    try:
        result = await harness.click(target, hybrid=use_real_cursor or None)
    except HarnessError as exc:
        return failure(f"Click failed: {exc}")
    if not result.get("ok"):
        return failure(f"Could not click '{target}'. Available elements:\n"
                       f"{render_listing(list(harness.elements.values())[:40])}")
    where = result.get("point")
    return (f"✅ Clicked '{target}' via {result.get('method')}"
            + (f" at screen {where}" if where else "")
            + f" ({result.get('engine')})")


@tool(category="browser", timeout=60)
async def page_type(target: str, text: str, press_enter: bool = True) -> str:
    """Type text into an input box on the current page.

    Args:
        target: Element index ("3"), placeholder text ("Search"), or CSS selector
        text: The text to type
        press_enter: Submit by pressing Enter afterwards (default true for searches)
    """
    harness = _harness()
    try:
        result = await harness.type_text(target, text, enter=press_enter)
    except HarnessError as exc:
        return failure(f"Typing failed: {exc}")
    if not result.get("ok"):
        return failure(f"Could not type into '{target}'. Try re-reading the page "
                       "first (`page_read`) and use the element's [index].")
    return (f"✅ Typed {len(text)} characters into '{target}'"
            + (" and pressed Enter" if press_enter else "")
            + f" ({result.get('engine')})")


@tool(category="browser", timeout=30)
async def page_press(key: str) -> str:
    """Press a keyboard key in the browser page.

    Args:
        key: Key name, e.g. "Enter", "Escape", "PageDown", "Control+L", "F5"
    """
    try:
        result = await _harness().press(key)
    except HarnessError as exc:
        return failure(f"Key press failed: {exc}")
    if not result.get("ok"):
        return failure(f"Could not press {key} (no browser page is focused).")
    return f"✅ Pressed {key}"


@tool(category="browser", timeout=30)
async def page_scroll(direction: str = "down", amount: int = 700) -> str:
    """Scroll the page to reveal more content.

    Args:
        direction: "down" or "up"
        amount: Pixels to scroll
    """
    try:
        result = await _harness().scroll(direction, amount)
    except HarnessError as exc:
        return failure(f"Scroll failed: {exc}")
    if not result.get("ok", True):
        return failure(f"Could not scroll {direction} ({result.get('error') or 'no page'})")
    return f"✅ Scrolled {direction} {amount}px ({result.get('engine')})"


@tool(category="browser", timeout=30)
async def page_back() -> str:
    """Go back to the previous page in the browser history."""
    try:
        result = await _harness().back()
    except HarnessError as exc:
        return failure(f"Back failed: {exc}")
    if not result.get("ok", True):
        return failure("Could not go back (no history in this engine/session).")
    return f"✅ Back to {result.get('url', 'previous page')}"


@tool(category="browser", timeout=30)
async def page_forward() -> str:
    """Go forward in the browser history."""
    try:
        result = await _harness().forward()
    except HarnessError as exc:
        return failure(f"Forward failed: {exc}")
    if not result.get("ok", True):
        return failure("Could not go forward (no forward history).")
    return f"✅ Forward to {result.get('url', 'next page')}"


@tool(category="browser", timeout=60)
async def page_tabs() -> str:
    """List the open browser tabs (index, title, URL)."""
    try:
        result = await _harness().tabs()
    except HarnessError as exc:
        return failure(f"Tab listing failed: {exc}")
    tabs = result.get("tabs") or []
    if not tabs:
        return failure("No tabs reported by the current engine.")
    lines = [f"[{t.get('index')}] {t.get('title') or '(untitled)'} — {t.get('url')}"
             for t in tabs]
    return "Open tabs:\n" + "\n".join(lines)


@tool(category="browser", timeout=60)
async def page_screenshot(full_page: bool = False) -> str:
    """Take a screenshot of the page (saved to disk; not analysed by a vision model).

    Use it when the operator asks for a picture of the page, or to attach as an
    artifact — the returned text still comes from the DOM/OCR.

    Args:
        full_page: Capture the entire scrollable page
    """
    try:
        result = await _harness().screenshot(full_page=full_page)
    except HarnessError as exc:
        return failure(f"Screenshot failed: {exc}")
    if not result.get("ok"):
        return f"Screenshot failed: {result.get('error')}"
    return f"✅ Screenshot saved: {result['path']} (via {result.get('method', result.get('engine'))})"


@tool(category="browser", timeout=60)
async def page_js(expression: str) -> str:
    """Run a JavaScript expression in the page and return the value.

    Use for precise extraction (e.g. "document.querySelectorAll('h2').length")
    or to reach parts of the page the element listing does not show.

    Args:
        expression: JavaScript expression, e.g. "document.title"
    """
    try:
        result = await _harness().evaluate(expression)
    except HarnessError as exc:
        return f"JS failed: {exc}"
    if not result.get("ok"):
        return f"JS error: {result.get('error')}"
    return f"Result: {truncate(str(result.get('value')), 3000)}"


@tool(category="browser", timeout=60)
async def page_find(text: str) -> str:
    """Find elements on the current page whose text or URL contains a string.

    Args:
        text: Text to search for among the page's elements
    """
    harness = _harness()
    if harness.snapshot is None:
        await harness.snapshot_page()
    matches = harness.snapshot.find(text) if harness.snapshot else []
    if not matches:
        return f"No elements matched '{text}'."
    return "\n".join(f"[{e.index}] {e.label()} → {e.href or selector_hint(e)}"
                     for e in matches[:20])


def selector_hint(element: Any) -> str:
    return element.selector if hasattr(element, "selector") else ""


@tool(category="browser", timeout=60)
async def browser_engines() -> str:
    """Report which browser engines are available on this machine and which is active."""
    return _harness().report()


@tool(category="browser", timeout=60)
async def browser_launch(url: str = "", visible: bool = True,
                         reuse_profile: bool = False) -> str:
    """Launch Chrome from the terminal as a standalone window (no automation library).

    Use this when you want a plain, human-controlled browser that JARVIS can also
    drive with the cursor, or when the automation engines are unavailable.

    Args:
        url: Page to open (optional)
        visible: Show a real window (false = headless)
        reuse_profile: Use the operator's real Chrome profile (keeps logins)
    """
    import json as _json

    from ...config import load_config
    from .terminal_launcher import launch_chrome

    browser_cfg = load_config().get("browser", {})
    try:
        process = launch_chrome(
            url,
            browser_type=browser_cfg.get("browser_type", "chromium"),
            headless=not visible,
            port=browser_cfg.get("remote_debugging_port", 9222),
            binary_path=browser_cfg.get("binary_path", ""),
            user_data_dir=browser_cfg.get("user_data_dir", "") if reuse_profile else "",
            window_size=(int((browser_cfg.get("window") or {}).get("width", 1440)),
                         int((browser_cfg.get("window") or {}).get("height", 900))),
            stealth=bool(browser_cfg.get("stealth", True)))
    except Exception as exc:
        return failure(f"Launch failed: {exc}")
    return f"✅ Chrome launched from terminal:\n{_json.dumps(process.status(), indent=2)}"


@tool(category="browser", timeout=60)
async def browser_status() -> str:
    """Show the live browser status: engine, page, indexed elements, degradations."""
    harness = _harness()
    status = harness.status()
    lines = [f"engine      : {status['engine']} "
             f"({'running' if status['started'] else 'not started'})",
             f"chain       : {' → '.join(status['chain'])}",
             f"hybrid click: {'real cursor' if status['hybrid_cursor'] else 'DOM only'}",
             f"page        : {truncate(status['current_url'], 70) or '(none)'}",
             f"elements    : {status['elements_indexed']} indexed",
             f"caps        : {', '.join(k for k, v in status['capabilities'].items() if v)}"]
    for step in status["degradations"]:
        lines.append(f"↳ degraded from {step['from']}: {truncate(step['reason'], 60)}")
    return "\n".join(lines)


BROWSER_TOOLS = [
    browse_open, page_read, page_click, page_type, page_press, page_scroll,
    page_back, page_forward, page_tabs, page_screenshot, page_js, page_find,
    browser_engines, browser_launch, browser_status,
]
