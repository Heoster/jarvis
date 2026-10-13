"""The Browser Harness — a real Chrome window, driven two ways at once.

Architecture
------------

::

    ┌─────────────── BrowserHarness ───────────────┐
    │  engine chain:  playwright → selenium → cdp  │
    │  fallback:      screen-only (cursor + OCR)   │
    └───────┬───────────────────────────┬──────────┘
            │ webdriver commands        │ OS-level input
            │ (DOM: selectors, JS)      │ (pyautogui/xdotool: real cursor)
            ▼                           ▼
          Chrome process launched from the terminal (--remote-debugging-port)

The hybrid path is the point: JARVIS *locates* an element through the DOM (exact,
cheap, no vision model) and then *executes* the click with genuine mouse input at
mapped screen coordinates.  That combination behaves like a human using the
browser — which is what makes it work on pages that block pure automation, and
it is also the fallback when a page's DOM is inaccessible (canvas apps, native
dialogs, extension popups).

Every failure degrades gracefully: Playwright → Selenium → raw CDP → screen-only,
with the reason recorded so ``/browser`` can explain what is available.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from ...utils.platform_probe import find_chrome_binary, probe_platform
from ...utils.sanitizer import truncate
from .cdp import CDPClient, cdp_available, websocket_available
from .dom_parser import DOMElement, DOMSnapshot, parse_html, selector_for
from .terminal_launcher import ChromeProcess, launch_chrome, pick_free_port, wait_for_port

log = get_logger("browser.harness")

__all__ = ["BrowserHarness", "HarnessError", "EngineReport", "compute_screen_point",
           "get_harness"]


class HarnessError(RuntimeError):
    """No engine could perform the requested action."""


@dataclass
class EngineReport:
    name: str
    available: bool
    detail: str = ""

    def line(self) -> str:
        return f"{'✓' if self.available else '✗'} {self.name:<11} {self.detail}"


def compute_screen_point(box: Mapping[str, Any], *,
                         window_left: float = 0, window_top: float = 0,
                         viewport_offset: Optional[Tuple[float, float]] = None) -> Tuple[int, int]:
    """Map a DOM bounding box to absolute screen coordinates.

    ``box`` is what the browser reports for ``getBoundingClientRect()`` plus the
    window geometry (``screenX/screenY/outerH/innerH/outerW/innerW``).

    The chrome (browser UI) sits between the window origin and the viewport, so::

        screen_x = window.screenX + (outerW - innerW)/2 + rect.x + rect.width/2
        screen_y = window.screenY + (outerH - innerH)      + rect.y + rect.height/2

    Those two offsets are exactly the window borders and the toolbar height, which
    is why this works without any vision model or calibration step.
    """
    if viewport_offset is not None:
        offset_x, offset_y = viewport_offset
    else:
        outer_w = float(box.get("outerW") or box.get("outerWidth") or 0)
        inner_w = float(box.get("innerW") or box.get("innerWidth") or 0)
        outer_h = float(box.get("outerH") or box.get("outerHeight") or 0)
        inner_h = float(box.get("innerH") or box.get("innerHeight") or 0)
        offset_x = max(0.0, (outer_w - inner_w) / 2.0 + 1.0)
        offset_y = max(0.0, outer_h - inner_h)

    origin_x = float(box.get("screenX", box.get("screen_x", window_left))) + window_left * 0
    origin_y = float(box.get("screenY", box.get("screen_y", window_top))) + window_top * 0
    if not box.get("screenX") and not box.get("screen_x"):
        origin_x = window_left
        origin_y = window_top

    x = origin_x + offset_x + float(box.get("x", 0)) + float(box.get("width", 0)) / 2.0
    y = origin_y + offset_y + float(box.get("y", 0)) + float(box.get("height", 0)) / 2.0
    return int(round(x)), int(round(y))


# ── engines ─────────────────────────────────────────────────────────────────
class BaseEngine:
    """Common contract every browser engine implements."""

    name = "base"
    uses_dom = True

    def __init__(self, harness: "BrowserHarness"):
        self.harness = harness
        self.cfg = harness.cfg
        self.started = False

    # lifecycle
    def available(self) -> Tuple[bool, str]:
        return False, "not implemented"

    def start(self) -> None:
        raise HarnessError(f"engine {self.name} cannot start")

    def stop(self) -> None:
        self.started = False

    def capabilities(self) -> Dict[str, bool]:
        return {"dom": True, "js": False, "screenshot": False, "input": True,
                "tabs": False, "navigation": True}

    # actions — default implementations raise; subclasses override
    async def goto(self, url: str, *, new_tab: bool = False) -> Dict[str, Any]:
        raise HarnessError(f"{self.name}: navigation unsupported")

    async def title(self) -> str:
        return ""

    async def html(self) -> str:
        raise HarnessError(f"{self.name}: cannot read DOM")

    async def text(self) -> str:
        return ""

    async def click_selector(self, selector: str) -> bool:
        raise HarnessError(f"{self.name}: cannot click by selector")

    async def type_selector(self, selector: str, text: str, *, enter: bool = False) -> bool:
        raise HarnessError(f"{self.name}: cannot type into a selector")

    async def press(self, key: str) -> bool:
        raise HarnessError(f"{self.name}: cannot send keys")

    async def scroll(self, direction: str = "down", amount: int = 600) -> bool:
        raise HarnessError(f"{self.name}: cannot scroll")

    async def screenshot(self, path: str, *, full_page: bool = False) -> str:
        raise HarnessError(f"{self.name}: cannot screenshot the page")

    async def evaluate(self, expression: str) -> Any:
        raise HarnessError(f"{self.name}: cannot run JS")

    async def element_box(self, selector: str) -> Optional[Dict[str, float]]:
        raise HarnessError(f"{self.name}: cannot measure elements")

    async def tabs(self) -> List[Dict[str, str]]:
        return []

    async def switch_tab(self, target: Any) -> Dict[str, Any]:
        raise HarnessError(f"{self.name}: this engine cannot switch tabs")

    async def close_tab(self, target: Any) -> Dict[str, Any]:
        raise HarnessError(f"{self.name}: this engine cannot close tabs")

    async def back(self) -> bool:
        return False

    async def forward(self) -> bool:
        return False

    async def current_url(self) -> str:
        return ""


class PlaywrightEngine(BaseEngine):
    """Playwright — the default: reliable, headless-capable, stealthy.

    One rule governs this engine: **every Playwright call runs on the same
    event loop**, the one owned by ``self._thread``.  Playwright's async objects
    (connection, browser, context, page) are bound to the loop they were created
    on; driving them from a second loop — which is exactly what happened when
    ``start()`` ran in a throw-away ``asyncio.run()`` and the tools then awaited
    ``goto()`` on the agent's loop — surfaces as the baffling
    ``BrowserContext.new_page: 'NoneType' object has no attribute 'send'`` and
    the whole engine is written off as broken.

    So ``start()`` boots Playwright on that loop and every public action is a
    thin dispatcher: the body stays a coroutine, but it is *executed* on the
    engine's loop and awaited back on the caller's (``asyncio.wrap_future``),
    which keeps the agent's loop free instead of blocking it.
    """

    name = "playwright"
    uses_dom = True

    def __init__(self, harness: "BrowserHarness"):
        super().__init__(harness)
        self._pw = None
        self._browser = None
        self._page = None
        self._context = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    # ── the dedicated loop ─────────────────────────────────────────────────
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start (once) the background thread that owns every Playwright call."""
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def runner() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            try:
                loop.run_forever()
            finally:                                # stop() closed the browser first
                with contextlib.suppress(Exception):
                    loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()

        self._loop = loop
        self._thread = threading.Thread(target=runner, name="jarvis-playwright",
                                        daemon=True)
        self._thread.start()
        ready.wait(10)
        return loop

    def _run(self, coro: Any, *, timeout: Optional[float] = None) -> Any:
        """Run a coroutine **on the engine loop** and block until it finishes."""
        loop = self._ensure_loop()
        limit = (float(timeout) if timeout else
                 float(self.cfg.get("timeout", 30000)) / 1000.0 + 60.0)
        return asyncio.run_coroutine_threadsafe(coro, loop).result(limit)

    async def _dispatch(self, coro: Any) -> Any:
        """Await ``coro`` on the engine loop from whatever loop the caller uses."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return await coro                     # not booted through _ensure_loop
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    def available(self) -> Tuple[bool, str]:
        try:
            import playwright  # noqa: F401
        except Exception:
            return False, "pip install playwright && playwright install chromium"
        binary = find_chrome_binary()
        return True, ("system Chrome will be reused" if binary
                      else "uses bundled Chromium")

    def _launch_kwargs(self, *, with_debug_port: bool = True) -> Dict[str, Any]:
        cfg = self.cfg
        binary = cfg.get("binary_path") or find_chrome_binary()
        args = [
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]
        if binary and with_debug_port:
            args.append("--remote-debugging-port=" +
                        str(cfg.get("remote_debugging_port") or pick_free_port()))
        kwargs: Dict[str, Any] = {"args": args}
        if binary:
            kwargs["executable_path"] = binary
        if cfg.get("headless") and probe_platform().has_display:
            kwargs["headless"] = True
        elif not probe_platform().has_display:
            kwargs["headless"] = True
        return kwargs

    async def _boot(self, *, with_debug_port: bool = True) -> None:
        from playwright.async_api import async_playwright  # type: ignore

        self._pw = await async_playwright().start()
        browser_type = self.cfg.get("browser_type", "chromium")
        if browser_type == "firefox":
            launcher = self._pw.firefox
        elif browser_type == "webkit":
            launcher = self._pw.webkit
        else:
            launcher = self._pw.chromium
        self._browser = await launcher.launch(**self._launch_kwargs(
            with_debug_port=with_debug_port))
        window = self.cfg.get("window") or {}
        context_kwargs: Dict[str, Any] = {
            "viewport": {"width": int(window.get("width", 1440)),
                         "height": int(window.get("height", 900))},
            "locale": "en-IN", "timezone_id": "Asia/Kolkata",
        }
        if self.cfg.get("user_agent"):
            context_kwargs["user_agent"] = self.cfg["user_agent"]
        if self.cfg.get("user_data_dir"):
            context_kwargs["user_data_dir"] = self.cfg["user_data_dir"]
        self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(float(self.cfg.get("timeout", 30000)))
        if self.cfg.get("stealth", True):
            await self._apply_stealth(self._page)
        # Prove the pipe really works before the harness trusts this engine:
        # a Chrome that died on startup (port already taken, bad profile, …)
        # fails on the first real command, not here, unless we make it.
        await self._page.goto("about:blank", timeout=float(self.cfg.get("timeout", 30000)))
        self.started = True

    def start(self) -> None:
        self._ensure_loop()
        errors: List[str] = []
        # First try keeps the debug port (the CDP engine can attach to it), then
        # retry without: port 9222 already being in use is the classic reason
        # Chrome exits instantly and new_page() reports a broken connection.
        for attempt, with_debug_port in ((1, True), (2, False)):
            try:
                self._run(self._boot(with_debug_port=with_debug_port))
                return
            except Exception as exc:
                errors.append(f"attempt {attempt}: {truncate(str(exc), 160)}")
                log.warning(f"playwright start failed ({errors[-1]})")
                self._reset()
        raise HarnessError("playwright could not start — " + " | ".join(errors))

    async def _shutdown(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass

    def _reset(self) -> None:
        """Drop the half-started objects (and try to close them) after a failure."""
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(),
                                                 self._loop).result(20)
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None
        self.started = False

    @staticmethod
    async def _apply_stealth(page: Any) -> None:
        script = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = window.chrome || {runtime: {}};
        Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en', 'hi']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        """
        try:
            await page.add_init_script(script)
        except Exception:
            pass

    async def _goto(self, url: str, *, new_tab: bool = False) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://", "about:", "file:", "chrome://")):
            url = f"https://{url}"
        if new_tab:
            page = await self._context.new_page()
            if self.cfg.get("stealth", True):
                await self._apply_stealth(page)
        else:
            page = self._page
        await page.goto(url, wait_until="domcontentloaded",
                        timeout=float(self.cfg.get("timeout", 30000)))
        self._page = page
        await asyncio.sleep(0.35)
        return {"url": page.url, "title": await page.title()}

    # ── actions (each public method dispatches its body onto the engine loop) ─
    async def goto(self, url: str, *, new_tab: bool = False) -> Dict[str, Any]:
        return await self._dispatch(self._goto(url, new_tab=new_tab))

    async def title(self) -> str:
        return await self._dispatch(self._title())

    async def _title(self) -> str:
        return await self._page.title() if self._page else ""

    async def html(self) -> str:
        return await self._dispatch(self._html())

    async def _html(self) -> str:
        return await self._page.content() if self._page else ""

    async def text(self) -> str:
        return await self._dispatch(self._text())

    async def _text(self) -> str:
        return await self._page.inner_text("body") if self._page else ""

    async def click_selector(self, selector: str) -> bool:
        return await self._dispatch(self._click_selector(selector))

    async def _click_selector(self, selector: str) -> bool:
        await self._page.click(selector, timeout=6000)
        await asyncio.sleep(0.25)
        return True

    async def type_selector(self, selector: str, text: str, *, enter: bool = False) -> bool:
        return await self._dispatch(self._type_selector(selector, text, enter=enter))

    async def _type_selector(self, selector: str, text: str, *, enter: bool = False) -> bool:
        await self._page.fill(selector, text, timeout=6000)
        if enter:
            await self._page.press(selector, "Enter")
            await asyncio.sleep(0.4)
        return True

    async def press(self, key: str) -> bool:
        return await self._dispatch(self._press(key))

    async def _press(self, key: str) -> bool:
        await self._page.keyboard.press(key)
        return True

    async def scroll(self, direction: str = "down", amount: int = 600) -> bool:
        return await self._dispatch(self._scroll(direction, amount))

    async def _scroll(self, direction: str = "down", amount: int = 600) -> bool:
        delta = amount if direction == "down" else -amount
        await self._page.mouse.wheel(0, delta)
        await asyncio.sleep(0.2)
        return True

    async def screenshot(self, path: str, *, full_page: bool = False) -> str:
        return await self._dispatch(self._screenshot(path, full_page=full_page))

    async def _screenshot(self, path: str, *, full_page: bool = False) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=path, full_page=full_page)
        return path

    async def evaluate(self, expression: str) -> Any:
        return await self._dispatch(self._evaluate(expression))

    async def _evaluate(self, expression: str) -> Any:
        return await self._page.evaluate(expression)

    async def element_box(self, selector: str) -> Optional[Dict[str, float]]:
        return await self._dispatch(self._element_box(selector))

    async def _element_box(self, selector: str) -> Optional[Dict[str, float]]:
        script = """
        (sel) => {
          const el = document.querySelector(sel);
          if (!el) return null;
          el.scrollIntoView({block: 'center', inline: 'center'});
          const r = el.getBoundingClientRect();
          return {x: r.left, y: r.top, width: r.width, height: r.height,
                  dpr: window.devicePixelRatio || 1,
                  screenX: window.screenX || 0, screenY: window.screenY || 0,
                  outerH: window.outerHeight || 0, innerH: window.innerHeight || 0,
                  outerW: window.outerWidth || 0, innerW: window.innerWidth || 0,
                  scrollX: window.scrollX, scrollY: window.scrollY,
                  windowId: null};
        }
        """
        try:
            box = await self._page.evaluate(script, selector)
        except Exception:
            return None
        return dict(box) if box else None

    async def tabs(self) -> List[Dict[str, str]]:
        return await self._dispatch(self._tabs())

    async def _tabs(self) -> List[Dict[str, str]]:
        out = []
        for index, page in enumerate(self._context.pages):
            try:
                out.append({"index": str(index), "title": await page.title(),
                            "url": page.url,
                            "active": str(page is self._page)})
            except Exception:
                continue
        return out

    async def _page_for(self, target: Any):
        pages = list(self._context.pages)
        if isinstance(target, bool):
            raise HarnessError("tab target must be an index or a title/url fragment")
        if isinstance(target, int) or (isinstance(target, str) and target.strip().isdigit()):
            index = int(target)
            if index < 0 or index >= len(pages):
                raise HarnessError(f"there is no tab {index} (open tabs: {len(pages)})")
            return pages[index]
        needle = str(target).strip().lower()
        for page in pages:
            if needle and needle in (page.url or "").lower():
                return page
        for page in pages:
            try:
                title = (await page.title()).lower()
            except Exception:
                continue
            if needle and needle in title:
                return page
        raise HarnessError(f"no open tab matches {target!r}")

    async def switch_tab(self, target: Any) -> Dict[str, Any]:
        return await self._dispatch(self._switch_tab(target))

    async def _switch_tab(self, target: Any) -> Dict[str, Any]:
        page = await self._page_for(target)
        await page.bring_to_front()
        self._page = page
        return {"index": self._context.pages.index(page), "url": page.url,
                "title": await page.title(), "engine": self.name}

    async def close_tab(self, target: Any) -> Dict[str, Any]:
        return await self._dispatch(self._close_tab(target))

    async def _close_tab(self, target: Any) -> Dict[str, Any]:
        page = await self._page_for(target)
        index = self._context.pages.index(page)
        was_current = page is self._page
        await page.close()
        if was_current and self._context.pages:
            self._page = self._context.pages[min(index, len(self._context.pages) - 1)]
        return {"closed": index, "remaining": len(self._context.pages),
                "active_url": self._page.url if self._page else "", "engine": self.name}

    async def back(self) -> bool:
        return await self._dispatch(self._back())

    async def _back(self) -> bool:
        await self._page.go_back()
        return True

    async def forward(self) -> bool:
        return await self._dispatch(self._forward())

    async def _forward(self) -> bool:
        await self._page.go_forward()
        return True

    async def current_url(self) -> str:
        return await self._dispatch(self._current_url())

    async def _current_url(self) -> str:
        return self._page.url if self._page else ""

    def capabilities(self) -> Dict[str, bool]:
        return {"dom": True, "js": True, "screenshot": True, "input": True,
                "tabs": True, "navigation": True, "stealth": True}

    def stop(self) -> None:
        try:
            if self._loop is not None and self._thread is not None:
                if self._thread.is_alive():
                    self._run(self._shutdown(), timeout=20)
        except Exception:
            pass
        self._pw = self._browser = self._context = self._page = None
        self.started = False
        loop, thread = self._loop, self._thread
        self._loop, self._thread = None, None
        if loop is not None and thread is not None and thread.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)


