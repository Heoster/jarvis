"""LLM provider adapters.

Every adapter is a thin, dependency-light HTTP client (``httpx``) so JARVIS can
talk to a provider without pulling in a vendor SDK — that keeps installs small
and lets the API manager swap providers at runtime.

Supported, all on free tiers:

============  ==========================================================
``ollama``    local runtime (unlimited, works offline)
``groq``      OpenAI-compatible, very fast free tier
``gemini``    Google AI Studio free tier (different JSON shape)
``openrouter``OpenAI-compatible, free models (``openrouter/free`` router)
``hf``        Hugging Face inference router (OpenAI-compatible)
``stub``      offline template responder — test/dev only, never in the chain
============  ==========================================================

The production chain is ``ollama → groq → gemini → openrouter → hf``; when
every member fails the API manager raises and the operator sees the error.
Models that a provider has retired or does not serve (HTTP 404, or HF's
``model_not_supported``) are skipped inside that provider by trying the
configured ``fallback_models`` before the provider is given up on.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .text_tool_calls import extract_text_tool_calls
from ..utils.sanitizer import redact_secrets
from .messages import (
    AIMessage, ensure_messages, normalise_role, text_of, to_openai_messages,
)

log = get_logger("providers")

__all__ = [
    "ProviderSpec", "LLMResponse", "LLMError", "ProviderUnavailable",
    "BaseProvider", "OllamaProvider", "OpenAICompatProvider", "GeminiProvider",
    "HFProvider", "StubProvider", "PROVIDER_SPECS", "build_provider",
]


class LLMError(RuntimeError):
    """Any provider-side failure.  The API manager catches this and falls back."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = True,
                 status: Optional[int] = None):
        self.provider = provider
        self.retryable = retryable
        self.status = status
        super().__init__(f"[{provider or 'llm'}] {message}")


class ProviderUnavailable(LLMError):
    """The provider cannot be used at all here (no key, no binary, no network)."""


@dataclass
class LLMResponse:
    """Normalised completion result."""

    content: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    fallback_from: str = ""
    degraded: bool = False

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self) -> AIMessage:
        return AIMessage(content=self.content, tool_calls=self.tool_calls)

    def usage_line(self) -> str:
        return (f"{self.provider}/{self.model} · {self.latency_ms} ms · "
                f"{self.total_tokens or (self.prompt_tokens + self.completion_tokens)} tokens")


@dataclass
class ProviderSpec:
    """Static description of a provider."""

    name: str
    kind: str = "openai"                 # openai | ollama | gemini | hf | stub
    category: str = "llm"
    base_url: str = ""
    default_model: str = ""
    env_keys: Tuple[str, ...] = ()
    config_path: str = ""                # dotted path in config.yaml for api_key
    requires_key: bool = True
    free_limits: str = ""
    priority: int = 100
    capabilities: Tuple[str, ...] = ("chat", "tools")
    notes: str = ""
    docs: str = ""
    # Extra models tried, in order, when the chosen model is retired or not
    # served for this key (404 / model_not_supported). Overridable per provider
    # via ``llm.<name>.fallback_models`` in config.yaml.
    fallback_models: Tuple[str, ...] = ()

    def api_key(self, config: Mapping[str, Any]) -> str:
        section, _, key = self.config_path.partition(".")
        node: Any = config
        for part in self.config_path.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                node = None
                break
        if isinstance(node, str) and node:
            return node
        for env in self.env_keys:
            value = os.environ.get(env)
            if value:
                return value
        return ""

    def key_pool(self, config: Mapping[str, Any]) -> List[str]:
        """All usable keys: explicit pool + primary key, de-duplicated."""
        pool: List[str] = []
        section = config.get("llm") if isinstance(config, Mapping) else None
        if isinstance(section, Mapping):
            node = section.get(self.name)
            if isinstance(node, Mapping):
                pool.extend(str(k) for k in (node.get("key_pool") or []) if k)
        primary = self.api_key(config)
        if primary:
            pool.insert(0, primary)
        for env in self.env_keys:
            value = os.environ.get(env)
            if value:
                pool.append(value)
        return list(dict.fromkeys(pool))


