"""The LLM fallback chain and provider adapters.

Production order is ollama → groq → gemini → openrouter → hf, then an explicit
error (never a canned answer).  These tests pin that order, the per-provider
model retries (retired / unserved models), the HF router behaviour, and the
no-key-in-URL rule for Gemini.  Everything is offline: network calls are
replaced with fakes.
"""

from __future__ import annotations

import pytest

from jarvis.core.messages import HumanMessage


# ── fakes ─────────────────────────────────────────────────────────────────
class FakeProvider:
    """Stands in for a real adapter; records calls, can fail or answer."""

    def __init__(self, name, *, reachable=True, configured=True, error=None, answer=None):
        from jarvis.core.providers import PROVIDER_SPECS

        self.spec = PROVIDER_SPECS[name]
        self.name = name
        self.reachable = reachable
        self.configured = configured
        self.error = error
        self.answer = answer or f"answer from {name}"
        self.calls = 0

    def is_configured(self):
        return self.configured

    def availability_problem(self):
        return "no API key" if not self.configured else ""

    def probe(self):
        if self.reachable:
            return True, 1, "ok"
        return False, 1, f"[{self.name}] connection refused"

    def complete(self, messages, **_kwargs):
        from jarvis.core.providers import LLMResponse

        self.calls += 1
        if self.error is not None:
            raise self.error
        return LLMResponse(content=self.answer, provider=self.name, model="fake")


def _manager(config, providers, chain=None):
    from jarvis.core.api_manager import APIManager

    from jarvis.core.api_manager import DEFAULT_CHAIN

    cfg = dict(config)
    # the shared test config pins the chain to stub; these tests want production's
    cfg["llm"] = dict(config["llm"], provider="auto",
                      fallback_chain=list(chain if chain is not None else DEFAULT_CHAIN))
    return APIManager(cfg, providers=providers)


def _llm_error(message, **kwargs):
    from jarvis.core.providers import LLMError

    return LLMError(message, provider="x", **kwargs)


# ── chain order + removals ────────────────────────────────────────────────
def test_default_chain_is_ollama_groq_gemini_openrouter_hf():
    from jarvis.core.api_manager import DEFAULT_CHAIN

    assert DEFAULT_CHAIN == ("ollama", "groq", "gemini", "openrouter", "hf")


def test_cerebras_is_gone_and_stub_is_not_in_production_chain(config):
    from jarvis.core.providers import PROVIDER_SPECS

    assert "cerebras" not in PROVIDER_SPECS
    manager = _manager(config, {}, chain=None)
    assert "cerebras" not in manager.chain
    assert "stub" not in manager.chain


def test_chain_drops_unknown_names_and_keeps_order(config):
    manager = _manager(config, {}, chain=["groq", "nonsense", "ollama", "groq", "hf"])
    assert manager.chain == ["groq", "ollama", "hf"]


def test_forced_provider_goes_first(config):
    from jarvis.core.api_manager import APIManager

    cfg = dict(config)
    cfg["llm"] = dict(config["llm"], provider="gemini",
                      fallback_chain=["ollama", "groq", "gemini", "openrouter", "hf"])
    assert APIManager(cfg, providers={}).chain == [
        "gemini", "ollama", "groq", "openrouter", "hf"]


# ── manager behaviour ─────────────────────────────────────────────────────
def test_ollama_answers_when_served(config):
    providers = {"ollama": FakeProvider("ollama"), "groq": FakeProvider("groq")}
    manager = _manager(config, providers)
    response = manager.complete([HumanMessage(content="hi")])
    assert response.provider == "ollama"
    assert response.fallback_from == ""
    assert providers["groq"].calls == 0


def test_ollama_not_running_falls_to_groq_and_is_reported(config):
    providers = {
        "ollama": FakeProvider("ollama", reachable=False),
        "groq": FakeProvider("groq"),
    }
    manager = _manager(config, providers)
    response = manager.complete([HumanMessage(content="hi")])
    assert response.provider == "groq"
    assert response.fallback_from == "ollama"
    assert response.degraded is True
    assert providers["ollama"].calls == 0          # never called a dead provider


def test_each_step_is_tried_in_order_until_one_answers(config):
    providers = {
        "ollama": FakeProvider("ollama", error=_llm_error("timeout after 120s")),
        "groq": FakeProvider("groq", error=_llm_error("server error 503")),
        "gemini": FakeProvider("gemini", error=_llm_error("HTTP 404: gone")),
        "openrouter": FakeProvider("openrouter", configured=False),
        "hf": FakeProvider("hf"),
    }
    manager = _manager(config, providers)
    response = manager.complete([HumanMessage(content="hi")])
    assert response.provider == "hf"
    assert response.fallback_from == "ollama"
    assert providers["openrouter"].calls == 0       # not configured → skipped
    assert providers["gemini"].calls == 1


