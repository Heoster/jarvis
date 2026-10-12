"""Config, messages, context budget and the API manager's fallback behaviour."""

from __future__ import annotations

import os
import time

import pytest


# ── config ────────────────────────────────────────────────────────────────
def test_config_loads_defaults(home, monkeypatch):
    """Without JARVIS_CONFIG the repository config.yaml is the baseline."""
    from jarvis.config import load_config

    monkeypatch.delenv("JARVIS_CONFIG", raising=False)
    cfg = load_config(refresh=True)
    assert cfg["jarvis"]["operator"] == "Heoster"
    assert cfg["jarvis"]["cost_policy"] == "free"
    assert cfg["llm"]["fallback_chain"] == ["ollama", "groq", "gemini", "openrouter", "hf"]
    assert "stub" not in cfg["llm"]["fallback_chain"]
    assert "cerebras" not in cfg["llm"]["fallback_chain"]
    assert cfg["context"]["max_budget"] >= 4000
    assert cfg["screen"]["vision_model"] is False


def test_config_env_override(home, monkeypatch):
    monkeypatch.setenv("JARVIS__CONTEXT__MAX_BUDGET", "7777")
    from jarvis.config import load_config

    cfg = load_config(refresh=True)
    assert cfg["context"]["max_budget"] == 7777


def test_config_secret_from_env(home, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    from jarvis.config import get, load_config

    cfg = load_config(refresh=True)
    assert get(cfg, "search.tavily.api_key") == "tvly-test-key"
    assert get(cfg, "search.tavily.key_pool") == ["tvly-test-key"]


# ── messages ──────────────────────────────────────────────────────────────
def test_message_roundtrip_uses_openai_roles():
    """Regression: 'human'/'ai' must be translated to user/assistant on the wire."""
    from jarvis.core.messages import AIMessage, HumanMessage, SystemMessage, to_openai_messages

    messages = [SystemMessage(content="sys"), HumanMessage(content="hi"),
                AIMessage(content="hello")]
    payload = to_openai_messages(messages)
    assert payload[0] == {"role": "system", "content": "sys"}
    assert payload[1]["role"] == "user"
    assert payload[2]["role"] == "assistant"
    assert payload[2]["content"] == "hello"


def test_tool_call_message_shape():
    from jarvis.core.messages import AIMessage, to_openai_messages

    message = AIMessage(content="", tool_calls=[{"id": "call_1", "name": "calculate",
                                                 "args": {"expression": "1+1"}}])
    payload = to_openai_messages([message])[0]
    assert payload["tool_calls"][0]["function"]["name"] == "calculate"
    assert "1+1" in payload["tool_calls"][0]["function"]["arguments"]


# ── context manager ───────────────────────────────────────────────────────
def test_context_manager_respects_budget(config):
    from jarvis.core.context_manager import ContextManager
    from jarvis.core.messages import HumanMessage, SystemMessage

    config["context"] = dict(config["context"])
    config["context"]["max_budget"] = 1500
    manager = ContextManager(config)
    messages = [SystemMessage(content="s" * 4000)] + [
        HumanMessage(content="x" * 3000) for _ in range(6)]
    fitted = manager.fit(messages)
    assert manager.count_messages(fitted) <= 1500, "prompt must fit the window"
    assert fitted[0].role == "system"
    assert len(fitted) < len(messages), "old turns must be dropped"


def test_tool_result_shrinking(config):
    from jarvis.core.context_manager import ContextManager

    manager = ContextManager(config)
    text = "\n".join(f"line {i} " + "y" * 60 for i in range(400))
    shrunk = manager.shrink_tool_result(text)
    assert len(shrunk) < len(text)
    assert "trimmed" in shrunk.lower()
    assert len(shrunk) <= manager.max_tool_result_chars + 200


# ── API manager ───────────────────────────────────────────────────────────
def test_api_manager_uses_stub_when_nothing_else_works(config):
    from jarvis.core.api_manager import get_api_manager
    from jarvis.core.messages import HumanMessage

    manager = get_api_manager(config, refresh=True)
    response = manager.complete([HumanMessage(content="hello")])
    assert response.provider, "a provider must always answer"
    assert isinstance(response.content, str)


def test_api_manager_verify_all_reports_every_provider(config):
    from jarvis.core.api_manager import get_api_manager

    manager = get_api_manager(config, refresh=True)
    results = manager.verify_all(force=True)
    assert {"ollama", "groq", "stub"} <= set(results)
    for row in results.values():
        assert "status" in row


def test_api_manager_remembers_fallbacks(config):
    from jarvis.core.api_manager import get_api_manager
    from jarvis.core.messages import HumanMessage

    manager = get_api_manager(config, refresh=True)
    manager.complete([HumanMessage(content="ping")])
    # with provider=stub there is nothing to fall back from, but the state
    # machine must still expose a usable history API
    assert isinstance(manager.recent_fallbacks(), list)
    assert manager.active_provider()


def test_quota_blocks_and_refunds(config):
    from jarvis.utils.quota import get_quota_manager

    manager = get_quota_manager(config, refresh=True)
    spec = manager.spec("tavily", category="search")
    assert spec.limit > 0
    # exhaust the window
    for _ in range(spec.limit):
        assert manager.allow("tavily", 1) is True
    assert manager.allow("tavily", 1) is False, "quota must refuse over the limit"
    manager.refund("tavily", 1)
    assert manager.allow("tavily", 1) is True
    state = manager.check("tavily", 1)
    assert state.used >= spec.limit
    manager.reset("tavily")
    assert manager.check("tavily", 1).used == 0


def test_token_counter_estimates_reasonably():
    from jarvis.utils.token_counter import count_tokens

    text = "JARVIS controls the browser and remembers what matters. " * 20
    estimate = count_tokens(text)
    assert 100 < estimate < 800


def test_sanitizer_redacts_and_truncates():
    from jarvis.utils.sanitizer import redact_secrets, sanitize_text, truncate

    dirty = "token=sk-abcdefghijklmnopqrstuvwxyz1234567890 and key AIzaSyA1234567890abcdefghijklmnopqrs"
    cleaned = redact_secrets(dirty)
    assert "sk-abcdefghijklmnop" not in cleaned
    assert "[redacted]" in cleaned or "…" in cleaned

    long_text = "a" * 5000
    short = truncate(long_text, 500)
    assert len(short) < 700

    assert "\x1b[31m" not in sanitize_text("\x1b[31mred\x1b[0m")


def test_rate_limiter_enforces_the_window():
    from jarvis.utils.rate_limiter import RateLimiter

    limiter = RateLimiter(max_calls=2, period=60)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False, "third call must exceed the window"
    assert limiter.wait_time() > 0
    assert limiter.snapshot()["used"] == 2

    bucket = __import__("jarvis.utils.rate_limiter", fromlist=["TokenBucket"]).TokenBucket(
        capacity=2, refill_per_second=0)
    assert bucket.consume() and bucket.consume() and not bucket.consume()
