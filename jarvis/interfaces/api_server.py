"""Local API server: OpenAI-compatible endpoint + status/health.

Runs on the standard library (``http.server``) so it works on a bare Python
install; if ``fastapi``/``uvicorn`` are available they are used instead, because
that gives Heoster automatic docs and proper async handling.

Endpoints
---------
``GET  /health``                 liveness
``GET  /status``                 brain, memory, tools, quota snapshot
``GET  /tools``                  tool list (name, category, description)
``POST /ask``                    {"message": "...", "session": "optional"}
``POST /v1/chat/completions``    OpenAI-compatible chat endpoint
``POST /read``                   {"url": "..."} — crawl chain shortcut
``POST /search``                 {"query": "..."} — search chain shortcut

Security: binds to 127.0.0.1 by default.  Set ``JARVIS_API_TOKEN`` to require
``Authorization: Bearer <token>``; bind 0.0.0.0 only on a trusted network.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Tuple

from ..config import get_env
from ..utils.logger import get_logger
from ..utils.sanitizer import truncate
from ..version import APP_NAME, __version__

log = get_logger("api")

__all__ = ["run_server", "JarvisAPI", "ApiConfig"]


class ApiConfig:
    def __init__(self, config: Mapping[str, Any]):
        section = dict((config.get("interface") or {}).get("api") or {})
        self.host = os.environ.get("JARVIS_API_HOST") or section.get("host") or "127.0.0.1"
        self.port = int(os.environ.get("JARVIS_API_PORT") or section.get("port") or 8000)
        self.token = get_env("JARVIS_API_TOKEN") or section.get("token") or ""
        self.model_name = section.get("model_name") or f"jarvis-{__version__}"

    @property
    def protected(self) -> bool:
        return bool(self.token)


class JarvisAPI:
    """Shared request handling for both server flavours."""

    def __init__(self, config: Mapping[str, Any]):
        from ..core.orchestrator import Orchestrator

        self.config = dict(config)
        self.api_config = ApiConfig(self.config)
        self.orchestrator = Orchestrator(self.config)
        self.orchestrator.initialize()
        self.started_at = time.time()
        log.info("API orchestrator ready")

    # ── handlers ───────────────────────────────────────────────────────────
    def health(self) -> Dict[str, Any]:
        return {"status": "ok", "app": APP_NAME, "version": __version__,
                "uptime_s": round(time.time() - self.started_at, 1)}

    def status(self) -> Dict[str, Any]:
        return self.orchestrator.status()

    def tools(self) -> Dict[str, Any]:
        return {"count": len(self.orchestrator.registry.get_all_tools()),
                "categories": self.orchestrator.registry.categories(),
                "tools": [{"name": t.name,
                           "category": getattr(t, "category", "utility"),
                           "description": truncate((t.description or "").split("\n")[0], 160)}
                          for t in self.orchestrator.registry.get_all_tools()]}

    def ask(self, message: str, *, history: Optional[list] = None) -> Dict[str, Any]:
        import asyncio

        result = asyncio.run(self.orchestrator.ask(message, history=history))
        return {"answer": result.answer, "provider": result.provider,
                "model": result.model, "iterations": result.iterations,
                "tool_calls": result.tool_calls, "latency_ms": result.latency_ms,
                "memory": result.memory}

    def tool_call(self, name: str, args: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        result = self.orchestrator.registry.execute_sync(name, args or {})
        return {"ok": result.ok, "content": result.content, "error": result.error,
                "elapsed_ms": result.elapsed_ms}

    def read(self, url: str, *, max_chars: int = 6000) -> Dict[str, Any]:
        if not url:
            return {"ok": False, "error": "missing 'url'"}
        result = self.orchestrator.registry.execute_sync(
            "read_url", {"url": url, "max_chars": max_chars})
        return {"ok": result.ok, "content": result.content, "error": result.error}

    def search(self, query: str, *, max_results: int = 6) -> Dict[str, Any]:
        if not query:
            return {"ok": False, "error": "missing 'query'"}
        result = self.orchestrator.registry.execute_sync(
            "search_web", {"query": query, "max_results": max_results})
        return {"ok": result.ok, "content": result.content, "error": result.error}

    def openai_chat(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        messages = list(body.get("messages") or [])
        user_text = ""
        for message in reversed(messages):
            if (message or {}).get("role") == "user":
                user_text = str((message or {}).get("content") or "")
                break
        result = self.ask(user_text)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.api_config.model_name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result["answer"]},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0},
            "jarvis": {"provider": result["provider"], "model": result["model"],
                       "tool_calls": len(result["tool_calls"]),
                       "latency_ms": result["latency_ms"]},
        }


# ── stdlib server ─────────────────────────────────────────────────────────
def _make_handler(api: JarvisAPI):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"{APP_NAME}/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            log.debug(fmt % args)

        # helpers ---------------------------------------------------------
        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if not api.api_config.protected:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {api.api_config.token}"

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorised():
                self._json({"error": "unauthorised"}, 401)
                return
            path = self.path.split("?")[0].rstrip("/") or "/health"
            if path in ("/health", "/"):
                self._json(api.health())
            elif path == "/status":
                self._json(api.status())
            elif path == "/tools":
                self._json(api.tools())
            else:
                self._json({"error": f"unknown path {path}"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                self._json({"error": "unauthorised"}, 401)
                return
            path = self.path.split("?")[0].rstrip("/")
            body = self._body()
            try:
                if path in ("/ask", "/chat"):
                    message = str(body.get("message") or body.get("prompt") or "")
                    if not message:
                        self._json({"error": "missing 'message'"}, 400)
                        return
                    self._json(api.ask(message))
                elif path == "/v1/chat/completions":
                    self._json(api.openai_chat(body))
                elif path == "/tool":
                    self._json(api.tool_call(
                        str(body.get("name") or body.get("tool") or ""),
                        body.get("args") or body.get("arguments") or {}))
                elif path == "/read":
                    self._json(api.read(str(body.get("url") or ""),
                                        max_chars=int(body.get("max_chars") or 6000)))
                elif path == "/search":
                    self._json(api.search(str(body.get("query") or ""),
                                          max_results=int(body.get("max_results") or 6)))
                else:
                    self._json({"error": f"unknown path {path}"}, 404)
            except Exception as exc:  # never leak a stack trace to a client
                log.error(f"request failed: {exc}")
                self._json({"error": str(exc)}, 500)

    return Handler


def run_server(config: Mapping[str, Any], *, host: Optional[str] = None,
               port: Optional[int] = None) -> None:
    """Start the JARVIS API server (blocking)."""
    api = JarvisAPI(config)
    bind_host = host or api.api_config.host
    bind_port = port or api.api_config.port

    try:  # prefer FastAPI when the operator has it installed
        import uvicorn  # type: ignore
        from fastapi import FastAPI, Request  # type: ignore

        app = FastAPI(title=f"{APP_NAME} API", version=__version__)

        @app.get("/health")
        def _health() -> Dict[str, Any]:
            return api.health()

        @app.get("/status")
        def _status() -> Dict[str, Any]:
            return api.status()

        @app.get("/tools")
        def _tools() -> Dict[str, Any]:
            return api.tools()

        @app.post("/ask")
        async def _ask(request: Request) -> Dict[str, Any]:
            body = await request.json()
            return api.ask(str(body.get("message") or ""))

        @app.post("/v1/chat/completions")
        async def _chat(request: Request) -> Dict[str, Any]:
            return api.openai_chat(await request.json())

        @app.post("/tool")
        async def _tool(request: Request) -> Dict[str, Any]:
            body = await request.json()
            return api.tool_call(str(body.get("name") or body.get("tool") or ""),
                                 body.get("args") or body.get("arguments") or {})

        @app.post("/read")
        async def _read(request: Request) -> Dict[str, Any]:
            body = await request.json()
            return api.read(str(body.get("url") or ""),
                            max_chars=int(body.get("max_chars") or 6000))

        @app.post("/search")
        async def _search(request: Request) -> Dict[str, Any]:
            body = await request.json()
            return api.search(str(body.get("query") or ""),
                              max_results=int(body.get("max_results") or 6))

        log.info(f"JARVIS API (FastAPI) on http://{bind_host}:{bind_port}")
        uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")
        return
    except ImportError:
        pass

    server = ThreadingHTTPServer((bind_host, bind_port), _make_handler(api))
    log.info(f"JARVIS API (stdlib) on http://{bind_host}:{bind_port}")
    print(f"✅ {APP_NAME} API listening on http://{bind_host}:{bind_port}")
    print("   GET  /health  /status  /tools")
    print("   POST /ask {\"message\": \"...\"}    POST /v1/chat/completions")
    if api.api_config.protected:
        print("   🔒 bearer token required (JARVIS_API_TOKEN)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping API server…")
    finally:
        server.server_close()