def test_when_every_provider_fails_it_raises_instead_of_answering(config):
    from jarvis.core.api_manager import AllProvidersFailed

    providers = {name: FakeProvider(name, error=_llm_error("auth failed (401)",
                                                           retryable=False))
                 for name in ("ollama", "groq", "gemini", "openrouter", "hf")}
    manager = _manager(config, providers)
    with pytest.raises(AllProvidersFailed) as info:
        manager.complete([HumanMessage(content="hi")])
    message = str(info.value)
    assert "every LLM provider failed" in message
    assert [name for name, _ in info.value.attempts] == [
        "ollama", "groq", "gemini", "openrouter", "hf"]
    assert "stub" not in message


def test_no_keys_and_no_ollama_is_an_error_not_a_stub_reply(config):
    from jarvis.core.api_manager import AllProvidersFailed

    providers = {
        "ollama": FakeProvider("ollama", reachable=False),
        "groq": FakeProvider("groq", configured=False),
        "gemini": FakeProvider("gemini", configured=False),
        "openrouter": FakeProvider("openrouter", configured=False),
        "hf": FakeProvider("hf", configured=False),
    }
    manager = _manager(config, providers)
    with pytest.raises(AllProvidersFailed):
        manager.complete([HumanMessage(content="hi")])


def test_orchestrator_turn_reports_the_error(config, monkeypatch):
    """A turn with no working model shows an error event, not a fake answer."""
    from jarvis.core.api_manager import AllProvidersFailed
    from jarvis.core.orchestrator import Orchestrator
    from jarvis.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.load_all_default_tools(config)
    orchestrator = Orchestrator(config, registry=registry)
    orchestrator.initialize(ingest_knowledge=False, resume_session=False)

    def boom(*_args, **_kwargs):
        raise AllProvidersFailed("every LLM provider failed — ollama: down")

    monkeypatch.setattr(orchestrator.brain.api, "complete", boom)

    async def run():
        return [event async for event in orchestrator.process("hello there", stream=False)]

    import asyncio

    events = asyncio.run(run())
    errors = [e for e in events if e.type == "error"]
    assert errors and "no model could answer" in errors[0].text
    assert "offline mode" not in " ".join(e.text for e in events)
    orchestrator.shutdown()


# ── per-provider model retries ────────────────────────────────────────────
def _fake_post(responses):
    """Return a `_post_json` replacement that pops scripted responses."""
    seen = []

    def post(url, payload, headers, timeout, provider):
        seen.append({"url": url, "model": payload.get("model"), "headers": headers})
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    post.seen = seen
    return post


