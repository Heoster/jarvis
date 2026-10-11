"""Code execution and analysis tools."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .bash_executor import SHELL_TOOLS, BashResult, run_bash
from .code_analyzer import code_find_symbol, code_outline, code_project_summary
from .python_executor import (
    PythonRunResult, analyze_code, analyze_python, run_python,
)

__all__ = ["register_tools", "run_python", "run_bash", "PythonRunResult",
           "BashResult", "analyze_code", "ALL_CODE_TOOLS"]

ALL_CODE_TOOLS = [run_python, analyze_python, run_bash, code_outline,
                  code_find_symbol, code_project_summary]


def register_tools(registry: Any, config: Optional[Mapping[str, Any]] = None) -> int:
    from .bash_executor import system_info, which_command

    tools = list(ALL_CODE_TOOLS) + [which_command, system_info]
    seen, unique = set(), []
    for tool in tools:
        if tool.name in seen:
            continue
        seen.add(tool.name)
        unique.append(tool)
    return registry.register_all(unique, "code")
