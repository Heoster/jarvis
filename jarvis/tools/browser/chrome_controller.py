"""Chrome controller — the stable public surface over the browser harness.

Historically JARVIS (like the earlier design it grew from) exposed loose
functions such as ``open_url`` / ``click_element`` / ``type_text``.  They still exist here
as thin, well-behaved wrappers, but everything now routes through
:class:`~jarvis.tools.browser.harness.BrowserHarness`, which adds engine
fallback, the element index map, and the DOM→cursor bridge.

Use these functions from code; the ``@tool``-decorated versions in
``actions.py`` are what the LLM sees.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ...utils.logger import get_logger
from .harness import BrowserHarness, HarnessError, get_harness

log = get_logger("browser.controller")

__all__ = [
    "open_url", "click_element", "type_text", "press_key", "get_page_content",
    "take_screenshot", "scroll_page", "go_back", "list_tabs", "close_browser",
    "run_js", "click_index", "launch_browser_terminal",
]


def _run(coro: Any) -> Any:
    """Run a coroutine whether or not we are already inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def open_url(url: str, *, new_tab: bool = False,
             config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Navigate to ``url`` and return title/url/snapshot summary."""
    harness = get_harness(config)
    result = _run(harness.goto(url, new_tab=new_tab, snapshot=True))
    snapshot = result.pop("snapshot", None)
    if snapshot is not None:
        result["title"] = result.get("title") or snapshot.title
        result["elements"] = [e.describe() for e in snapshot.elements[:40]]
        result["text_preview"] = snapshot.text[:1200]
    return result


def click_element(target: Any, *, hybrid: bool = False,
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Click by element index, visible text or CSS selector."""
    return _run(get_harness(config).click(target, hybrid=hybrid or None))


def click_index(index: int, *, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Click the element carrying ``[index]`` in the last snapshot."""
    return click_element(index, config=config)


def type_text(target: Any, text: str, *, enter: bool = False,
              config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _run(get_harness(config).type_text(target, text, enter=enter))


def press_key(key: str, *, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _run(get_harness(config).press(key))


def get_page_content(*, max_chars: int = 6000,
                     config: Optional[Dict[str, Any]] = None) -> str:
    harness = get_harness(config)
    return _run(harness.text_snapshot(max_chars=max_chars))


def scroll_page(direction: str = "down", amount: int = 700,
                *, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _run(get_harness(config).scroll(direction, amount))


def go_back(*, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _run(get_harness(config).back())


def list_tabs(*, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _run(get_harness(config).tabs())


def run_js(expression: str, *, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _run(get_harness(config).evaluate(expression))


def take_screenshot(*, full_page: bool = False,
                    config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _run(get_harness(config).screenshot(full_page=full_page))


def launch_browser_terminal(url: str = "", *, visible: bool = True,
                            config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Launch Chrome as a standalone terminal-spawned process."""
    from .terminal_launcher import launch_chrome

    config = config or {}
    browser_cfg = config.get("browser") or {}
    window = browser_cfg.get("window") or {}
    process = launch_chrome(
        url, browser_type=browser_cfg.get("browser_type", "chromium"),
        headless=not visible,
        port=browser_cfg.get("remote_debugging_port", 9222),
        binary_path=browser_cfg.get("binary_path", ""),
        user_data_dir=browser_cfg.get("user_data_dir", ""),
        window_size=(int(window.get("width", 1440)), int(window.get("height", 900))),
        stealth=bool(browser_cfg.get("stealth", True)))
    return process.status()


def close_browser(*, config: Optional[Dict[str, Any]] = None,
                  kill_browser: bool = False) -> None:
    _run(get_harness(config).close(kill_browser=kill_browser))
