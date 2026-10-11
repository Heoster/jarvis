"""HTTP calls: webhooks, simple API requests, downloads.

One tool covers the common cases (GET/POST JSON, headers, auth headers) because
a small model handles one well-documented tool far better than five similar ones.
Secrets are redacted from the logged result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ...config import get_env
from ...utils.logger import get_logger
from ...utils.sanitizer import redact_secrets, truncate
from ..base import tool

log = get_logger("comms.webhook")

__all__ = ["http_request", "send_webhook", "download_file", "WEBHOOK_TOOLS"]

_BLOCKED_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "metadata")


@tool(category="communication", timeout=90)
def http_request(url: str, method: str = "GET", body: str = "",
                 headers: str = "", timeout: float = 30) -> str:
    """Make an HTTP request to any API and return the response.

    Use for public APIs, webhooks, and anything the search/crawl tools do not
    cover. Sending a body is optional; headers can be a JSON object string.

    Args:
        url: Full URL
        method: GET, POST, PUT, PATCH or DELETE
        body: Request body (JSON string, form string, or plain text)
        headers: Optional JSON object of headers, e.g. '{"Authorization": "Bearer x"}'
        timeout: Seconds to wait
    """
    import httpx

    method = (method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        return f"Unsupported method '{method}'."
    if not url.startswith(("http://", "https://")):
        return "URL must start with http:// or https://"

    parsed_headers: Dict[str, str] = {"User-Agent": "JARVIS/1.0 (Heoster)"}
    if headers.strip():
        try:
            parsed_headers.update(json.loads(headers))
        except Exception as exc:
            return f"`headers` must be a JSON object: {exc}"

    payload: Any = None
    json_body: Any = None
    if body.strip():
        try:
            json_body = json.loads(body)
            parsed_headers.setdefault("Content-Type", "application/json")
        except Exception:
            payload = body
    try:
        with httpx.Client(timeout=float(timeout), follow_redirects=True) as client:
            response = client.request(method, url, headers=parsed_headers,
                                      json=json_body, content=None if json_body is not None else payload)
    except Exception as exc:
        return f"Request failed: {exc}"

    text = response.text or ""
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            text = json.dumps(response.json(), indent=2, ensure_ascii=False)
        except Exception:
            pass
    return "\n".join([
        f"**{method} {url}** → `{response.status_code} {response.reason_phrase}`",
        f"content-type: {content_type or 'unknown'} · {len(response.content)} bytes",
        "",
        "```",
        truncate(redact_secrets(text), 4000),
        "```",
    ])


@tool(category="communication", timeout=60)
def send_webhook(url: str, message: str, format: str = "auto") -> str:
    """Send a notification to a webhook (Discord, Slack, ntfy, custom).

    Args:
        url: Webhook URL
        message: The message text
        format: "auto", "discord", "slack", "ntfy" or "json"
    """
    import httpx

    kind = (format or "auto").lower()
    if kind == "auto":
        if "discord" in url:
            kind = "discord"
        elif "slack" in url:
            kind = "slack"
        elif "ntfy" in url:
            kind = "ntfy"
        else:
            kind = "json"

    if kind == "discord":
        payload, headers = {"content": message}, {"Content-Type": "application/json"}
    elif kind == "slack":
        payload, headers = {"text": message}, {"Content-Type": "application/json"}
    elif kind == "ntfy":
        payload, headers = message.encode("utf-8"), {"Content-Type": "text/plain"}
    else:
        payload = {"message": message}
        headers = {"Content-Type": "application/json"}

    try:
        with httpx.Client(timeout=20.0) as client:
            if isinstance(payload, bytes):
                response = client.post(url, content=payload, headers=headers)
            else:
                response = client.post(url, json=payload, headers=headers)
    except Exception as exc:
        return f"Webhook failed: {exc}"
    if response.status_code >= 400:
        return f"Webhook returned HTTP {response.status_code}: {truncate(response.text, 200)}"
    return f"✅ Webhook delivered ({kind}, HTTP {response.status_code})"


@tool(category="communication", timeout=180)
def download_file(url: str, destination: str = "", max_mb: float = 50) -> str:
    """Download a file from a URL to disk.

    Args:
        url: File URL
        destination: Where to save it (default: ~/Downloads/<filename>)
        max_mb: Refuse downloads bigger than this
    """
    import httpx

    if not url.startswith(("http://", "https://")):
        return "URL must start with http:// or https://"
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "download.bin"
    target = Path(destination).expanduser() if destination else \
        Path.home() / "Downloads" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.stream("GET", url, timeout=60.0, follow_redirects=True) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length", 0) or 0)
            if declared and declared > max_mb * 1_000_000:
                return (f"Refusing: file is {declared / 1e6:.1f} MB "
                        f"(limit {max_mb} MB). Raise max_mb to allow it.")
            written = 0
            with open(target, "wb") as handle:
                for chunk in response.iter_bytes(65_536):
                    written += len(chunk)
                    if written > max_mb * 1_000_000:
                        handle.close()
                        target.unlink(missing_ok=True)
                        return f"Aborted: download exceeded {max_mb} MB."
                    handle.write(chunk)
    except Exception as exc:
        return f"Download failed: {exc}"
    return f"✅ Downloaded {written / 1e6:.2f} MB to {target}"


WEBHOOK_TOOLS = [http_request, send_webhook, download_file]
