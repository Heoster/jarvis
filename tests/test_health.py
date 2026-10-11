"""The health checks CI gates on: broken exports, CLI paths that used to NameError.

These are the cheap, high-signal checks that a full linter backlog would drown
out — every ``__all__`` entry must exist, and the commands that touch dates must
actually run.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import jarvis

#: modules whose ``__all__`` is consumed by star-imports elsewhere
SPOTLIGHT = [
    "jarvis.tools.screen.mouse_keyboard",
    "jarvis.tools.utility.clipboard",
    "jarvis.tools.browser",
    "jarvis.tools.obsidian",
]


def _all_jarvis_modules() -> list[str]:
    names = []
    for module in pkgutil.walk_packages(jarvis.__path__, prefix="jarvis."):
        names.append(module.name)
    return sorted(names)


@pytest.mark.parametrize("name", SPOTLIGHT)
def test_every_export_exists(name):
    module = importlib.import_module(name)
    missing = [entry for entry in getattr(module, "__all__", [])
               if not hasattr(module, entry)]
    assert not missing, f"{name}.__all__ lists names that do not exist: {missing}"


def test_no_module_declares_a_broken_export():
    """Cheap sweep over the whole package — catches the F822 class of bug."""
    broken = {}
    for name in _all_jarvis_modules():
        try:
            module = importlib.import_module(name)
        except Exception:                     # optional engine/dependency missing
            continue
        missing = [entry for entry in getattr(module, "__all__", [])
                   if not hasattr(module, entry)]
        if missing:
            broken[name] = missing
    assert not broken, f"broken __all__ entries: {broken}"


def test_cli_commands_that_take_dates_run(config, home, tmp_path):
    """`jarvis coach brief --day …` used to raise NameError: date."""
    from click.testing import CliRunner

    from jarvis.interfaces.cli import main

    vault = tmp_path / "Vault"
    (vault / "01 Subjects" / "Physics").mkdir(parents=True)
    (vault / "02 Daily Notes").mkdir(parents=True)

    runner = CliRunner()
    for args in (["coach", "--vault", str(vault), "brief", "--day", "2026-01-05"],
                 ["coach", "--vault", str(vault), "weekly", "--day", "2026-01-05"],
                 ["coach", "--vault", str(vault), "brief"]):
        result = runner.invoke(main, args)
        assert "NameError" not in str(result.exception), (args, result.exception)
        assert result.exit_code == 0, (args, result.output, result.exception)


def test_the_wheel_would_contain_the_runtime_files():
    """The package job builds a wheel — make sure the package tree is complete."""
    package_dir = Path(jarvis.__file__).parent
    for needed in ("config.py", "core/brain.py", "core/system_prompt.py",
                   "tools/browser/media.py", "tools/obsidian/coach.py",
                   "tools/registry.py", "interfaces/cli.py", "__main__.py"):
        assert (package_dir / needed).is_file(), f"missing from the package: {needed}"
