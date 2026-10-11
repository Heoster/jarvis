"""User interfaces: CLI, local API server, optional web UI.

``cli`` is imported eagerly (it is the entry point); ``api_server`` needs the
optional web extra, so it is exposed lazily and only raises when it is actually
asked for.
"""

from __future__ import annotations

import importlib

__all__ = ["cli", "api_server"]

from . import cli  # noqa: E402  (the CLI has no optional dependencies)


def __getattr__(name: str):
    if name == "api_server":                     # needs the `web` extra (fastapi)
        return importlib.import_module("jarvis.interfaces.api_server")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