def _ok(model="m", text="hello"):
    return {"model": model, "choices": [{"message": {"content": text},
                                          "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def test_hf_skips_model_not_supported_and_uses_next_model(config, monkeypatch):
    from jarvis.core import providers
    from jarvis.core.providers import LLMError, build_provider

    cfg = dict(config)
    cfg["llm"] = dict(config["llm"], hf={"api_key": "hf_test"})
    provider = build_provider("hf", cfg)
    post = _fake_post([
        LLMError("HTTP 400: The requested model is not supported by any provider you "
                 "have enabled", provider="hf", retryable=False, status=400),
        _ok(model="openai/gpt-oss-20b", text="from second model"),
    ])
    monkeypatch.setattr(providers, "_post_json", post)
    response = provider.complete([HumanMessage(content="hi")])
    assert response.content == "from second model"
    assert [item["model"] for item in post.seen] == [
        provider.model_candidates[0], provider.model_candidates[1]]
    assert all(item["url"] == "https://router.huggingface.co/v1/chat/completions"
               for item in post.seen)


def test_hf_uses_configured_base_url(config, monkeypatch):
    from jarvis.core import providers
    from jarvis.core.providers import build_provider

    cfg = dict(config)
    cfg["llm"] = dict(config["llm"], hf={"api_key": "hf_test",
                                         "base_url": "https://example.test/v1/"})
    provider = build_provider("hf", cfg)
    post = _fake_post([_ok()])
    monkeypatch.setattr(providers, "_post_json", post)
    provider.complete([HumanMessage(content="hi")])
    assert post.seen[0]["url"] == "https://example.test/v1/chat/completions"


def test_auth_errors_do_not_retry_other_models(config, monkeypatch):
    from jarvis.core import providers
    from jarvis.core.providers import LLMError, build_provider

    cfg = dict(config)
    cfg["llm"] = dict(config["llm"], groq={"api_key": "gsk_test"})
    provider = build_provider("groq", cfg)
    post = _fake_post([LLMError("auth failed (401)", provider="groq",
                                retryable=False, status=401)])
    monkeypatch.setattr(providers, "_post_json", post)
    with pytest.raises(LLMError):
        provider.complete([HumanMessage(content="hi")])
    assert len(post.seen) == 1


def test_openrouter_retired_free_slug_moves_to_the_next_model(config, monkeypatch):
    from jarvis.core import providers
    from jarvis.core.providers import LLMError, build_provider

    cfg = dict(config)
    cfg["llm"] = dict(config["llm"], openrouter={"api_key": "sk-or-test"})
    provider = build_provider("openrouter", cfg)
    assert provider.model == "openrouter/free"
    post = _fake_post([
        LLMError("HTTP 404: This model is unavailable for free", provider="openrouter",
                 retryable=False, status=404),
        _ok(text="routed"),
    ])
    monkeypatch.setattr(providers, "_post_json", post)
    assert provider.complete([HumanMessage(content="hi")]).content == "routed"
    assert post.seen[1]["model"] == provider.model_candidates[1]


def test_gemini_retired_model_moves_on_and_key_is_not_in_url(config, monkeypatch):
    from jarvis.core import providers
    from jarvis.core.providers import LLMError, build_provider

    cfg = dict(config)
    cfg["llm"] = dict(config["llm"], gemini={"api_key": "AIza-test-key"})
    provider = build_provider("gemini", cfg)
    assert provider.model == "gemini-3.5-flash-lite"
    gemini_ok = {"candidates": [{"content": {"parts": [{"text": "hi from gemini"}]},
                                 "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3,
                                   "totalTokenCount": 5}}
    post = _fake_post([
        LLMError("HTTP 404: models/gemini-3.5-flash-lite is not found",
                 provider="gemini", retryable=False, status=404),
        gemini_ok,
    ])
    monkeypatch.setattr(providers, "_post_json", post)
    response = provider.complete([HumanMessage(content="hi")])
    assert response.content == "hi from gemini"
    assert response.model == provider.model_candidates[1]
    for item in post.seen:
        assert "key=" not in item["url"]
        assert item["headers"]["x-goog-api-key"] == "AIza-test-key"


def test_candidate_models_come_from_config_then_builtin(config):
    from jarvis.core.providers import build_provider

    cfg = dict(config)
    cfg["llm"] = dict(config["llm"], fallback_models={"groq": ["a-model", "b-model"]})
    provider = build_provider("groq", cfg)
    assert provider.model_candidates == ["llama-3.3-70b-versatile", "a-model", "b-model"]


def test_is_model_unavailable_classifies_errors():
    from jarvis.core.providers import LLMError, is_model_unavailable

    assert is_model_unavailable(LLMError("x", status=404))
    assert is_model_unavailable(LLMError("HTTP 400: model_not_supported", status=400))
    assert not is_model_unavailable(LLMError("HTTP 400: bad json", status=400))
    assert not is_model_unavailable(LLMError("auth failed (401)", status=401))
    assert not is_model_unavailable(LLMError("rate limited (429)", status=429))


# ── a model that is gone is skipped for the whole session ──────────────────
def test_model_unavailable_disables_provider_for_the_session(config):
    from jarvis.core.providers import LLMError

    gone = LLMError("[groq] 404 model retired", provider="groq", status=404)
    groq = FakeProvider("groq", error=gone)
    gemini = FakeProvider("gemini")
    manager = _manager(config, {"groq": groq, "gemini": gemini},
                       chain=["groq", "gemini"])

    assert manager.complete([HumanMessage(content="hi")]).provider == "gemini"
    assert manager.states["groq"].status == "disabled"
    assert manager.states["groq"].disabled is True

    # the next turn must not call groq again, even after backoff would have expired
    manager.states["groq"].backoff_until = 0.0
    assert manager.complete([HumanMessage(content="again")]).provider == "gemini"
    assert groq.calls == 1


def test_transient_error_is_not_a_session_disable(config):
    from jarvis.core.providers import LLMError

    flaky = FakeProvider("groq", error=LLMError("[groq] timeout", provider="groq", status=None))
    gemini = FakeProvider("gemini")
    manager = _manager(config, {"groq": flaky, "gemini": gemini}, chain=["groq", "gemini"])
    manager.complete([HumanMessage(content="hi")])
    assert manager.states["groq"].disabled is False
    assert manager.states["groq"].status == "down"


# ── agent routing uses whole-word triggers ────────────────────────────────
def test_browser_trigger_needs_a_whole_word():
    from jarvis.agents.agent_factory import _trigger_hit

    assert _trigger_hit("browser", "open the browser")
    assert not _trigger_hit("browser", "my browsers are slow")
    assert not _trigger_hit("go to", "algo tomorrow")