class SeleniumEngine(BaseEngine):
    """Selenium — the industry standard fallback for stubborn pages."""

    name = "selenium"

    def __init__(self, harness: "BrowserHarness"):
        super().__init__(harness)
        self._driver = None

    def available(self) -> Tuple[bool, str]:
        try:
            import selenium  # noqa: F401
        except Exception:
            return False, "pip install selenium"
        if not (find_chrome_binary() or shutil.which("chromedriver")):
            return False, "needs Chrome (or chromedriver on PATH)"
        return True, "uses system Chrome + Selenium Manager"

    def start(self) -> None:
        from selenium import webdriver  # type: ignore
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        binary = self.cfg.get("binary_path") or find_chrome_binary()
        if binary:
            options.binary_location = binary
        window = self.cfg.get("window") or {}
        options.add_argument(f"--window-size={int(window.get('width', 1440))},"
                             f"{int(window.get('height', 900))}")
        if self.cfg.get("stealth", True):
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
        if self.cfg.get("user_data_dir"):
            options.add_argument(f"--user-data-dir={self.cfg['user_data_dir']}")
        if self.cfg.get("headless") or not probe_platform().has_display:
            options.add_argument("--headless=new")
        service = Service() if shutil.which("chromedriver") else Service()
        self._driver = webdriver.Chrome(service=service, options=options)
        self._driver.set_page_load_timeout(float(self.cfg.get("timeout", 30000)) / 1000)
        if self.cfg.get("stealth", True):
            try:
                self._driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": "Object.defineProperty(navigator, 'webdriver',"
                              "{get: () => undefined});"})
            except Exception:
                pass
        self.started = True

    async def goto(self, url: str, *, new_tab: bool = False) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://", "about:", "file:")):
            url = f"https://{url}"
        driver = self._driver
        if new_tab:
            driver.switch_to.new_window("tab")
        driver.get(url)
        time.sleep(0.3)
        return {"url": driver.current_url, "title": driver.title}

    async def title(self) -> str:
        return self._driver.title

    async def html(self) -> str:
        return self._driver.page_source

    async def text(self) -> str:
        try:
            return self._driver.find_element("tag name", "body").text
        except Exception:
            return ""

    async def click_selector(self, selector: str) -> bool:
        from selenium.webdriver.common.by import By

        element = self._driver.find_element(By.CSS_SELECTOR, selector)
        element.click()
        time.sleep(0.3)
        return True

    async def type_selector(self, selector: str, text: str, *, enter: bool = False) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        element = self._driver.find_element(By.CSS_SELECTOR, selector)
        element.clear()
        element.send_keys(text)
        if enter:
            element.send_keys(Keys.ENTER)
            time.sleep(0.4)
        return True

    async def press(self, key: str) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        target = self._driver.switch_to.active_element
        target.send_keys(getattr(Keys, key.upper(), key))
        return True

    async def scroll(self, direction: str = "down", amount: int = 600) -> bool:
        delta = amount if direction == "down" else -amount
        self._driver.execute_script(f"window.scrollBy(0, {delta});")
        return True

    async def screenshot(self, path: str, *, full_page: bool = False) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if full_page:
            from selenium.webdriver import Chrome

            original = self._driver.get_window_size()
            height = self._driver.execute_script(
                "return Math.max(document.body.scrollHeight,"
                " document.documentElement.scrollHeight);")
            self._driver.set_window_size(original["width"], min(4000, height))
            self._driver.save_screenshot(path)
            self._driver.set_window_size(**original)
        else:
            self._driver.save_screenshot(path)
        return path

    async def evaluate(self, expression: str) -> Any:
        return self._driver.execute_script(f"return ({expression});")

    async def element_box(self, selector: str) -> Optional[Dict[str, float]]:
        js = """
        const el = document.querySelector(arguments[0]);
        if (!el) return null;
        el.scrollIntoView({block:'center', inline:'center'});
        const r = el.getBoundingClientRect();
        return {x: r.left, y: r.top, width: r.width, height: r.height,
                dpr: window.devicePixelRatio || 1,
                screenX: window.screenX || 0, screenY: window.screenY || 0,
                outerH: window.outerHeight || 0, innerH: window.innerHeight || 0,
                outerW: window.outerWidth || 0, innerW: window.innerWidth || 0,
                scrollX: window.scrollX, scrollY: window.scrollY};
        """
        try:
            return self._driver.execute_script(js, selector)
        except Exception:
            return None

    async def tabs(self) -> List[Dict[str, str]]:
        handles = self._driver.window_handles
        current = self._driver.current_window_handle
        out = []
        for index, handle in enumerate(handles):
            self._driver.switch_to.window(handle)
            out.append({"index": str(index), "title": self._driver.title,
                        "url": self._driver.current_url,
                        "active": str(handle == current)})
        self._driver.switch_to.window(current)
        return out

    def _window_for(self, target: Any) -> str:
        handles = self._driver.window_handles
        if isinstance(target, int) or (isinstance(target, str) and str(target).strip().isdigit()):
            index = int(target)
            if index < 0 or index >= len(handles):
                raise HarnessError(f"there is no tab {index} (open tabs: {len(handles)})")
            return handles[index]
        needle = str(target).strip().lower()
        current = self._driver.current_window_handle
        for handle in handles:
            try:
                self._driver.switch_to.window(handle)
            except Exception:
                continue
            if needle and (needle in self._driver.current_url.lower()
                           or needle in self._driver.title.lower()):
                return handle
        self._driver.switch_to.window(current)
        raise HarnessError(f"no open tab matches {target!r}")

    async def switch_tab(self, target: Any) -> Dict[str, Any]:
        handle = self._window_for(target)
        self._driver.switch_to.window(handle)
        return {"index": self._driver.window_handles.index(handle),
                "url": self._driver.current_url, "title": self._driver.title,
                "engine": self.name}

    async def close_tab(self, target: Any) -> Dict[str, Any]:
        handle = self._window_for(target)
        handles = self._driver.window_handles
        self._driver.switch_to.window(handle)
        self._driver.close()
        remaining = self._driver.window_handles
        if remaining:
            self._driver.switch_to.window(remaining[0])
        return {"closed": handles.index(handle), "remaining": len(remaining),
                "active_url": self._driver.current_url if remaining else "",
                "engine": self.name}

    async def back(self) -> bool:
        self._driver.back()
        return True

    async def forward(self) -> bool:
        self._driver.forward()
        return True

    async def current_url(self) -> str:
        return self._driver.current_url

    def capabilities(self) -> Dict[str, bool]:
        return {"dom": True, "js": True, "screenshot": True, "input": True,
                "tabs": True, "navigation": True, "stealth": True}

    def stop(self) -> None:
        try:
            if self._driver:
                self._driver.quit()
        except Exception:
            pass
        self.started = False


