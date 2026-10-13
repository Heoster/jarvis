#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  JARVIS — all-in-one setup for Linux and macOS (Windows: setup.ps1).
#
#  One command takes you from a fresh clone to a verified install:
#
#      bash scripts/jarvis-setup.sh
#
#  It creates the virtualenv, installs the core and focus-area dependencies,
#  the dev extras, the Chromium build Playwright drives, the OS-level tools the
#  screen agent needs (OCR, window control), copies .env, then *proves* the
#  install by running the test suite, the lint gate, a wheel build and
#  `jarvis doctor`.
#
#  Everything optional degrades to a warning instead of aborting: JARVIS is
#  designed to run with any subset of its capabilities.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── options ──────────────────────────────────────────────────────────────────
SKIP_BROWSER=0     # --skip-browser   no Chromium download (~150 MB)
SKIP_TESTS=0       # --skip-tests     no pytest / ruff / wheel build
SKIP_SYSTEM=0      # --skip-system    never touch the OS package manager
WITH_MODEL=0       # --model          also `ollama pull` the default brain (~2 GB)
WITH_WEB=0         # --web            also the FastAPI + Gradio web UI extras
NO_DEV=0           # --no-dev         skip pytest / ruff / build
PYTHON="${PYTHON:-}"
ASSUME_YES=0       # --yes            never prompt

usage() {
  cat <<'USAGE'
Usage: bash scripts/jarvis-setup.sh [options]

  --python PATH     interpreter to build the venv from (default: python3, else python)
  --skip-browser    skip the Playwright Chromium download (~150 MB)
  --skip-tests      skip the verification stage (pytest, ruff, wheel build)
  --skip-system     never install OS packages (tesseract, xdotool, ...); report only
  --model           also pull the default ollama model (~2 GB download)
  --web             also install the web UI extras (FastAPI, uvicorn, Gradio)
  --no-dev          skip the dev extras (pytest, pytest-asyncio, ruff)
  --yes             do not prompt before installing OS packages
  -h, --help        this text

Environment: PYTHON, JARVIS_SETUP_QUIET=1
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-browser) SKIP_BROWSER=1 ;;
    --skip-tests)   SKIP_TESTS=1 ;;
    --skip-system)  SKIP_SYSTEM=1 ;;
    --model)        WITH_MODEL=1 ;;
    --web)          WITH_WEB=1 ;;
    --no-dev)       NO_DEV=1 ;;
    --yes|-y)       ASSUME_YES=1 ;;
    --python)       PYTHON="${2:?--python needs a path}"; shift ;;
    -h|--help)      usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ── output helpers (bash 3.2 compatible — macOS ships 3.2) ───────────────────
if [ -t 1 ] && [ -z "${JARVIS_SETUP_QUIET:-}" ]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_AMBER=$'\033[33m'; C_OFF=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_AMBER=""; C_OFF=""
fi

