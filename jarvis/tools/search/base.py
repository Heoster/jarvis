"""Shared plumbing for search providers: results, HTTP, quota, chaining.

Every provider module uses :func:`fetch_json` / :func:`fetch_text`, which
enforce the free-tier quota *before* the request goes out and surface a
``SearchQuotaExceeded`` so the chain can move to the next provider instead of
failing the whole task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...utils.logger import get_logger
from ...utils.quota import get_quota_manager
from ...utils.sanitizer import truncate

log = get_logger("search")

__all__ = ["SearchResult", "SearchQuotaExceeded", "SearchError", "fetch_json",
           "fetch_text", "format_results", "provider_available", "USER_AGENT"]

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 JARVIS/1.0")


class SearchError(RuntimeError):
    """Provider-side failure (network, parse, HTTP error)."""


class SearchQuotaExceeded(SearchError):
    """Free-tier allowance for this provider is used up."""


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    score: float = 0.0
    published: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def line(self, index: int = 0, *, snippet_chars: int = 260) -> str:
        head = f"{index}. **{self.title or self.url}**" if index else f"**{self.title}**"
        parts = [head, f"   {self.url}"]
        if self.snippet:
            parts.append(f"   {truncate(self.snippet, snippet_chars)}")
        if self.published:
            parts.append(f"   _published: {self.published}_")
        return "\n".join(parts)


def _quota_check(provider: str, cost: int = 1) -> None:
    manager = get_quota_manager()
    if not manager.allow(provider, cost=cost):
        state = manager.state(provider)
        raise SearchQuotaExceeded(
            f"{provider} free-tier exhausted ({state.used}/{state.limit} "
            f"{state.unit}s per {state.period})")


def fetch_json(url: str, *, provider: str, method: str = "GET",
               params: Optional[Mapping[str, Any]] = None,
               json_body: Optional[Mapping[str, Any]] = None,
               headers: Optional[Mapping[str, str]] = None, timeout: float = 20.0,
               cost: int = 1) -> Any:
    """Quota-guarded JSON request."""
    _quota_check(provider, cost)
    import httpx

    request_headers = {"User-Agent": USER_AGENT,
                       "Accept": "application/json, text/plain, */*"}
    request_headers.update(dict(headers or {}))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.request(method, url, params=params, json=json_body,
                                      headers=request_headers)
    except Exception as exc:
        get_quota_manager().refund(provider, cost, reason="network-error")
        raise SearchError(f"{provider} request failed: {exc}") from exc

    if response.status_code == 429:
        raise SearchQuotaExceeded(f"{provider} rate limited (429)")
    if response.status_code == 401:
        raise SearchError(f"{provider} rejected the API key (401)")
    if response.status_code >= 400:
        raise SearchError(f"{provider} HTTP {response.status_code}: "
                          f"{truncate(response.text, 200)}")
    try:
        return response.json()
    except ValueError as exc:
        raise SearchError(f"{provider} returned non-JSON") from exc


def fetch_text(url: str, *, provider: str, params: Optional[Mapping[str, Any]] = None,
               headers: Optional[Mapping[str, str]] = None, timeout: float = 25.0,
               cost: int = 1, method: str = "GET",
               data: Optional[Mapping[str, Any]] = None) -> str:
    """Quota-guarded text/HTML request."""
    _quota_check(provider, cost)
    import httpx

    request_headers = {"User-Agent": USER_AGENT,
                       "Accept": "text/html,application/xhtml+xml,text/plain,*/*"}
    request_headers.update(dict(headers or {}))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.request(method, url, params=params, data=data,
                                      headers=request_headers)
    except Exception as exc:
        get_quota_manager().refund(provider, cost, reason="network-error")
        raise SearchError(f"{provider} request failed: {exc}") from exc
    if response.status_code >= 400:
        raise SearchError(f"{provider} HTTP {response.status_code}")
    return response.text


def provider_available(provider: str) -> Tuple[bool, str]:
    """Is this provider usable *and* within quota right now?"""
    manager = get_quota_manager()
    state = manager.state(provider)
    if state.exhausted:
        return False, (f"quota used up ({state.used}/{state.limit} {state.unit}s "
                       f"per {state.period})")
    if provider == "tavily" and not _key("TAVILY_API_KEY", "search.tavily.api_key"):
        return False, "no API key (free at tavily.com)"
    if provider == "serper" and not _key("SERPER_API_KEY", "search.serper.api_key"):
        return False, "no API key (free at serper.dev)"
    if provider == "searxng":
        from ...config import load_config

        base = ((load_config().get("search") or {}).get("searxng") or {}).get(
            "base_url", "")
        return (bool(base), "self-hosted instance not configured" if not base else "ready")
    return True, "ready"


def _key(env: str, dotted: str) -> str:
    import os

    from ...config import get, load_config

    value = os.environ.get(env) or ""
    if value:
        return value
    return str(get(load_config(), dotted, "") or "")


def format_results(results: Sequence[SearchResult], *, header: str = "",
                   max_chars: int = 4000) -> str:
    if not results:
        return header + "No results found." if header else "No results found."
    lines = [header] if header else []
    used = len(header) if header else 0
    for index, result in enumerate(results, 1):
        block = result.line(index)
        if used + len(block) > max_chars:
            lines.append(f"…{len(results) - index + 1} more results omitted")
            break
        lines.append(block)
        used += len(block) + 1
    return "\n".join(lines)


def dedupe_results(results: Iterable[SearchResult]) -> List[SearchResult]:
    seen: Dict[str, SearchResult] = {}
    for result in results:
        key = result.url.split("#")[0].rstrip("/")
        if key and key not in seen:
            seen[key] = result
        elif key in seen and result.snippet and not seen[key].snippet:
            seen[key] = result
    return list(seen.values())
