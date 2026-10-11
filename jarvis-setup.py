#!/usr/bin/env python3
"""JARVIS — one setup command that works on any platform.

    python jarvis-setup.py

This is a thin launcher: it picks the right native installer for the machine and
hands your arguments straight through, so the real logic lives in exactly one
place per platform.

    Windows            scripts/jarvis-setup.ps1   (PowerShell)
    Linux / macOS      scripts/jarvis-setup.sh    (bash)

Why a launcher rather than one big Python script: the OS-level half of the job
(package managers, Tesseract, Playwright's browser download, winget) is
genuinely platform-specific, and each native script can be read and run on its
own — ``bash scripts/jarvis-setup.sh`` and the PowerShell equivalent both work
without this file.

Run either installer with ``--help`` (bash) or ``Get-Help`` (PowerShell) for the
full option list. The flags below are passed through unchanged on Linux/macOS,
and translated to their PowerShell equivalents on Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent

# bash flag → PowerShell parameter. Everything else is identical in both.
_FLAG_TRANSLATION = {
    "--skip-browser": "-SkipBrowser",
    "--skip-tests": "-SkipTests",
    "--skip-system": "-SkipSystem",
    "--model": "-Model",
    "--web": "-Web",
    "--no-dev": "-NoDev",
    "--yes": "-Yes",
    "-y": "-Yes",
}

USAGE = """\
JARVIS setup — one command, any platform.

Usage: python jarvis-setup.py [options]

  --python PATH     interpreter to build the venv from
  --skip-browser    skip the Playwright Chromium download (~150 MB)
  --skip-tests      skip the verification stage (pytest, ruff, wheel build)
  --skip-system     never install OS packages; report only
  --model           also pull the default ollama model (~2 GB)
  --web             also install the web UI extras (FastAPI, uvicorn, Gradio)
  --no-dev          skip the dev extras (pytest, pytest-asyncio, ruff)
  --yes, -y         do not prompt before installing OS packages
  -h, --help        this text

Windows runs scripts/jarvis-setup.ps1, Linux and macOS run
scripts/jarvis-setup.sh. Both can also be invoked directly.
"""


def _fail(message: str) -> NoReturn:
    print(f"\n  X {message}", file=sys.stderr)
    return sys.exit(1)


def run_windows(argv: list[str]) -> int:
    script = ROOT / "scripts" / "jarvis-setup.ps1"
    if not script.is_file():
        return _fail(f"missing {script.relative_to(ROOT)}")

    args: list[str] = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--python":
            if index + 1 >= len(argv):
                return _fail("--python needs a value")
            args += ["-Python", argv[index + 1]]
            index += 2
            continue
        if flag in ("-h", "--help"):
            print(USAGE)
            return 0
        if flag in _FLAG_TRANSLATION:
            args.append(_FLAG_TRANSLATION[flag])
        else:
            args.append(flag)          # let PowerShell raise the real error
        index += 1

    # pwsh (PowerShell 7) if present, otherwise Windows PowerShell 5.1.
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        return _fail("neither pwsh nor powershell is on PATH")

    command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(script), *args]
    print(f"→ {' '.join(command[:1])} … {script.name} {' '.join(args)}".rstrip())
    return subprocess.call(command, cwd=str(ROOT))


def run_unix(argv: list[str]) -> int:
    script = ROOT / "scripts" / "jarvis-setup.sh"
    if not script.is_file():
        return _fail(f"missing {script.relative_to(ROOT)}")

    bash = os.environ.get("BASH") or shutil.which("bash")
    if not bash:
        return _fail("bash not found on PATH")

    return subprocess.call([bash, str(script), *argv], cwd=str(ROOT))


def main(argv: list[str]) -> int:
    if os.name == "nt":
        return run_windows(argv)
    return run_unix(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
