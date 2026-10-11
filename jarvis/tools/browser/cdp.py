"""Raw Chrome DevTools Protocol client (no Selenium, no Playwright required).

Two layers:

* **HTTP endpoints** (``/json/version``, ``/json/list``, ``/json/new``,
  ``/json/activate``, ``/json/close``) — always available, and enough to launch,
  navigate, enumerate and close tabs.
* **WebSocket commands** (``Page.navigate``, ``Runtime.evaluate``,
  ``Page.captureScreenshot``, ``Input.dispatchMouseEvent``…) — used when the
  optional ``websockets`` package is installed, which unlocks full DOM + input
  control with zero browser-driver dependency.

This matters on a CPU-only machine: raw CDP is ~2 MB of dependencies versus
~300 MB for a driver stack, and it still speaks to the *same* browser the
operator sees on screen.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from ...utils.sanitizer import truncate

log = get_logger("browser.cdp")

__all__ = ["CDPClient", "CDPTarget", "cdp_available", "websocket_available"]


def websocket_available() -> bool:
    try:
        import websockets  # noqa: F401

        return True
    except Exception:
        try:
            import websocket  # type: ignore  # noqa: F401

            return True
        except Exception:
            return False


def cdp_available(port: int = 9222, *, timeout: float = 2.0) -> bool:
    import httpx

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"http://127.0.0.1:{port}/json/version")
        return resp.status_code == 200
    except Exception:
        return False


@dataclass
class CDPTarget:
    id: str
    title: str
    url: str
    type: str = "page"
    ws_url: str = ""

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CDPTarget":
        return cls(id=str(data.get("id", "")), title=str(data.get("title", "")),
                   url=str(data.get("url", "")), type=str(data.get("type", "page")),
                   ws_url=str(data.get("webSocketDebuggerUrl", "")))


class CDPClient:
    """Minimal but genuinely useful DevTools client."""

    def __init__(self, port: int = 9222, *, timeout: float = 20.0):
        self.port = int(port)
        self.timeout = timeout
        self.base = f"http://127.0.0.1:{self.port}"
        self._ws: Any = None
        self._message_id = 0
        self._target_id: str = ""
        self._pinned_id: str = ""      # tab chosen with switch_to()

    # ── HTTP layer ─────────────────────────────────────────────────────────
    def version(self) -> Dict[str, Any]:
        import httpx

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base}/json/version")
        resp.raise_for_status()
        return resp.json()

    def targets(self, *, kind: str = "page") -> List[CDPTarget]:
        import httpx

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{self.base}/json/list")
            resp.raise_for_status()
            return [CDPTarget.from_json(t) for t in resp.json()
                    if not kind or t.get("type") == kind]
        except Exception as exc:
            log.debug(f"targets() failed: {exc}")
            return []

    def current(self) -> Optional[CDPTarget]:
        pages = self.targets()
        if not pages:
            return None
        # an explicitly selected target wins over the heuristic
        pinned = getattr(self, "_pinned_id", "")
        if pinned:
            for target in pages:
                if target.id == pinned or target.id.startswith(pinned):
                    return target
        # otherwise prefer a real page (not about:blank)
        for target in pages:
            if target.url and target.url not in ("about:blank", "chrome://newtab/"):
                return target
        return pages[0]

    def switch_to(self, target: "CDPTarget") -> bool:
        """Make ``target`` the tab later commands act on."""
        self._pinned_id = target.id
        if self._ws is not None:            # reconnect the websocket to it
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._target_id = target.id
            return self.connect(target)
        return True

    def close_target(self, target_id: str) -> bool:
        return self.close_tab(target_id)

    def new_tab(self, url: str = "about:blank") -> Optional[CDPTarget]:
        import httpx

        url = url if "://" in url or url.startswith("about:") else f"https://{url}"
        with httpx.Client(timeout=self.timeout) as client:
            for method in ("put", "get"):
                try:
                    resp = getattr(client, method)(f"{self.base}/json/new?{url}")
                    if resp.status_code < 400:
                        return CDPTarget.from_json(resp.json())
                except Exception:
                    continue
        log.warning("could not open a new tab over CDP HTTP")
        return None

    def activate(self, target_id: str) -> bool:
        import httpx

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{self.base}/json/activate/{target_id}")
            return resp.status_code < 400
        except Exception:
            return False

    def close_tab(self, target_id: str) -> bool:
        import httpx

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{self.base}/json/close/{target_id}")
            return resp.status_code < 400
        except Exception:
            return False

    # ── WebSocket layer ────────────────────────────────────────────────────
    def connect(self, target: Optional[CDPTarget] = None) -> bool:
        """Attach to a target's WebSocket (needs the ``websockets`` package)."""
        if self._ws is not None:
            return True
        target = target or self.current()
        if target is None or not target.ws_url:
            return False
        try:
            from websockets.sync.client import connect  # type: ignore

            self._ws = connect(target.ws_url, open_timeout=self.timeout,
                               max_size=32 * 1024 * 1024)
            self._target_id = target.id
            return True
        except Exception as exc:
            log.debug(f"websocket connect failed: {exc}")
            self._ws = None
            return False

    def send(self, method: str, params: Optional[Mapping[str, Any]] = None,
             *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Send a CDP command and wait for its result."""
        if self._ws is None and not self.connect():
            raise RuntimeError("no CDP websocket; install `websockets` "
                              "(pip install websockets) for full control")
        self._message_id += 1
        message_id = self._message_id
        payload = {"id": message_id, "method": method,
                   "params": dict(params or {})}
        try:
            self._ws.send(json.dumps(payload))
        except Exception as exc:
            self._ws = None
            raise RuntimeError(f"CDP send failed: {exc}") from exc

        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            raw = self._ws.recv(timeout=max(0.5, deadline - time.time()))
            if raw is None:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if data.get("id") != message_id:
                continue
            if "error" in data:
                raise RuntimeError(f"CDP {method} error: {data['error']}")
            return data.get("result") or {}
        raise RuntimeError(f"CDP {method} timed out")

    # ── higher-level helpers ───────────────────────────────────────────────
    def evaluate(self, expression: str, *, return_by_value: bool = True,
                 await_promise: bool = True) -> Any:
        result = self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": return_by_value,
            "awaitPromise": await_promise, "userGesture": True,
        })
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error: {result['exceptionDetails'].get('text')}")
        return (result.get("result") or {}).get("value")

    def navigate(self, url: str, *, wait: bool = True, timeout: float = 25.0) -> str:
        url = url if "://" in url or url.startswith("about:") else f"https://{url}"
        self.send("Page.enable")
        self.send("Page.navigate", {"url": url})
        if wait:
            self._wait_ready(timeout)
        return self.evaluate("location.href") or url

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.evaluate("document.readyState")
                if state in ("interactive", "complete"):
                    return
            except Exception:
                pass
            time.sleep(0.25)

    def title(self) -> str:
        try:
            return str(self.evaluate("document.title") or "")
        except Exception:
            target = self.current()
            return target.title if target else ""

    def html(self) -> str:
        return str(self.evaluate(
            "document.documentElement.outerHTML") or "")

    def text(self) -> str:
        return str(self.evaluate(
            "document.body ? document.body.innerText : ''") or "")

    def click_selector(self, selector: str, *, timeout: float = 5.0) -> bool:
        """Click by CSS selector using a real, blessed DOM click."""
        js = f"""
        (() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return false;
          el.scrollIntoView({{block: 'center', inline: 'center'}});
          const rect = el.getBoundingClientRect();
          const opts = {{bubbles: true, cancelable: true, view: window,
                        clientX: rect.left + rect.width / 2,
                        clientY: rect.top + rect.height / 2}};
          el.focus && el.focus();
          el.dispatchEvent(new MouseEvent('mousedown', opts));
          el.dispatchEvent(new MouseEvent('mouseup', opts));
          el.click && el.click();
          return true;
        }})()
        """
        return bool(self.evaluate(js, await_promise=False))

    def fill_selector(self, selector: str, text: str) -> bool:
        js = f"""
        (() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return false;
          el.focus();
          const setter = Object.getOwnPropertyDescriptor(
              el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                        : HTMLInputElement.prototype, 'value');
          if (setter && setter.set) setter.set.call(el, {json.dumps(text)});
          else el.value = {json.dumps(text)};
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return true;
        }})()
        """
        return bool(self.evaluate(js, await_promise=False))

    def submit_enter(self, selector: str = "") -> bool:
        target = f"document.querySelector({json.dumps(selector)})" if selector else "document.activeElement"
        js = f"""
        (() => {{
          const el = {target} || document.activeElement;
          if (!el) return false;
          for (const type of ['keydown', 'keypress', 'keyup']) {{
            el.dispatchEvent(new KeyboardEvent(type, {{key: 'Enter', code: 'Enter',
                              keyCode: 13, which: 13, bubbles: true}}));
          }}
          if (el.form) el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit();
          return true;
        }})()
        """
        return bool(self.evaluate(js, await_promise=False))

    def element_box(self, selector: str) -> Optional[Dict[str, float]]:
        js = f"""
        (() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return null;
          el.scrollIntoView({{block: 'center', inline: 'center'}});
          const r = el.getBoundingClientRect();
          const info = window.__jarvisViewport || {{}};
          return {{x: r.left, y: r.top, width: r.width, height: r.height,
                   dpr: window.devicePixelRatio || 1,
                   screenX: (window.screenX || 0),
                   screenY: (window.screenY || 0),
                   outerH: (window.outerHeight || 0),
                   innerH: (window.innerHeight || 0),
                   innerW: (window.innerWidth || 0),
                   outerW: (window.outerWidth || 0),
                   scrollX: window.scrollX, scrollY: window.scrollY}};
        }})()
        """
        try:
            return self.evaluate(js, await_promise=False)
        except Exception:
            return None

    def screenshot(self, path: str, *, full_page: bool = False) -> str:
        params: Dict[str, Any] = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        result = self.send("Page.captureScreenshot", params)
        data = base64.b64decode(result.get("data", ""))
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def scroll(self, *, direction: str = "down", amount: int = 600) -> bool:
        dy = amount if direction == "down" else -amount
        return bool(self.evaluate(f"window.scrollBy(0, {dy}); true",
                                  await_promise=False))

    def close(self) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        self._ws = None

    def info(self) -> Dict[str, Any]:
        try:
            version = self.version()
        except Exception:
            version = {}
        pages = self.targets()
        return {"port": self.port, "browser": version.get("Browser", ""),
                "protocol": version.get("Protocol-Version", ""),
                "tabs": [{"id": t.id, "title": truncate(t.title, 60), "url": truncate(t.url, 80)}
                         for t in pages],
                "websocket": self._ws is not None, "ws_support": websocket_available()}