class CDPEngine(BaseEngine):
    """Attach to a Chrome JARVIS launched from the terminal, via DevTools."""

    name = "cdp"

    def __init__(self, harness: "BrowserHarness"):
        super().__init__(harness)
        self.client: Optional[CDPClient] = None
        self.process: Optional[ChromeProcess] = None
        self.port: int = int(harness.cfg.get("remote_debugging_port") or 9222)

    def available(self) -> Tuple[bool, str]:
        try:
            import httpx  # noqa: F401
        except Exception:
            return False, "pip install httpx"
        if not (find_chrome_binary() or cdp_available(self.port)):
            return False, "no Chrome binary and no debugger on port " + str(self.port)
        ws = "full control (websockets installed)" if websocket_available() \
            else "navigation + tabs only (pip install websockets for DOM control)"
        return True, ws

    def start(self) -> None:
        if not cdp_available(self.port):
            self.process = launch_chrome(
                "", browser_type=self.cfg.get("browser_type", "chromium"),
                headless=bool(self.cfg.get("headless")) or not probe_platform().has_display,
                port=self.port, binary_path=self.cfg.get("binary_path", ""),
                user_data_dir=self.cfg.get("user_data_dir", ""),
                window_size=(int((self.cfg.get("window") or {}).get("width", 1440)),
                             int((self.cfg.get("window") or {}).get("height", 900))),
                stealth=bool(self.cfg.get("stealth", True)))
        self.client = CDPClient(self.port, timeout=float(self.cfg.get("timeout", 30000)) / 1000)
        self.client.connect()
        self.started = True

    async def goto(self, url: str, *, new_tab: bool = False) -> Dict[str, Any]:
        assert self.client is not None
        target = self.client.current()
        if new_tab or target is None:
            target = self.client.new_tab(url)
            if target and not websocket_available():
                return {"url": target.url, "title": target.title}
        if target is not None and websocket_available():
            self.client.connect(target)
            final = self.client.navigate(url)
            return {"url": final, "title": self.client.title()}
        return {"url": url, "title": ""}

    async def title(self) -> str:
        if self.client and websocket_available():
            return self.client.title()
        target = self.client.current() if self.client else None
        return target.title if target else ""

    async def html(self) -> str:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` to read the DOM")
        return self.client.html()

    async def text(self) -> str:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` to read text")
        return self.client.text()

    async def click_selector(self, selector: str) -> bool:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` to click")
        return self.client.click_selector(selector)

    async def type_selector(self, selector: str, text: str, *, enter: bool = False) -> bool:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` to type")
        ok = self.client.fill_selector(selector, text)
        if enter:
            ok = self.client.submit_enter(selector) and ok
        return ok

    async def press(self, key: str) -> bool:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` to send keys")
        self.client.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        self.client.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        return True

    async def scroll(self, direction: str = "down", amount: int = 600) -> bool:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` to scroll")
        return self.client.scroll(direction=direction, amount=amount)

    async def screenshot(self, path: str, *, full_page: bool = False) -> str:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` for screenshots")
        return self.client.screenshot(path, full_page=full_page)

    async def evaluate(self, expression: str) -> Any:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` for JS")
        return self.client.evaluate(expression)

    async def element_box(self, selector: str) -> Optional[Dict[str, float]]:
        if not (self.client and websocket_available()):
            raise HarnessError("raw CDP needs `pip install websockets` to measure elements")
        return self.client.element_box(selector)

    async def tabs(self) -> List[Dict[str, str]]:
        if not self.client:
            return []
        current = self.client.current()
        return [{"index": str(i), "title": t.title, "url": t.url,
                 "active": str(bool(current and t.id == current.id))}
                for i, t in enumerate(self.client.targets())]

    def _target_for(self, target: Any):
        assert self.client is not None
        targets = self.client.targets()
        if isinstance(target, int) or (isinstance(target, str) and str(target).strip().isdigit()):
            index = int(target)
            if index < 0 or index >= len(targets):
                raise HarnessError(f"there is no tab {index} (open tabs: {len(targets)})")
            return targets[index]
        needle = str(target).strip().lower()
        for item in targets:
            if needle and (needle in (item.url or "").lower()
                           or needle in (item.title or "").lower()):
                return item
        raise HarnessError(f"no open tab matches {target!r}")

    async def switch_tab(self, target: Any) -> Dict[str, Any]:
        item = self._target_for(target)
        self.client.switch_to(item)
        return {"index": self.client.targets().index(item), "url": item.url,
                "title": item.title, "engine": self.name}

    async def close_tab(self, target: Any) -> Dict[str, Any]:
        item = self._target_for(target)
        index = self.client.targets().index(item)
        closed = self.client.close_tab(item.id)
        if not closed:
            raise HarnessError(f"could not close tab {index} over the DevTools protocol")
        if websocket_available():
            remaining = self.client.targets()
            if remaining:
                self.client.connect(remaining[min(index, len(remaining) - 1)])
        return {"closed": index, "remaining": len(self.client.targets()),
                "engine": self.name}

    async def current_url(self) -> str:
        target = self.client.current() if self.client else None
        return target.url if target else ""

    def capabilities(self) -> Dict[str, bool]:
        ws = websocket_available()
        return {"dom": ws, "js": ws, "screenshot": ws, "input": True,
                "tabs": True, "navigation": True, "stealth": True}

    def stop(self) -> None:
        if self.client:
            self.client.close()
        self.started = False