step() { printf '\n%s── %s %s\n' "$C_BOLD" "$1" "$C_OFF"; }
info() { printf '   %s\n' "$1"; }
dim()  { printf '   %s%s%s\n' "$C_DIM" "$1" "$C_OFF"; }
ok()   { printf '   %s✓%s %s\n' "$C_GREEN" "$C_OFF" "$1"; }
warn() { printf '   %s!%s %s\n' "$C_AMBER" "$C_OFF" "$1"; WARNINGS="${WARNINGS:-}$1
"; }
die()  { printf '\n%s✗ %s%s\n' "$C_RED" "$1" "$C_OFF" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

printf '%s── JARVIS setup%s  %s\n' "$C_BOLD" "$C_OFF" "$ROOT"

# ── 1. interpreter ───────────────────────────────────────────────────────────
step "1/10  Python interpreter"
if [ -z "$PYTHON" ]; then
  if have python3; then PYTHON=python3
  elif have python; then PYTHON=python
  else die "no python3 on PATH — install Python 3.10+ first"
  fi
fi
have "$PYTHON" || die "'$PYTHON' is not executable"

PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Python 3.10+ required, found $PY_VERSION ($PYTHON)"
ok "Python $PY_VERSION ($PYTHON)"

# ── 2. virtualenv ────────────────────────────────────────────────────────────
step "2/10  Virtual environment"
if [ -d .venv ]; then
  ok ".venv already exists — reusing it"
else
  "$PYTHON" -m venv .venv || die "could not create .venv (is python3-venv installed?)"
  ok "created .venv"
fi
# shellcheck disable=SC1091
. ./.venv/bin/activate
PY=".venv/bin/python"
"$PY" -c 'import sys; print(sys.executable)' >/dev/null || die "the venv python does not run"

# ── 3. core dependencies ─────────────────────────────────────────────────────
step "3/10  Core dependencies"
"$PY" -m pip install --quiet --upgrade pip
# Installed as the project itself, so `jarvis` lands on PATH inside the venv.
"$PY" -m pip install --quiet -e . || die "core install failed — check your network / index"
ok "$("$PY" -m jarvis --version 2>/dev/null || echo 'JARVIS installed')"

# ── 4. focus-area extras ─────────────────────────────────────────────────────
step "4/10  Focus-area extras (screen, browser, reading)"
if "$PY" -m pip install --quiet \
     mss Pillow pytesseract pyautogui \
     playwright selenium websockets \
     trafilatura beautifulsoup4 tiktoken; then
  ok "screen + browser + reading extras installed"
else
  warn "some focus-area extras failed — JARVIS degrades gracefully without them"
fi
if [ "$WITH_WEB" = 1 ]; then
  if [ -f requirements-web.txt ] && "$PY" -m pip install --quiet -r requirements-web.txt; then
    ok "web UI extras installed"
  else
    warn "web UI extras failed to install"
  fi
fi
if [ "$NO_DEV" = 0 ]; then
  if "$PY" -m pip install --quiet "pytest>=8.0" "pytest-asyncio>=0.23" "ruff>=0.4" build; then
    ok "dev extras installed (pytest, pytest-asyncio, ruff, build)"
  else
    warn "dev extras failed — the verification stage will be skipped"
    SKIP_TESTS=1
  fi
fi

# ── 5. browser engine ────────────────────────────────────────────────────────
step "5/10  Browser engine"
if [ "$SKIP_BROWSER" = 1 ]; then
  dim "skipped (--skip-browser)"
elif ! "$PY" -c 'import playwright' 2>/dev/null; then
  warn "playwright is not installed — skipping the Chromium download"
else
  info "downloading the Chromium build Playwright drives (~150 MB, once)"
  if "$PY" -m playwright install chromium; then
    ok "Chromium ready"
  else
    warn "Chromium download failed — install Chrome/Chromium manually, or re-run later"
  fi
fi

# ── 6. OS-level tools ────────────────────────────────────────────────────────
# These cannot be pip-installed: OCR and window control are system binaries.
step "6/10  OS-level tools (OCR, window control)"

PKGMGR=""
if have apt-get; then PKGMGR=apt
elif have dnf; then PKGMGR=dnf
elif have yum; then PKGMGR=yum
elif have pacman; then PKGMGR=pacman
elif have zypper; then PKGMGR=zypper
elif have brew; then PKGMGR=brew
fi

install_system() {
  # install_system <package...> — best effort, never fatal.
  [ "$SKIP_SYSTEM" = 1 ] && return 1
  [ -n "$PKGMGR" ] || return 1
  if [ "$ASSUME_YES" = 0 ]; then
    printf '   %sinstall %s via %s? [y/N] %s' "$C_AMBER" "$*" "$PKGMGR" "$C_OFF"
    IFS= read -r answer || answer=""
    case "$answer" in y|Y|yes|YES) ;; *) info "declined"; return 1 ;; esac
  fi
  case "$PKGMGR" in
    apt)
      if [ "$(id -u)" = 0 ]; then
        apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
      elif have sudo; then
        sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
      else
        warn "need root or sudo for apt-get"; return 1
      fi ;;
    dnf|yum)
      if [ "$(id -u)" = 0 ]; then "$PKGMGR" install -y -q "$@"
      elif have sudo; then sudo "$PKGMGR" install -y -q "$@"
      else warn "need root or sudo for $PKGMGR"; return 1; fi ;;
    pacman)
      if [ "$(id -u)" = 0 ]; then pacman -S --needed --noconfirm "$@"
      elif have sudo; then sudo pacman -S --needed --noconfirm "$@"
      else warn "need root or sudo for pacman"; return 1; fi ;;
    zypper)
      if [ "$(id -u)" = 0 ]; then zypper --noninteractive install "$@"
      elif have sudo; then sudo zypper --noninteractive install "$@"
      else warn "need root or sudo for zypper"; return 1; fi ;;
    brew) brew install "$@" ;;
  esac
}

MISSING=""
for tool in tesseract xdotool wmctrl scrot; do
  have "$tool" || MISSING="$MISSING $tool"
done

if [ -z "$MISSING" ]; then
  ok "tesseract, xdotool, wmctrl and scrot are all present"
