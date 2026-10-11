"""Tool abstraction used across JARVIS.

Every capability (browser, screen, search, code…) is a :class:`Tool`: a name, a
description written *for an LLM*, a JSON-schema argument spec, a category and a
callable (sync or async).  The registry executes them uniformly, and the brain
uses ``category`` for selective binding.

The decorator infers the schema from type hints so tool authors write plain
Python functions::

    @tool(category="browser", description="Open a URL in Chrome")
    def open_url(url: str, new_tab: bool = False) -> str:
        ...
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import typing
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, List, Mapping, Optional, Sequence,
                    Tuple, get_args, get_origin)

from ..utils.logger import get_logger
from ..utils.sanitizer import truncate

log = get_logger("tools")

__all__ = ["Tool", "tool", "ToolResult", "describe_return"]


@dataclass
class ToolResult:
    """Normalised tool output (tools may return str, dict or this)."""

    ok: bool = True
    content: str = ""
    data: Any = None
    error: str = ""
    elapsed_ms: int = 0
    tool: str = ""
    artifacts: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.content if self.ok else f"Error: {self.error or self.content}"

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "content": self.content, "error": self.error,
                "tool": self.tool, "elapsed_ms": self.elapsed_ms,
                "artifacts": self.artifacts, "data": self.data}


def describe_return(value: Any, *, max_chars: int = 6000) -> str:
    """Turn any tool return value into text for the model."""
    if value is None:
        return "(no output)"
    if isinstance(value, ToolResult):
        return value.content if value.ok else f"Error: {value.error or value.content}"
    if isinstance(value, str):
        return truncate(value, max_chars)
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        text = str(value)
    return truncate(text, max_chars)


_TYPE_MAP: Dict[Any, str] = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def _schema_for_annotation(annotation: Any) -> Dict[str, Any]:
    if annotation is None:                     # -> None annotation
        return {"type": "string"}
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    if isinstance(annotation, str):
        # A module using `from __future__ import annotations` leaves raw strings
        # here; map the common names instead of degrading everything to string.
        lowered = annotation.lower()
        if "bool" in lowered:
            return {"type": "boolean"}
        if "int" in lowered:
            return {"type": "integer"}
        if "float" in lowered or "decimal" in lowered:
            return {"type": "number"}
        if "list" in lowered or "sequence" in lowered or "tuple" in lowered:
            return {"type": "array", "items": {"type": "string"}}
        if "dict" in lowered or "mapping" in lowered:
            return {"type": "object"}
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    origin = get_origin(annotation)
    if origin in (list, List, Sequence):
        args = get_args(annotation)
        item = _schema_for_annotation(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item}
    if origin in (dict, Dict, Mapping):
        return {"type": "object"}
    if origin is typing.Union or str(origin) == "typing.Union":
        args = [a for a in get_args(annotation) if a is not type(None)]
        return _schema_for_annotation(args[0]) if args else {"type": "string"}
    if isinstance(annotation, type):
        return {"type": _TYPE_MAP.get(annotation, "string")}
    return {"type": "string"}


class Tool:
    """A callable capability with an LLM-facing contract."""

    def __init__(self, func: Callable[..., Any], *, name: Optional[str] = None,
                 description: Optional[str] = None, category: str = "utility",
                 parameters: Optional[Mapping[str, Any]] = None,
                 requires: Optional[Sequence[str]] = None,
                 dangerous: bool = False, timeout: Optional[float] = None,
                 returns: str = ""):
        self.func = func
        self.name = name or func.__name__
        self.category = category
        self.is_async = inspect.iscoroutinefunction(func)
        self.dangerous = dangerous
        self.timeout = timeout
        self.requires = list(requires or [])
        self.returns = returns
        doc = inspect.getdoc(func) or ""
        self.description = description or doc.strip() or f"{self.name} tool"
        self._signature = inspect.signature(func)
        self.args_schema = dict(parameters) if parameters else self._infer_schema()
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.total_ms = 0

    # ── schema ─────────────────────────────────────────────────────────────
    def _infer_schema(self) -> Dict[str, Any]:
        properties: Dict[str, Any] = {}
        required: List[str] = []
        # `from __future__ import annotations` (used by every tool module) turns
        # annotations into strings — resolve them back into real types.
        try:
            resolved_hints = typing.get_type_hints(self.func)
        except Exception:
            resolved_hints = {}
        for pname, param in self._signature.parameters.items():
            if pname in ("self", "cls") or param.kind in (
                    inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            schema = _schema_for_annotation(resolved_hints.get(pname, param.annotation))
            doc = self._param_doc(pname)
            if doc:
                schema["description"] = doc
            if param.default is inspect.Parameter.empty:
                required.append(pname)
            else:
                if param.default is not None and param.default is not False:
                    schema["default"] = param.default
                elif param.default is False:
                    schema["default"] = False
            properties[pname] = schema
        schema: Dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def _param_doc(self, name: str) -> str:
        doc = inspect.getdoc(self.func) or ""
        match = None
        for line in doc.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}:") or stripped.startswith(f"{name} ("):
                match = stripped.split(":", 1)[-1].strip()
                break
            if stripped.startswith(f":param {name}:"):
                match = stripped.split(":", 2)[-1].strip()
                break
        return match or ""

    def schema(self) -> Dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name,
            "description": truncate(self.description, 900),
            "parameters": self.args_schema,
        }}

    # ── execution ──────────────────────────────────────────────────────────
    def invoke(self, args: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> ToolResult:
        payload = dict(args or {})
        payload.update(kwargs)
        start = time.perf_counter()
        try:
            payload = self._coerce(payload)
            if self.is_async:
                result = asyncio.run(self.func(**payload))  # type: ignore[misc]
            else:
                result = self.func(**payload)
        except TypeError as exc:
            self.failures += 1
            self.last_error = str(exc)
            return ToolResult(ok=False, tool=self.name,
                              error=f"bad arguments for {self.name}: {exc}. "
                                    f"Expected schema: {json.dumps(self.args_schema)}",
                              elapsed_ms=int((time.perf_counter() - start) * 1000))
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning(f"tool {self.name} failed: {self.last_error}")
            return ToolResult(ok=False, tool=self.name, error=self.last_error,
                              elapsed_ms=int((time.perf_counter() - start) * 1000))
        elapsed = int((time.perf_counter() - start) * 1000)
        self.calls += 1
        self.total_ms += elapsed
        return self._wrap(result, elapsed)

    async def ainvoke(self, args: Optional[Mapping[str, Any]] = None,
                      **kwargs: Any) -> ToolResult:
        payload = self._coerce({**dict(args or {}), **kwargs})
        start = time.perf_counter()
        try:
            if self.is_async:
                result = await self.func(**payload)  # type: ignore[misc]
            else:
                result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.func(**payload))
        except TypeError as exc:
            self.failures += 1
            return ToolResult(ok=False, tool=self.name,
                              error=f"bad arguments for {self.name}: {exc}",
                              elapsed_ms=int((time.perf_counter() - start) * 1000))
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning(f"tool {self.name} failed: {self.last_error}")
            return ToolResult(ok=False, tool=self.name, error=self.last_error,
                              elapsed_ms=int((time.perf_counter() - start) * 1000))
        elapsed = int((time.perf_counter() - start) * 1000)
        self.calls += 1
        self.total_ms += elapsed
        return self._wrap(result, elapsed)

    def _wrap(self, result: Any, elapsed_ms: int) -> ToolResult:
        if isinstance(result, ToolResult):
            result.tool = result.tool or self.name
            result.elapsed_ms = result.elapsed_ms or elapsed_ms
            return result
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
            ok, payload = result
            return ToolResult(ok=ok, content=describe_return(payload) if ok else "",
                              error="" if ok else describe_return(payload),
                              data=payload, tool=self.name, elapsed_ms=elapsed_ms)
        if isinstance(result, Mapping) and "ok" in result:
            return ToolResult(
                ok=bool(result.get("ok")),
                content=describe_return(result.get("content") or result.get("result")),
                data=result, error=str(result.get("error", "")),
                tool=self.name, elapsed_ms=elapsed_ms,
                artifacts=list(result.get("artifacts") or []))
        return ToolResult(ok=True, content=describe_return(result), data=result,
                          tool=self.name, elapsed_ms=elapsed_ms)

    def _coerce(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Drop unexpected keys and coerce ``"true"`` → ``True`` (small-model slop)."""
        props = self.args_schema.get("properties") or {}
        if props:
            unknown = [key for key in payload if key not in props]
            known = [key for key in payload if key in props]
            if unknown and not known:
                raise TypeError(
                    f"unknown argument(s) {unknown}; valid arguments for "
                    f"{self.name}: {sorted(props)}")
        coerced: Dict[str, Any] = {}
        for key, value in payload.items():
            if props and key not in props:
                continue
            spec = props.get(key) or {}
            kind = spec.get("type")
            if kind == "boolean" and isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            elif kind == "integer" and isinstance(value, str):
                try:
                    value = int(float(value))
                except ValueError:
                    pass
            elif kind == "number" and isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    pass
            coerced[key] = value
        return coerced

    # ── introspection ──────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        return {"name": self.name, "category": self.category, "calls": self.calls,
                "failures": self.failures, "avg_ms": int(self.total_ms / self.calls)
                if self.calls else 0, "dangerous": self.dangerous}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tool {self.name} [{self.category}]>"


def failure(message: str) -> Tuple[bool, str]:
    """Standard *failure* result for a tool: ``ok=False`` plus the reason.

    Tools must never return a plain string when they did not do the thing they
    were asked to do — the model (and the HTTP API) treats a string as success
    and will happily report a failure as an accomplishment.
    """
    return (False, str(message))


def tool(_func: Optional[Callable[..., Any]] = None, *, name: Optional[str] = None,
         description: Optional[str] = None, category: str = "utility",
         parameters: Optional[Mapping[str, Any]] = None,
         requires: Optional[Sequence[str]] = None, dangerous: bool = False,
         timeout: Optional[float] = None) -> Any:
    """Decorator producing a :class:`Tool` (usable bare or with arguments)."""
    def wrap(func: Callable[..., Any]) -> Tool:
        return Tool(func, name=name, description=description, category=category,
                    parameters=parameters, requires=requires, dangerous=dangerous,
                    timeout=timeout)

    if callable(_func):
        return wrap(_func)
    return wrap