class ScreenOnlyEngine(BaseEngine):
    """Last resort: no DOM access — drive the window with the real cursor + OCR.

    Useful for canvas apps, native file pickers, and any page where the driver
    cannot attach.  It still navigates by launching Chrome from the terminal.
    """

    name = "screen"
    uses_dom = False

    def __init__(self, harness: "BrowserHarness"):
        super().__init__(harness)
        self.process: Optional[ChromeProcess] = None
        self._last_url = ""

    def available(self) -> Tuple[bool, str]:
        platform_info = probe_platform()
        if not platform_info.has_display:
            return False, "no display (headless environment)"
        if not platform_info.can_control_mouse:
            return False, ("no mouse backend (install pyautogui, or xdotool/ydotool "
                           "on Linux)")
        return True, "cursor + OCR only (no DOM)"

    def start(self) -> None:
        # Reuse the window this harness already opened instead of spawning a new
        # Chrome every time the engine is (re)started.
        existing = getattr(self.harness, "screen_engine_process", None)
        if existing is not None and existing.is_running():
            self.process = existing
        else:
            self.process = launch_chrome(
                "", browser_type=self.cfg.get("browser_type", "chromium"),
                headless=False, binary_path=self.cfg.get("binary_path", ""),
                user_data_dir=self.cfg.get("user_data_dir", ""),
                window_size=(int((self.cfg.get("window") or {}).get("width", 1440)),
                             int((self.cfg.get("window") or {}).get("height", 900))),
                stealth=bool(self.cfg.get("stealth", True)))
            self.harness.screen_engine_process = self.process
        self.started = True

    async def goto(self, url: str, *, new_tab: bool = False) -> Dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        opened = False
        if self.process and self.process.is_running():
            opened = self.process.open_tab(url)
        if not opened:
            self.process = launch_chrome(
                url, browser_type=self.cfg.get("browser_type", "chromium"),
                headless=False, port=pick_free_port(),
                binary_path=self.cfg.get("binary_path", ""),
                user_data_dir=self.cfg.get("user_data_dir", ""),
                stealth=bool(self.cfg.get("stealth", True)))
            self.harness.screen_engine_process = self.process
        self._last_url = url
        return {"url": url, "title": "", "note": "screen-only engine: page title "
                                                "unavailable, use OCR to read"}

    async def current_url(self) -> str:
        return self._last_url

    def capabilities(self) -> Dict[str, bool]:
        return {"dom": False, "js": False, "screenshot": True, "input": True,
                "tabs": False, "navigation": True, "stealth": False,
                "ocr": True}

    def stop(self) -> None:
        # leave the operator's window open on purpose
        self.started = False