else
  warn "missing:$MISSING"
  case "$(uname -s)" in
    Darwin)
      info "macOS:  brew install tesseract"
      info "        xdotool / wmctrl / scrot are X11 tools — not needed on macOS" ;;
    *)
      case "$PKGMGR" in
        apt)    info "Debian/Ubuntu: sudo apt-get install -y tesseract-ocr xdotool wmctrl scrot" ;;
        dnf)    info "Fedora:        sudo dnf install -y tesseract xdotool wmctrl scrot" ;;
        yum)    info "RHEL/CentOS:   sudo yum install -y tesseract xdotool wmctrl scrot" ;;
        pacman) info "Arch:          sudo pacman -S --needed tesseract xdotool wmctrl scrot" ;;
        zypper) info "openSUSE:      sudo zypper install tesseract xdotool wmctrl scrot" ;;
        *)      info "install tesseract-ocr, xdotool, wmctrl and scrot from your package manager" ;;
      esac
      # Try it — a failure here is a warning, not a broken install.
      case "$PKGMGR" in
        apt)    install_system tesseract-ocr xdotool wmctrl scrot || true ;;
        dnf|yum) install_system tesseract xdotool wmctrl scrot || true ;;
        pacman) install_system tesseract xdotool wmctrl scrot || true ;;
        zypper) install_system tesseract xdotool wmctrl scrot || true ;;
        *) : ;;
      esac
      ;;
  esac
fi

# Headless box? A virtual display makes the screen tools work anyway.
if [ "$(uname -s)" = "Linux" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  warn "no DISPLAY — the screen tools need one. Run: bash scripts/start-xvfb.sh"
fi

# ── 7. local model (optional) ────────────────────────────────────────────────
step "7/10  Local model (optional)"
if have ollama; then
  ok "ollama found: $(command -v ollama)"
  if [ "$WITH_MODEL" = 1 ]; then
    info "pulling hermes3:3b (~2 GB, once)"
    ollama pull hermes3:3b || warn "ollama pull failed — run it again later"
  else
    dim "not pulling a model (pass --model to pull hermes3:3b, ~2 GB)"
  fi
else
  warn "ollama not installed — JARVIS will fall back to a free cloud provider"
  info "install: curl -fsSL https://ollama.com/install.sh | sh   (or brew install ollama)"
fi

# ── 8. environment file ──────────────────────────────────────────────────────
step "8/10  Environment file"
if [ -f .env ]; then
  ok ".env already exists — leaving it alone"
elif [ -f .env.example ]; then
  cp .env.example .env
  ok "created .env from .env.example (add your free API keys there)"
else
  warn ".env.example is missing — nothing to copy"
fi

# ── 9. verify ────────────────────────────────────────────────────────────────
step "9/10  Verification"
if [ "$SKIP_TESTS" = 1 ]; then
  dim "skipped (--skip-tests)"
else
  if [ -d tests ]; then
    info "running the test suite"
    if "$PY" -m pytest tests/ -q; then ok "test suite passed"
    else warn "the test suite reported failures — see the output above"; fi
  else
    dim "no tests/ directory — skipping pytest"
  fi

  if "$PY" -c 'import ruff' 2>/dev/null || have ruff || [ -x .venv/bin/ruff ]; then
    info "running the lint gate (the same rules CI enforces)"
    if .venv/bin/ruff check --select F821,F822,F811,F823,F841,B023,B904,E713,E9 \
         --output-format concise .; then
      ok "lint gate clean"
    else
      warn "the lint gate found problems — see the output above"
    fi
  fi

  if "$PY" -c 'import build' 2>/dev/null; then
    info "building the wheel + sdist"
    if "$PY" -m build --outdir dist >/dev/null 2>&1; then
      ok "built: $(ls dist 2>/dev/null | tr '\n' ' ')"
    else
      warn "the wheel build failed — see the output above"
    fi
  fi
fi

# ── 10. doctor ───────────────────────────────────────────────────────────────
step "10/10  Capability report"
"$PY" -m jarvis doctor || warn "jarvis doctor exited non-zero"

# ── summary ──────────────────────────────────────────────────────────────────
printf '\n%s── Done%s\n' "$C_BOLD" "$C_OFF"
if [ -n "${WARNINGS:-}" ]; then
  printf '%sThings to look at:%s\n' "$C_AMBER" "$C_OFF"
  printf '%s' "$WARNINGS" | sed '/^$/d; s/^/   • /'
fi

cat <<'MSG'

Next steps:

   # or use a free cloud key instead of Ollama (add to .env):
   #   GROQ_API_KEY=...  GEMINI_API_KEY=...  OPENROUTER_API_KEY=...

Free API keys go in .env — every one is optional, and the chain always ends
in a stub so a reply is always produced.
MSG