PROVIDER_SPECS: Dict[str, ProviderSpec] = {
    "ollama": ProviderSpec(
        name="ollama", kind="ollama", base_url="http://localhost:11434",
        default_model="hermes3:3b", env_keys=("OLLAMA_HOST", "OLLAMA_BASE_URL"),
        config_path="llm.base_url", requires_key=False,
        free_limits="unlimited (local)", priority=10,
        capabilities=("chat", "tools", "stream"),
        notes="CPU-friendly, works offline, no key ever needed",
        docs="https://ollama.com",
    ),
    "groq": ProviderSpec(
        name="groq", kind="openai", base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile", env_keys=("GROQ_API_KEY",),
        config_path="llm.groq.api_key", free_limits="~14 400 req/day",
        priority=20, capabilities=("chat", "tools", "stream"),
        notes="fastest free inference, excellent tool calling",
        docs="https://console.groq.com/keys",
        fallback_models=("llama-3.1-8b-instant",),
    ),
    "openrouter": ProviderSpec(
        name="openrouter", kind="openai", base_url="https://openrouter.ai/api/v1",
        # ``openrouter/free`` is OpenRouter's own router over the free pool, so it
        # does not go stale the way individual ``:free`` slugs do (they are retired
        # with a 404 — e.g. meta-llama/llama-3.3-70b-instruct:free in July 2026).
        default_model="openrouter/free",
        env_keys=("OPENROUTER_API_KEY",), config_path="llm.openrouter.api_key",
        free_limits="~50 req/day on free models", priority=40,
        capabilities=("chat", "tools"),
        notes="free-model router; keep the ':free' suffix on explicit slugs",
        docs="https://openrouter.ai/keys",
        fallback_models=("openai/gpt-oss-120b:free",
                         "qwen/qwen3-next-80b-a3b-instruct:free"),
    ),
    "gemini": ProviderSpec(
        name="gemini", kind="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        # gemini-2.0-flash(-lite) was shut down on 2026-06-01 (404 for every key).
        default_model="gemini-3.5-flash-lite",
        env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        config_path="llm.gemini.api_key", free_limits="~1 500 req/day (Flash models)",
        priority=50, capabilities=("chat", "tools"),
        docs="https://aistudio.google.com/apikey",
        fallback_models=("gemini-3.1-flash-lite", "gemini-2.5-flash-lite",
                         "gemini-2.5-flash"),
    ),
    "hf": ProviderSpec(
        name="hf", kind="hf",
        # The old api-inference.huggingface.co host is retired; the router is the
        # OpenAI-compatible endpoint Hugging Face documents for chat.
        base_url="https://router.huggingface.co/v1",
        default_model="Qwen/Qwen2.5-7B-Instruct",
        env_keys=("HUGGINGFACE_API_KEY", "HF_TOKEN"), config_path="llm.hf.api_key",
        free_limits="serverless rate limited", priority=60,
        capabilities=("chat",),
        notes="text only; a model is usable only if an enabled HF provider serves it",
        docs="https://huggingface.co/settings/tokens",
        fallback_models=("openai/gpt-oss-20b", "meta-llama/Llama-3.1-8B-Instruct"),
    ),
    "stub": ProviderSpec(
        name="stub", kind="stub", requires_key=False, priority=999,
        free_limits="always available", capabilities=("chat",),
        notes="offline template responder used when everything else fails",
    ),
}


# ── HTTP plumbing ───────────────────────────────────────────────────────────
def _client(timeout: float = 60.0):
    import httpx  # imported lazily so `import jarvis` works without httpx

    return httpx.Client(timeout=timeout, follow_redirects=True)