_ENGINES: Dict[str, type[BaseEngine]] = {
    "playwright": PlaywrightEngine,
    "selenium": SeleniumEngine,
    "cdp": CDPEngine,
    "screen": ScreenOnlyEngine,
}


# ── harness ─────────────────────────────────────────────────────────────────
class BrowserHarness:
    """One object that owns the browser, the engine chain and the element map."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None):
        self.config = dict(config or {})
        self.cfg = dict(self.config.get("browser") or {})
        self.engine_chain = self._chain()
        self.engine: Optional[BaseEngine] = None
        self.engine_name: str = ""
        self.history: List[Dict[str, Any]] = []
        self.snapshot: Optional[DOMSnapshot] = None
        self.elements: Dict[int, DOMElement] = {}
        self.started_at: float = 0.0
        self.degradations: List[Dict[str, Any]] = []
        self.hybrid_cfg = dict(self.cfg.get("hybrid") or {})
        self.hybrid_enabled = bool(self.hybrid_cfg.get("enabled", True))
        self.screenshot_dir = Path(self.cfg.get("screenshot_dir")
                                   or (Path(__file__).resolve().parents[3]
                                       / "data" / "screenshots"))
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._last_box: Dict[str, Any] = {}
        # the Chrome window the screen engine opened, reused across engine restarts
        self.screen_engine_process: Optional[ChromeProcess] = None

    def _chain(self) -> List[str]:
        requested = str(self.cfg.get("engine", "auto")).lower()
        if requested and requested not in ("auto", ""):
            chain = [requested]
        else:
            chain = [str(name).lower() for name in
                     (self.cfg.get("engine_chain") or ["playwright", "selenium", "cdp"])]
        if "screen" not in chain:
            chain.append("screen")
        return [name for name in chain if name in _ENGINES]

    # ── lifecycle ──────────────────────────────────────────────────────────
    def engine_reports(self) -> List[EngineReport]:
        reports: List[EngineReport] = []
        for name in self.engine_chain:
            engine_cls = _ENGINES[name]
            try:
                engine = engine_cls(self)
                ok, detail = engine.available()
            except Exception as exc:
                ok, detail = False, str(exc)
            reports.append(EngineReport(name, ok, detail))
        return reports

    async def ensure(self) -> BaseEngine:
        """Start the best available engine (once), degrading on failure."""
        if self.engine is not None and self.engine.started:
            return self.engine

        errors: List[str] = []
        for name in self.engine_chain:
            engine_cls = _ENGINES[name]
            engine = engine_cls(self)
            ok, detail = engine.available()
            if not ok:
                errors.append(f"{name}: {detail}")
                continue
            try:
                engine.start()
            except Exception as exc:
                errors.append(f"{name}: start failed — {exc}")
                log.warning(f"engine {name} failed to start: {exc}")
                continue
            self.engine = engine
            self.engine_name = name
            self.started_at = time.time()
            log.info(f"browser engine: {name} ({detail})")
            return engine

        raise HarnessError("no browser engine could start. "
                           + " | ".join(errors) +
                           " — install one: pip install playwright && "
                           "playwright install chromium, or install Chrome.")

    async def degrade(self, reason: str) -> bool:
        """Move to the next engine in the chain after a failure."""
        if self.engine is None:
            return False
        failed = self.engine_name
        self.degradations.append({"from": failed, "reason": truncate(reason, 200),
                                  "at": time.time()})
        log.warning(f"engine {failed} failed ({truncate(reason, 120)}); degrading")
        try:
            self.engine.stop()
        except Exception:
            pass
        self.engine = None
        try:
            remaining = self.engine_chain[self.engine_chain.index(failed) + 1:]
        except ValueError:
            remaining = []
        if not remaining:
            raise HarnessError(f"all engines exhausted after {failed}: {reason}")
        # the screen engine needs no library, so it is always the last resort
        self.engine_chain = list(remaining)
        if "screen" not in self.engine_chain:
            self.engine_chain.append("screen")
        await self.ensure()
        return True

    # ── navigation ─────────────────────────────────────────────────────────
    async def goto(self, url: str, *, new_tab: bool = False,
                   snapshot: bool = True) -> Dict[str, Any]:
        engine = await self.ensure()
        try:
            result = await engine.goto(url, new_tab=new_tab)
        except Exception as first_error:
            await self.degrade(str(first_error))
            result = await self.engine.goto(url, new_tab=new_tab)  # type: ignore[union-attr]
        self._log("goto", {"url": url, "new_tab": new_tab, "engine": self.engine_name})
        out = {"ok": True, "engine": self.engine_name, **result}
        if snapshot and engine.uses_dom:
            snap = await self.snapshot_page()
            out["snapshot"] = snap
            out["elements"] = len(snap.elements)
        return out

    async def snapshot_page(self, *, max_elements: Optional[int] = None,
                            max_text_chars: Optional[int] = None) -> DOMSnapshot:
        """Refresh the DOM snapshot + index map."""
        engine = await self.ensure()
        dom_cfg = dict(self.cfg.get("dom") or {})
        limit = int(max_elements or dom_cfg.get("max_elements", 120))
        text_limit = int(max_text_chars or dom_cfg.get("max_text_chars", 6000))
        url = ""
        try:
            url = await engine.current_url()
        except Exception:
            pass
        html = ""
        try:
            html = await engine.html()
        except Exception as exc:
            if engine.uses_dom:
                await self.degrade(f"cannot read DOM: {exc}")
                return await self.snapshot_page(max_elements=max_elements,
                                                max_text_chars=max_text_chars)
        snapshot = parse_html(html, url=url, max_elements=limit,
                              max_text_chars=text_limit * 2)
        try:
            snapshot.title = snapshot.title or await engine.title()
        except Exception:
            pass
        self.snapshot = snapshot
        self.elements = {element.index: element for element in snapshot.elements}
        self._log("snapshot", {"url": url, "elements": len(snapshot.elements),
                               "engine": self.engine_name})
        return snapshot

    async def text_snapshot(self, *, max_chars: int = 6000) -> str:
        snapshot = await self.snapshot_page(max_text_chars=max_chars)
        return snapshot.as_text(max_chars=max_chars)

    # ── element actions ────────────────────────────────────────────────────
    def resolve(self, index_or_selector: Any) -> Tuple[Optional[DOMElement], str]:
        """Accept ``12``, ``"[12]"``, ``"#login"`` or ``"button.submit"``."""
        if isinstance(index_or_selector, int):
            index = index_or_selector
            return self.elements.get(index), (self.elements.get(index) and
                                              selector_for(self.elements[index]) or "")
        text = str(index_or_selector).strip()
        match = text.strip("[]")
        if match.isdigit():
            index = int(match)
            element = self.elements.get(index)
            return element, selector_for(element) if element else ""
        for element in self.elements.values():
            if text.lower() in (element.text or "").lower() and element.text:
                return element, selector_for(element)
        return None, text

    async def click(self, target: Any, *, hybrid: Optional[bool] = None) -> Dict[str, Any]:
        """Click an element by index, text or selector — DOM-located, OS-executed."""
        engine = await self.ensure()
        element, selector = self.resolve(target)
        use_hybrid = self.hybrid_enabled if hybrid is None else hybrid
        method = "dom"

        if use_hybrid and element is not None:
            box = await self._box_for(selector) if selector else None
            if box:
                try:
                    from ..screen.mouse_keyboard import click_screen_point
                    move_ms = int(self.hybrid_cfg.get("move_duration_ms", 260))
                    x, y = self.screen_point(box)
                    click_screen_point(x, y, move_duration_ms=move_ms,
                                       jitter=bool(self.hybrid_cfg.get("jitter", True)))
                    settle = int(self.hybrid_cfg.get("click_settle_ms", 350)) / 1000
                    await asyncio.sleep(settle)
                    self._log("click", {"target": str(target), "method": "hybrid-cursor",
                                        "point": [x, y], "selector": selector})
                    return {"ok": True, "method": "hybrid-cursor", "point": [x, y],
                            "element": element.describe(), "engine": self.engine_name}
                except Exception as exc:
                    log.info(f"hybrid click unavailable ({exc}); using DOM click")

        try:
            ok = await engine.click_selector(selector)
        except Exception as exc:
            await self.degrade(str(exc))
            ok = await self.engine.click_selector(selector)  # type: ignore[union-attr]
        self._log("click", {"target": str(target), "method": method,
                            "selector": selector, "ok": bool(ok)})
        return {"ok": bool(ok), "method": method, "selector": selector,
                "element": element.describe() if element else None,
                "engine": self.engine_name}

    async def type_text(self, target: Any, text: str, *, enter: bool = False,
                        hybrid: bool = False) -> Dict[str, Any]:
        engine = await self.ensure()
        element, selector = self.resolve(target)
        if hybrid and element is not None:
            box = await self._box_for(selector)
            if box:
                try:
                    from ..screen.mouse_keyboard import (
                        click_screen_point, type_text as type_keys,
                    )

                    x, y = self.screen_point(box)
                    click_screen_point(x, y)
                    await asyncio.sleep(0.15)
                    type_keys(text, interval=0.012)
                    if enter:
                        from ..screen.mouse_keyboard import press_key
                        press_key("enter")
                    self._log("type", {"target": str(target), "method": "hybrid-cursor"})
                    return {"ok": True, "method": "hybrid-cursor", "point": [x, y],
                            "engine": self.engine_name}
                except Exception as exc:
                    log.info(f"hybrid typing unavailable ({exc})")
        try:
            ok = await engine.type_selector(selector, text, enter=enter)
        except Exception as exc:
            await self.degrade(str(exc))
            ok = await self.engine.type_selector(selector, text, enter=enter)  # type: ignore
        self._log("type", {"target": str(target), "chars": len(text), "enter": enter})
        return {"ok": bool(ok), "selector": selector, "chars": len(text),
                "element": element.describe() if element else None,
                "engine": self.engine_name}

    async def press(self, key: str) -> Dict[str, Any]:
        engine = await self.ensure()
        try:
            ok = await engine.press(key)
        except Exception as exc:
            await self.degrade(str(exc))
            ok = await self.engine.press(key)  # type: ignore[union-attr]
        self._log("press", {"key": key})
        return {"ok": bool(ok), "key": key, "engine": self.engine_name}

    async def scroll(self, direction: str = "down", amount: int = 600) -> Dict[str, Any]:
        engine = await self.ensure()
        try:
            ok = await engine.scroll(direction, amount)
        except Exception as exc:
            await self.degrade(str(exc))
            ok = await self.engine.scroll(direction, amount)  # type: ignore[union-attr]
        self._log("scroll", {"direction": direction, "amount": amount})
        return {"ok": bool(ok), "direction": direction, "engine": self.engine_name}

    async def back(self) -> Dict[str, Any]:
        engine = await self.ensure()
        ok = await engine.back()
        await asyncio.sleep(0.3)
        return {"ok": ok, "url": await engine.current_url()}

    async def forward(self) -> Dict[str, Any]:
        engine = await self.ensure()
        ok = await engine.forward()
        await asyncio.sleep(0.3)
        return {"ok": ok, "url": await engine.current_url()}

    async def evaluate(self, expression: str) -> Dict[str, Any]:
        engine = await self.ensure()
        try:
            value = await engine.evaluate(expression)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "engine": self.engine_name}
        return {"ok": True, "value": value, "engine": self.engine_name}

    async def screenshot(self, *, full_page: bool = False,
                         name: str = "") -> Dict[str, Any]:
        engine = await self.ensure()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = name or f"page-{stamp}.png"
        path = str(self.screenshot_dir / filename)
        try:
            saved = await engine.screenshot(path, full_page=full_page)
        except Exception as exc:
            try:
                from ..screen.screen_capture import capture_region

                saved = capture_region(None, path)
                return {"ok": True, "path": saved, "method": "desktop-fallback",
                        "engine": self.engine_name, "error": str(exc)}
            except Exception as inner:
                return {"ok": False, "error": f"{exc} / {inner}"}
        self._log("screenshot", {"path": saved, "full_page": full_page})
        return {"ok": True, "path": saved, "full_page": full_page,
                "engine": self.engine_name}

    async def tabs(self) -> Dict[str, Any]:
        engine = await self.ensure()
        return {"ok": True, "tabs": await engine.tabs(), "engine": self.engine_name}

    async def switch_tab(self, target: Any) -> Dict[str, Any]:
        engine = await self.ensure()
        result = await engine.switch_tab(target)
        self._log("switch_tab", {"target": str(target), "engine": self.engine_name})
        return {"ok": True, "engine": self.engine_name, **result}

    async def close_tab(self, target: Any) -> Dict[str, Any]:
        engine = await self.ensure()
        result = await engine.close_tab(target)
        self._log("close_tab", {"target": str(target), "engine": self.engine_name})
        return {"ok": True, "engine": self.engine_name, **result}

    async def current_url(self) -> str:
        engine = await self.ensure()
        try:
            return await engine.current_url()
        except Exception:
            return ""

    # ── hybrid coordinate bridge ───────────────────────────────────────────
    async def _box_for(self, selector: str) -> Optional[Dict[str, float]]:
        engine = await self.ensure()
        try:
            box = await engine.element_box(selector)
        except Exception as exc:
            log.debug(f"element_box failed: {exc}")
            return None
        if box:
            self._last_box = dict(box)
        return box

    def screen_point(self, box: Mapping[str, Any]) -> Tuple[int, int]:
        window = self.cfg.get("window") or {}
        return compute_screen_point(
            box,
            window_left=float(window.get("left", 0)),
            window_top=float(window.get("top", 0)))

    async def click_at(self, x: int, y: int, *, smooth: bool = True) -> Dict[str, Any]:
        """Click absolute screen coordinates with the real cursor."""
        try:
            from ..screen.mouse_keyboard import click_screen_point

            click_screen_point(x, y,
                               move_duration_ms=int(self.hybrid_cfg.get(
                                   "move_duration_ms", 260)) if smooth else 0,
                               jitter=bool(self.hybrid_cfg.get("jitter", True)))
            self._log("click_at", {"point": [x, y]})
            return {"ok": True, "point": [x, y], "method": "cursor"}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "point": [x, y]}

    async def click_element_with_cursor(self, target: Any) -> Dict[str, Any]:
        """Force the OS-cursor path for one click (used by tests/verification)."""
        return await self.click(target, hybrid=True)

    # ── reporting ──────────────────────────────────────────────────────────
    def _log(self, action: str, data: Mapping[str, Any]) -> None:
        self.history.append({"action": action, "at": time.time(), **dict(data)})
        if len(self.history) > 200:
            self.history = self.history[-200:]

    def status(self) -> Dict[str, Any]:
        capabilities = self.engine.capabilities() if self.engine else {}
        return {
            "engine": self.engine_name or "(not started)",
            "chain": self.engine_chain,
            "started": bool(self.engine and self.engine.started),
            "capabilities": capabilities,
            "hybrid_cursor": self.hybrid_enabled,
            "current_url": (self.snapshot.url if self.snapshot else ""),
            "elements_indexed": len(self.elements),
            "tabs": 0,
            "degradations": self.degradations[-3:],
            "screenshot_dir": str(self.screenshot_dir),
            "websockets": websocket_available(),
        }

    def report(self) -> str:
        lines = ["Browser engines:"]
        for report in self.engine_reports():
            lines.append("  " + report.line())
        active = self.engine_name or "none"
        lines.append(f"  active: {active}")
        if self.engine:
            caps = ", ".join(k for k, v in self.engine.capabilities().items() if v)
            lines.append(f"  capabilities: {caps}")
        lines.append(f"  hybrid cursor clicks: {'on' if self.hybrid_enabled else 'off'}")
        if self.snapshot:
            lines.append(f"  page: {truncate(self.snapshot.title or self.snapshot.url, 60)}"
                         f" ({len(self.elements)} elements indexed)")
        for step in self.degradations[-2:]:
            lines.append(f"  ↳ degraded from {step['from']}: "
                         f"{truncate(step['reason'], 70)}")
        return "\n".join(lines)

    # ── terminal launch + verified fallback ────────────────────────────────
    def launch(self, url: Optional[str] = None, *, headless: Optional[bool] = None,
               browser_type: str = "chromium") -> Dict[str, Any]:
        """Launch Chrome from the terminal and wait for its debug port.

        This is the "launch Chrome from the terminal" step the operator asked
        for: the browser really is a normal process, so JARVIS can also drive it
        by screen control if every programmatic engine fails.
        """
        from .terminal_launcher import launch_chrome

        target = url or str(self.cfg.get("home_url") or "about:blank")
        try:
            process = launch_chrome(
                target, browser_type=browser_type,
                headless=bool(self.cfg.get("headless", False) if headless is None else headless),
                port=int(self.cfg.get("remote_debugging_port") or 0) or None,
                binary_path=str(self.cfg.get("binary_path") or ""),
                user_data_dir=str(self.cfg.get("user_data_dir") or ""),
                window_size=tuple(self.cfg.get("window_size") or (1440, 900)),  # type: ignore[arg-type]
                stealth=bool(self.cfg.get("stealth", True)))
        except Exception as exc:
            log.warning(f"terminal launch failed: {exc}")
            return {"ok": False, "detail": str(exc), "url": target}
        self._launched = process  # type: ignore[attr-defined]
        self.screen_engine_process = process
        log.info(f"Chrome launched from terminal: pid={process.pid} port={process.port}")
        return {"ok": True, "pid": process.pid, "port": process.port,
                "cdp": process.cdp_base, "url": target,
                "detail": f"Chrome pid {process.pid} on CDP port {process.port}"}

    def verify(self, *, deep: bool = False) -> str:
        """Verify the engine chain: which engines *actually* work right now.

        ``deep=True`` also starts each usable engine and loads ``about:blank``, so
        the fallback is proven end-to-end rather than assumed from imports.
        """
        lines = ["Browser harness verification", "─" * 46]
        working: List[str] = []
        for report in self.engine_reports():
            lines.append(f"  {report.line()}")
            if not report.available:
                continue
            if not deep:
                working.append(report.name)
                continue
            try:
                engine = _ENGINES[report.name](self)
                engine.start()
                detail = "started"
                try:
                    result = run_sync(engine.goto("about:blank", new_tab=False))
                    detail = f"navigated → {result.get('url') or 'about:blank'}"
                except Exception as exc:
                    detail = f"started but navigation failed: {exc}"
                engine.stop()
                working.append(report.name)
                lines.append(f"    ✓ {report.name}: {detail}")
            except Exception as exc:
                lines.append(f"    ✗ {report.name}: start failed — {exc}")

        if working:
            lines.append("")
            lines.append(f"✅ working engines (in fallback order): {' → '.join(working)}")
            lines.append("   JARVIS will use the first one and degrade automatically.")
        else:
            lines.append("")
            lines.append("❌ no browser engine works here.")
            lines.append("   Fix with: pip install playwright && playwright install chromium")
            lines.append("   or install Google Chrome, then: python -m jarvis browser --verify --deep")
        return "\n".join(lines)

    async def close(self, *, kill_browser: bool = False) -> None:
        if self.engine is not None:
            if kill_browser and isinstance(self.engine, ScreenOnlyEngine) and \
                    self.engine.process is not None:
                self.engine.process.kill()
            else:
                self.engine.stop()
        self.engine = None
        self.engine_name = ""


_HARNESS: Optional[BrowserHarness] = None


def run_sync(coro: Any) -> Any:
    """Run a coroutine from synchronous code (safe with or without a live loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Called from inside a running loop (e.g. a tool invoked from async code):
    # use a worker thread so we never nest event loops.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def get_harness(config: Optional[Mapping[str, Any]] = None, *,
                refresh: bool = False) -> BrowserHarness:
    """Process-wide harness singleton (keeps one browser, one element map)."""
    global _HARNESS
    if _HARNESS is None or refresh:
        if _HARNESS is not None:
            try:
                run_sync(_HARNESS.close())
            except Exception:
                pass
        if config is None:
            from ...config import load_config

            config = load_config()
        _HARNESS = BrowserHarness(config)
    return _HARNESS
