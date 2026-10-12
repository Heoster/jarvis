"""Ollama runtime: URL resolution, auto-start of `ollama serve`, and provider wiring.

The auto-start tests put a fake `ollama` executable on PATH.  It reads
OLLAMA_HOST the way the real server does and answers /api/tags, so the
start → wait → serve path runs end to end without a real Ollama install.
"""

from __future__ import annotations

import socket
import stat
import sys
import textwrap

import pytest


@pytest.fixture()
def runtime(monkeypatch, tmp_path):
    from jarvis.core import ollama_runtime

    monkeypatch.setenv("JARVIS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(ollama_runtime, "_LAST_FAILED_AT", 0.0)
    monkeypatch.setattr(ollama_runtime, "_LAST_FAILURE", "")
    monkeypatch.setattr(ollama_runtime, "_PROCESS", None)
    yield ollama_runtime
    proc = ollama_runtime._PROCESS
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fake_ollama(bin_dir, *, exit_immediately=False):
    """Write an `ollama` executable that serves /api/tags on $OLLAMA_HOST."""
    script = bin_dir / "ollama"
    if exit_immediately:
        body = "import sys\nsys.stderr.write('Error: address already in use\\n')\nsys.exit(3)\n"
    else:
        body = textwrap.dedent('''
            import http.server, json, os, sys
            host, _, port = os.environ["OLLAMA_HOST"].rpartition(":")
            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    body = json.dumps({"models": [{"name": "hermes3:3b"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
            if sys.argv[1:] != ["serve"]:
                sys.exit(2)
            http.server.ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
        ''')
    script.write_text(f"#!{sys.executable}\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ── URL resolution ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw, expected", [
    ("0.0.0.0:11434", "http://127.0.0.1:11434"),
    ("127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("localhost", "http://localhost:11434"),
    ("http://127.0.0.1:9999/", "http://127.0.0.1:9999"),
    ("https://ollama.example.com", "https://ollama.example.com"),
    ("https://gateway.example.com/ollama/", "https://gateway.example.com/ollama"),
])
def test_normalise_url(runtime, raw, expected):
    assert runtime.normalise_url(raw) == expected


def test_resolution_precedence(runtime):
    cfg = {"llm": {"base_url": "http://legacy:1", "ollama": {"base_url": "http://cfg:2"}}}
    assert runtime.resolve_base_url(cfg, env={}) == "http://cfg:2"
    assert runtime.resolve_base_url(cfg, env={"OLLAMA_HOST": "0.0.0.0:3"}) == "http://127.0.0.1:3"
    assert runtime.resolve_base_url(
        cfg, env={"OLLAMA_HOST": "h:3", "OLLAMA_BASE_URL": "http://env:4"}) == "http://env:4"
    assert runtime.resolve_base_url({"llm": {"base_url": "http://legacy:1"}}, env={}) == \
        "http://legacy:1"
    assert runtime.resolve_base_url({}, env={}) == runtime.DEFAULT_URL


def test_autostart_switches(runtime):
    assert runtime.autostart_enabled({}, env={}) is True
    assert runtime.autostart_enabled({"llm": {"ollama": {"autostart": False}}}, env={}) is False
    # the env var overrides config in both directions
    assert runtime.autostart_enabled({"llm": {"ollama": {"autostart": False}}},
                                     env={"JARVIS_OLLAMA_AUTOSTART": "1"}) is True
    assert runtime.autostart_enabled({}, env={"JARVIS_OLLAMA_AUTOSTART": "0"}) is False


# ── starting ollama serve ─────────────────────────────────────────────────
def test_already_running_is_used_without_starting(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "is_served", lambda url, timeout=1.5: True)
    monkeypatch.setattr(runtime, "_spawn", lambda *a: pytest.fail("must not spawn"))
    assert runtime.ensure_running("http://127.0.0.1:11434") == (True, "running")


def test_starts_ollama_serve_when_down(runtime, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ollama(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{':' if sys.platform != 'win32' else ';'}"
                       + __import__("os").environ.get("PATH", ""))
    url = f"http://127.0.0.1:{_free_port()}"
    ok, detail = runtime.ensure_running(url, autostart=True, timeout=15)
    assert ok, detail
    assert "started" in detail
    assert runtime.is_served(url)
    # a second caller sees the server and does not spawn another one
    pid = runtime._PROCESS.pid
    assert runtime.ensure_running(url, autostart=True, timeout=5) == (True, "running")
    assert runtime._PROCESS.pid == pid


def test_missing_binary_is_reported_not_retried(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    url = f"http://127.0.0.1:{_free_port()}"
    ok, detail = runtime.ensure_running(url, autostart=True, timeout=2)
    assert not ok
    assert "not installed" in detail and "ollama.com" in detail


def test_server_that_exits_is_reported_with_its_log(runtime, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ollama(bin_dir, exit_immediately=True)
    monkeypatch.setenv("PATH", f"{bin_dir}{':' if sys.platform != 'win32' else ';'}"
                       + __import__("os").environ.get("PATH", ""))
    url = f"http://127.0.0.1:{_free_port()}"
    ok, detail = runtime.ensure_running(url, autostart=True, timeout=10)
    assert not ok
    assert "exited with code 3" in detail and "address already in use" in detail
    # inside the cooldown it does not spawn again
    ok2, detail2 = runtime.ensure_running(url, autostart=True, timeout=10)
    assert not ok2 and "failed recently" in detail2


def test_remote_or_disabled_is_never_started(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_spawn", lambda *a: pytest.fail("must not spawn"))
    ok, detail = runtime.ensure_running("http://gpu-box.lan:11434", autostart=True, timeout=1)
    assert not ok and "remote host" in detail
    ok, detail = runtime.ensure_running("http://127.0.0.1:1", autostart=False, timeout=1)
    assert not ok and "autostart is off" in detail


# ── provider wiring ───────────────────────────────────────────────────────
def test_ollama_provider_uses_resolved_url(config, monkeypatch):
    from jarvis.core.providers import build_provider

    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:5555")
    provider = build_provider("ollama", config)
    assert provider.base_url == "http://127.0.0.1:5555"


def test_provider_probe_starts_ollama_then_answers(config, runtime, tmp_path, monkeypatch):
    from jarvis.core.providers import build_provider

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ollama(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{':' if sys.platform != 'win32' else ';'}"
                       + __import__("os").environ.get("PATH", ""))
    port = _free_port()
    monkeypatch.setenv("OLLAMA_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("JARVIS_OLLAMA_AUTOSTART", "1")
    provider = build_provider("ollama", config)
    ok, latency, detail = provider.probe()
    assert ok, detail
    assert "hermes3" in detail or "model" in detail


def test_provider_reports_missing_ollama_as_error(config, runtime, tmp_path, monkeypatch):
    from jarvis.core.providers import build_provider

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("OLLAMA_BASE_URL", f"http://127.0.0.1:{_free_port()}")
    monkeypatch.setenv("JARVIS_OLLAMA_AUTOSTART", "1")
    ok, _latency, detail = build_provider("ollama", config).probe()
    assert not ok and "not installed" in detail


def test_embedder_uses_the_same_url(config, monkeypatch):
    from jarvis.memory.embedder import Embedder

    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:7777")
    assert Embedder({"llm": config["llm"]}).base_url == "http://127.0.0.1:7777"