def _post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str],
               timeout: float, provider: str) -> Dict[str, Any]:
    import httpx

    try:
        with _client(timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise LLMError(f"timeout after {timeout}s", provider=provider) from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"network error: {exc}", provider=provider) from exc

    if resp.status_code == 429:
        retry_after = resp.headers.get("retry-after", "")
        raise LLMError(f"rate limited (429) retry-after={retry_after or '?'}",
                       provider=provider, status=429)
    if resp.status_code in (401, 403):
        raise LLMError(f"auth failed ({resp.status_code}) — check the API key",
                       provider=provider, retryable=False, status=resp.status_code)
    if resp.status_code >= 500:
        raise LLMError(f"server error {resp.status_code}", provider=provider,
                       status=resp.status_code)
    if resp.status_code >= 400:
        detail = redact_secrets(resp.text[:400])
        raise LLMError(f"HTTP {resp.status_code}: {detail}", provider=provider,
                       retryable=False, status=resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise LLMError("provider returned non-JSON", provider=provider) from exc


_MODEL_UNAVAILABLE_MARKERS = (
    "model_not_supported", "not supported by any provider", "model not found",
    "not a valid model", "does not exist", "unavailable", "no longer available",
    "decommissioned", "retired", "is not found", "model_not_found",
)


def is_model_unavailable(exc: LLMError) -> bool:
    """True when the *model* (not the key or the network) is the problem."""
    if exc.status == 404:
        return True
    if exc.status == 400:
        text = str(exc).lower()
        return any(marker in text for marker in _MODEL_UNAVAILABLE_MARKERS)
    return False


def _extract_openai_tool_calls(message: Mapping[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") if isinstance(raw.get("function"), Mapping) else {}
        args = fn.get("arguments", raw.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                args = {"_raw": args}
        calls.append({
            "id": raw.get("id") or f"call_{len(calls)}_{int(time.time()*1000)%100000}",
            "name": fn.get("name") or raw.get("name", ""),
            "args": dict(args or {}),
        })
    # some models emit tool calls inside content as JSON
    if not calls:
        content = message.get("content") or ""
        if isinstance(content, str) and '"tool_calls"' in content:
            try:
                blob = json.loads(content[content.index("{"): content.rindex("}") + 1])
                for raw in blob.get("tool_calls", []):
                    fn = raw.get("function", raw)
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        args = json.loads(args)
                    calls.append({"id": raw.get("id", "call_parsed"),
                                  "name": fn.get("name", ""), "args": args})
            except Exception:
                pass
    return calls


# ── Base ────────────────────────────────────────────────────────────────────
class BaseProvider:
    """Common behaviour: config binding, model choice, usage accounting."""

    spec: ProviderSpec = ProviderSpec(name="base")

    def __init__(self, config: Mapping[str, Any], *, timeout: float = 60.0):
        self.config = config
        self.llm_cfg: Mapping[str, Any] = (config.get("llm") or {}) if config else {}
        self.timeout = float(self.llm_cfg.get("request_timeout", timeout) or timeout)
        self._key_index = 0

    # identity -------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def model(self) -> str:
        models = self.llm_cfg.get("provider_models") or {}
        if isinstance(models, Mapping) and models.get(self.name):
            return str(models[self.name])
        node = self.llm_cfg.get(self.name)
        if isinstance(node, Mapping) and node.get("model"):
            return str(node["model"])
        if self.name == "ollama" and self.llm_cfg.get("model"):
            return str(self.llm_cfg["model"])
        return self.spec.default_model

    @property
    def base_url(self) -> str:
        if self.name == "ollama":
            from .ollama_runtime import resolve_base_url

            return resolve_base_url(self.config)
        node = self.llm_cfg.get(self.name)
        if isinstance(node, Mapping) and node.get("base_url"):
            return str(node["base_url"]).rstrip("/")
        return self.spec.base_url

    @property
    def model_candidates(self) -> List[str]:
        """The configured model first, then the provider's fallback models."""
        models = [self.model]
        extra: Any = None
        # config: llm.fallback_models.<provider>  (or llm.<provider>.fallback_models)
        shared = self.llm_cfg.get("fallback_models")
        if isinstance(shared, Mapping):
            extra = shared.get(self.name)
        node = self.llm_cfg.get(self.name)
        if extra is None and isinstance(node, Mapping):
            extra = node.get("fallback_models")
        if extra is None:
            extra = self.spec.fallback_models
        for name in extra or []:
            if name and str(name) not in models:
                models.append(str(name))
        return models

    def _try_models(self, call: Any) -> LLMResponse:
        """Run ``call(model)`` for each candidate until one is served.

        Only "this model is not available to this key" errors move on to the
        next model; auth, quota and network errors are raised immediately so the
        API manager handles them as a provider-level failure.
        """
        last: Optional[LLMError] = None
        for model in self.model_candidates:
            try:
                return call(model)
            except LLMError as exc:
                if not is_model_unavailable(exc):
                    raise
                last = exc
                log.warning(f"{self.name}: model '{model}' not served for this key, "
                            f"trying the next model")
        raise last or LLMError("no model configured", provider=self.name,
                               retryable=False)

    # credentials ----------------------------------------------------------
    @property
    def keys(self) -> List[str]:
        return self.spec.key_pool(self.config)

    def next_key(self) -> str:
        keys = self.keys
        if not keys:
            return ""
        key = keys[self._key_index % len(keys)]
        self._key_index += 1
        return key

    def header(self, key: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def is_configured(self) -> bool:
        if self.spec.requires_key and not self.keys:
            return False
        return True

    def availability_problem(self) -> str:
        if self.spec.requires_key and not self.keys:
            return (f"no API key (set {self.spec.env_keys[0]} "
                    f"or llm.{self.name}.api_key in config.yaml)")
        return ""

    # capability probe -----------------------------------------------------
    def probe(self) -> Tuple[bool, int, str]:
        """Cheap liveness check → ``(ok, latency_ms, detail)``."""
        start = time.perf_counter()
        try:
            detail = self._probe_impl()
            return True, int((time.perf_counter() - start) * 1000), detail
        except LLMError as exc:
            return False, int((time.perf_counter() - start) * 1000), str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return False, int((time.perf_counter() - start) * 1000), f"{type(exc).__name__}: {exc}"

    def _probe_impl(self) -> str:
        raise NotImplementedError

    # completion -----------------------------------------------------------
    def complete(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 json_mode: bool = False) -> LLMResponse:
        raise NotImplementedError

    # shared helpers -------------------------------------------------------
    def _temperature(self, override: Optional[float]) -> float:
        if override is not None:
            return float(override)
        return float(self.llm_cfg.get("temperature", 0.3))

    def _max_tokens(self, override: Optional[int]) -> int:
        if override is not None:
            return int(override)
        return int(self.llm_cfg.get("max_tokens", 2048))

    @staticmethod
    def _tool_schemas(tools: Optional[Sequence[Any]]) -> List[Dict[str, Any]]:
        schemas: List[Dict[str, Any]] = []
        for tool in tools or []:
            schema = getattr(tool, "schema", None)
            if callable(schema):
                schema = schema()
            if isinstance(schema, Mapping):
                schemas.append(dict(schema))
                continue
            if isinstance(tool, Mapping) and "function" in tool:
                schemas.append(dict(tool))
                continue
            name = getattr(tool, "name", None)
            if not name:
                continue
            description = (getattr(tool, "description", "") or "")[:600]
            args_schema = getattr(tool, "args_schema", None)
            params: Dict[str, Any] = {"type": "object", "properties": {}}
            if isinstance(args_schema, Mapping):
                params = dict(args_schema)
            elif hasattr(args_schema, "model_json_schema"):
                try:
                    params = args_schema.model_json_schema()  # type: ignore[union-attr]
                except Exception:
                    params = {"type": "object", "properties": {}}
            schemas.append({"type": "function", "function": {
                "name": name, "description": description, "parameters": params}})
        return schemas


# ── Providers ───────────────────────────────────────────────────────────────
class OllamaProvider(BaseProvider):
    """Local Ollama runtime — the first choice, because it is free and offline."""

    spec = PROVIDER_SPECS["ollama"]

    def ensure_running(self) -> None:
        """Start ``ollama serve`` if it is down and allowed; raise if still down."""
        from .ollama_runtime import autostart_enabled, autostart_timeout
        from .ollama_runtime import ensure_running as _ensure

        ok, detail = _ensure(self.base_url, autostart=autostart_enabled(self.config),
                             timeout=autostart_timeout(self.config))
        if not ok:
            raise LLMError(detail, provider=self.name)

    def _probe_impl(self) -> str:
        import httpx

        self.ensure_running()
        try:
            with _client(4.0) as client:
                resp = client.get(f"{self.base_url}/api/tags")
        except httpx.HTTPError as exc:
            raise LLMError(f"not reachable at {self.base_url}: {exc}", provider=self.name) from exc
        if resp.status_code != 200:
            raise LLMError(f"ollama returned {resp.status_code}", provider=self.name)
        models = [m.get("name", "") for m in (resp.json().get("models") or [])]
        wanted = self.model
        if wanted and models and not any(m.startswith(wanted.split(":")[0]) for m in models):
            raise LLMError(
                f"model '{wanted}' not pulled. Available: {', '.join(models[:5]) or 'none'}. "
                f"Run: ollama pull {wanted}", provider=self.name, retryable=False)
        return f"{len(models)} model(s) available, using {wanted or models[0] if models else '?'}"

    def complete(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 json_mode: bool = False) -> LLMResponse:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(ensure_messages(messages)),
            "stream": False,
            "options": {
                "temperature": self._temperature(temperature),
                "num_predict": self._max_tokens(max_tokens),
                "num_ctx": int(self.llm_cfg.get("context_window", 16384)),
            },
        }
        schemas = self._tool_schemas(tools)
        if schemas:
            payload["tools"] = schemas
        if json_mode:
            payload["format"] = "json"

        start = time.perf_counter()
        try:
            data = _post_json(f"{self.base_url}/api/chat", payload,
                              {"Content-Type": "application/json"}, self.timeout, self.name)
        except LLMError as exc:
            if exc.status is not None:
                raise
            # connection refused / timeout: Ollama may have stopped since the probe
            self.ensure_running()
            data = _post_json(f"{self.base_url}/api/chat", payload,
                              {"Content-Type": "application/json"}, self.timeout, self.name)
        latency = int((time.perf_counter() - start) * 1000)

        message = data.get("message") or {}
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or raw
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            calls.append({"id": raw.get("id") or f"call_{len(calls)}",
                          "name": fn.get("name", ""), "args": dict(args or {})})

        content = message.get("content") or ""
        if not calls:
            # small models often write the call as text; recover it, but only for bound tools
            calls, content = extract_text_tool_calls(content, tools)

        return LLMResponse(
            content=content,
            tool_calls=calls, provider=self.name, model=self.model,
            prompt_tokens=int(data.get("prompt_eval_count", 0) or 0),
            completion_tokens=int(data.get("eval_count", 0) or 0),
            total_tokens=int((data.get("prompt_eval_count", 0) or 0)
                             + (data.get("eval_count", 0) or 0)),
            latency_ms=latency, finish_reason=str(data.get("done_reason", "")),
            raw={"model": data.get("model"), "done": data.get("done")},
        )


class OpenAICompatProvider(BaseProvider):
    """Groq / OpenRouter / any ``/chat/completions`` endpoint."""

    def __init__(self, config: Mapping[str, Any], spec: ProviderSpec, *, timeout: float = 60.0):
        self.spec = spec
        super().__init__(config, timeout=timeout)

    def header(self, key: str) -> Dict[str, str]:
        headers = super().header(key)
        if self.name == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/Heoster/jarvis"
            headers["X-Title"] = "JARVIS (Heoster)"
        return headers

    def _probe_impl(self) -> str:
        key = self.next_key()
        with _client(8.0) as client:
            resp = client.get(f"{self.base_url}/models", headers=self.header(key))
        if resp.status_code == 401:
            raise LLMError("invalid API key", provider=self.name, retryable=False)
        if resp.status_code == 429:
            raise LLMError("rate limited during probe", provider=self.name)
        if resp.status_code >= 400:
            raise LLMError(f"probe HTTP {resp.status_code}", provider=self.name)
        try:
            models = [m.get("id", "") for m in (resp.json().get("data") or [])]
        except Exception:
            models = []
        candidates = self.model_candidates
        if models and not any(model in models for model in candidates):
            raise LLMError(f"none of {', '.join(candidates)} is offered; try "
                           f"{', '.join(models[:3])}", provider=self.name, retryable=False)
        return f"{len(models) or '✓'} model(s); using {candidates[0]}"

    def complete(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 json_mode: bool = False) -> LLMResponse:
        key = self.next_key()
        msgs = to_openai_messages(ensure_messages(messages))
        schemas = self._tool_schemas(tools)

        def call(model: str) -> LLMResponse:
            payload: Dict[str, Any] = {
                "model": model,
                "messages": msgs,
                "temperature": self._temperature(temperature),
                "max_tokens": self._max_tokens(max_tokens),
                "stream": False,
            }
            if schemas and "tools" in self.spec.capabilities:
                payload["tools"] = schemas
                payload["tool_choice"] = "auto"
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            start = time.perf_counter()
            data = _post_json(f"{self.base_url}/chat/completions", payload,
                              self.header(key), self.timeout, self.name)
            latency = int((time.perf_counter() - start) * 1000)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            usage = data.get("usage") or {}
            content = text_of(message.get("content") or "")
            calls = _extract_openai_tool_calls(message)
            if not calls:
                calls, content = extract_text_tool_calls(content, tools)
            return LLMResponse(
                content=content,
                tool_calls=calls,
                provider=self.name, model=str(data.get("model") or model),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                latency_ms=latency, finish_reason=str(choice.get("finish_reason", "")),
                raw={"id": data.get("id")},
            )

        return self._try_models(call)


class GeminiProvider(BaseProvider):
    """Google AI Studio (different request/response shape)."""

    spec = PROVIDER_SPECS["gemini"]

    def _probe_impl(self) -> str:
        key = self.next_key()
        # the key travels in a header, never in the URL, so it cannot leak into logs
        with _client(8.0) as client:
            resp = client.get(f"{self.base_url}/models", headers={"x-goog-api-key": key})
        if resp.status_code in (400, 401, 403):
            raise LLMError("invalid API key", provider=self.name, retryable=False)
        if resp.status_code >= 400:
            raise LLMError(f"probe HTTP {resp.status_code}", provider=self.name)
        try:
            models = [m.get("name", "").replace("models/", "")
                      for m in (resp.json().get("models") or [])]
        except Exception:
            models = []
        candidates = self.model_candidates
        if models and not any(model in models for model in candidates):
            raise LLMError(f"none of {', '.join(candidates)} is offered; e.g. "
                           f"{', '.join(models[:3])}", provider=self.name, retryable=False)
        return f"{len(models)} model(s); using {candidates[0]}"

    @staticmethod
    def _convert(messages: Sequence[Any]) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []
        for msg in ensure_messages(messages):
            role = normalise_role(msg.role)
            if role == "system":
                continue  # handled via systemInstruction
            if role == "tool":
                contents.append({"role": "user", "parts": [
                    {"text": f"[tool result {msg.name or ''}] {msg.content}"}]})
                continue
            parts: List[Dict[str, Any]] = []
            if msg.content:
                parts.append({"text": msg.content})
            for call in msg.tool_calls:
                parts.append({"functionCall": {"name": call.get("name"),
                                               "args": call.get("args") or {}}})
            if not parts:
                parts.append({"text": ""})
            contents.append({"role": "model" if role == "ai" else "user", "parts": parts})
        return contents

    def complete(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 json_mode: bool = False) -> LLMResponse:
        key = self.next_key()
        msgs = ensure_messages(messages)
        system_text = "\n\n".join(m.content for m in msgs
                                  if normalise_role(m.role) == "system" and m.content)

        payload: Dict[str, Any] = {
            "contents": self._convert(msgs),
            "generationConfig": {
                "temperature": self._temperature(temperature),
                "maxOutputTokens": self._max_tokens(max_tokens),
            },
        }
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        schemas = self._tool_schemas(tools)
        if schemas:
            payload["tools"] = [{"functionDeclarations": [
                {"name": s["function"]["name"],
                 "description": s["function"].get("description", ""),
                 "parameters": _strip_titles(s["function"].get("parameters") or
                                             {"type": "object", "properties": {}})}
                for s in schemas]}]

        def call(model: str) -> LLMResponse:
            start = time.perf_counter()
            data = _post_json(f"{self.base_url}/models/{model}:generateContent", payload,
                              {"Content-Type": "application/json", "x-goog-api-key": key},
                              self.timeout, self.name)
            latency = int((time.perf_counter() - start) * 1000)
            candidate = (data.get("candidates") or [{}])[0]
            parts = ((candidate.get("content") or {}).get("parts") or [])
            text_parts, calls = [], []
            for part in parts:
                if "text" in part:
                    text_parts.append(part["text"])
                if "functionCall" in part:
                    fn = part["functionCall"]
                    calls.append({"id": f"call_{len(calls)}_{int(time.time()*1000)%100000}",
                                  "name": fn.get("name", ""),
                                  "args": dict(fn.get("args") or {})})
            usage = data.get("usageMetadata") or {}
            return LLMResponse(
                content="".join(text_parts), tool_calls=calls,
                provider=self.name, model=model,
                prompt_tokens=int(usage.get("promptTokenCount", 0) or 0),
                completion_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
                total_tokens=int(usage.get("totalTokenCount", 0) or 0),
                latency_ms=latency, finish_reason=str(candidate.get("finishReason", "")),
            )

        return self._try_models(call)


class HFProvider(BaseProvider):
    """Hugging Face inference router (OpenAI-compatible chat completions).

    Failure modes this adapter handles:

    * the router URL comes from the spec / ``llm.hf.base_url`` (it used to be
      hard-coded, so config changes were silently ignored);
    * ``400 model_not_supported`` means "no provider you enabled serves this
      model" — the next candidate model is tried instead of failing the turn;
    * the probe tolerates a non-JSON body instead of crashing the health check.
    """

    spec = PROVIDER_SPECS["hf"]

    def _probe_impl(self) -> str:
        key = self.next_key()
        with _client(8.0) as client:
            resp = client.get("https://huggingface.co/api/whoami-v2",
                              headers={"Authorization": f"Bearer {key}"})
        if resp.status_code in (401, 403):
            raise LLMError("invalid HF token", provider=self.name, retryable=False)
        if resp.status_code >= 400:
            raise LLMError(f"probe HTTP {resp.status_code}", provider=self.name)
        try:
            name = resp.json().get("name", "unknown")
        except ValueError:
            name = "unknown"
        return f"token ok for {name}; model {self.model_candidates[0]}"

    def complete(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 json_mode: bool = False) -> LLMResponse:
        key = self.next_key()
        msgs = to_openai_messages(ensure_messages(messages))

        def call(model: str) -> LLMResponse:
            payload: Dict[str, Any] = {
                "model": model,
                "messages": msgs,
                "temperature": self._temperature(temperature),
                "max_tokens": self._max_tokens(max_tokens),
                "stream": False,
            }
            start = time.perf_counter()
            data = _post_json(f"{self.base_url}/chat/completions", payload,
                              self.header(key), self.timeout, self.name)
            latency = int((time.perf_counter() - start) * 1000)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            usage = data.get("usage") or {}
            content = text_of(message.get("content") or "")
            calls = _extract_openai_tool_calls(message)
            if not calls:
                calls, content = extract_text_tool_calls(content, tools)
            return LLMResponse(
                content=content,
                tool_calls=calls,
                provider=self.name, model=str(data.get("model") or model),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                total_tokens=int(usage.get("total_tokens", 0) or 0),
                latency_ms=latency, finish_reason=str(choice.get("finish_reason", "")),
            )

        return self._try_models(call)


class StubProvider(BaseProvider):
    """Guaranteed last resort: no network, no model, still useful.

    It is deliberately honest — it never pretends to reason about open-ended
    questions.  Its job is to (a) keep the CLI alive when the machine is
    offline, (b) emit *valid tool calls* for the obvious commands so the agent
    can still act, and (c) print a clear instruction on how to get a real
    model running.
    """

    spec = PROVIDER_SPECS["stub"]

    def is_configured(self) -> bool:
        return True

    def _probe_impl(self) -> str:
        return "always available (offline template responder)"

    def complete(self, messages: Sequence[Any], *, tools: Optional[Sequence[Any]] = None,
                 temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                 json_mode: bool = False) -> LLMResponse:
        msgs = ensure_messages(messages)
        last_user = ""
        for msg in reversed(msgs):
            if normalise_role(msg.role) == "human":
                last_user = msg.content
                break
        tool_names = {getattr(t, "name", "") for t in (tools or [])}
        had_tool_result = any(normalise_role(m.role) == "tool" for m in msgs)

        calls: List[Dict[str, Any]] = []
        lowered = last_user.lower()
        if not had_tool_result and tool_names:
            import re

            match = re.search(r"https?://\S+", last_user)
            pct_of = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)\s*(?:of|ka)\s*"
                r"(\d+(?:\.\d+)?)", last_user, re.I)
            pct_ratio = re.search(
                r"what\s+(?:percent(?:age)?|%|part)[^0-9]{0,24}?"
                r"(\d+(?:\.\d+)?)\s*(?:of|out of|is|/)\s*(\d+(?:\.\d+)?)",
                last_user, re.I)
            expr_match = re.search(
                r"[-+]?\d+(?:\.\d+)?(?:\s*[-+*/%^]\s*[-+]?\d+(?:\.\d+)?)+",
                last_user)
            arithmetic = bool(expr_match)
            if match and {"jina_read_url", "open_url"} & tool_names:
                name = "jina_read_url" if "jina_read_url" in tool_names else "open_url"
                calls.append({"id": "call_stub_1", "name": name,
                              "args": {"url": match.group(0)}})
            elif pct_of and "calculate" in tool_names:
                # "15% of 4200" → arithmetic, not a part/whole ratio question
                calls.append({"id": "call_stub_pct_of", "name": "calculate",
                              "args": {"expression":
                                       f"({pct_of.group(1)}/100*{pct_of.group(2)})"}})
            elif pct_ratio and "percentage" in tool_names:
                calls.append({"id": "call_stub_pct", "name": "percentage",
                              "args": {"part": float(pct_ratio.group(1)),
                                       "whole": float(pct_ratio.group(2))}})
            elif arithmetic and "calculate" in tool_names:
                expr = expr_match.group(0).strip().rstrip(".,")
                calls.append({"id": "call_stub_0", "name": "calculate",
                              "args": {"expression": expr}})
            elif tool_names & {"get_current_datetime"} and any(
                    w in lowered for w in ("time", "date", "day", "samay")):
                calls.append({"id": "call_stub_2", "name": "get_current_datetime",
                              "args": {}})
            elif tool_names & {"search_web"} and any(
                    w in lowered for w in ("search", "find", "who is", "what is",
                                           "kya", "latest", "news")):
                calls.append({"id": "call_stub_3", "name": "search_web",
                              "args": {"query": last_user[:200]}})

        if calls:
            return LLMResponse(content="", tool_calls=calls, provider=self.name,
                               model="stub", latency_ms=0,
                               finish_reason="tool_calls")

        if had_tool_result:
            last_tool = next((m for m in reversed(msgs)
                              if normalise_role(m.role) == "tool"), None)
            body = (last_tool.content if last_tool else "")[:1200]
            content = (f"**Tool output** ({getattr(last_tool, 'name', 'tool')}):\n\n"
                       f"```\n{body}\n```\n\n_No LLM is connected, so this is the raw "
                       f"result. Run `ollama pull hermes3:3b` or add a free API key to "
                       f"get reasoning._")
        else:
            content = (
                "Standing by, Heoster. No LLM backend is reachable right now, so I am "
                "running in **offline mode**.\n\n"
                "To give me a brain (all free):\n"
                "1. `ollama serve` then `ollama pull hermes3:3b`  — local, no key, no cost\n"
                "2. or set a free key: `export GROQ_API_KEY=...` "
                "(console.groq.com), `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, "
                "`HUGGINGFACE_API_KEY`\n\n"
                "Everything that does not need a model still works: browser control, "
                "screen control, search, memory, files.\n\n"
                f"_(you said: {last_user[:200]})_"
            )
        return LLMResponse(content=content, provider=self.name, model="stub",
                           latency_ms=0, finish_reason="stop")


def _strip_titles(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini rejects JSON-Schema ``title`` keys and some ``$ref`` shapes."""
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()
                    if k not in ("title", "additionalProperties", "$schema", "default")}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    cleaned = walk(schema)
    if not isinstance(cleaned, dict) or not cleaned:
        return {"type": "object", "properties": {}}
    cleaned.setdefault("type", "object")
    return cleaned


def build_provider(name: str, config: Mapping[str, Any], *,
                   timeout: float = 60.0) -> BaseProvider:
    """Factory used by the API manager."""
    spec = PROVIDER_SPECS.get(name)
    if spec is None:
        raise KeyError(f"unknown provider '{name}'")
    if spec.kind == "ollama":
        return OllamaProvider(config, timeout=timeout)
    if spec.kind == "openai":
        return OpenAICompatProvider(config, spec, timeout=timeout)
    if spec.kind == "gemini":
        return GeminiProvider(config, timeout=timeout)
    if spec.kind == "hf":
        return HFProvider(config, timeout=timeout)
    return StubProvider(config, timeout=timeout)
