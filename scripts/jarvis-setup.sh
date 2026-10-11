#!/usr/bin/env bash
# JARVIS one-shot setup — installs every free dependency and verifies the install.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "── JARVIS setup ─────────────────────────────────────────────"
echo "project: $ROOT"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null; then
  echo "❌ python3 not found. Install Python 3.10+ first." >&2
  exit 1
fi
echo "python : $($PYTHON --version)"

# 1. virtual environment -------------------------------------------------
if [ ! -d .venv ]; then
  echo "→ creating .venv"
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 2. core + focus-area dependencies --------------------------------------
echo "→ installing core requirements"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "→ installing focus-area extras (screen, browser, reading)"
pip install --quiet playwright selenium websockets \
                        mss Pillow pytesseract trafilatura beautifulsoup4 || \
  echo "  (some optional packages failed — JARVIS degrades gracefully)"

# 3. browser engines -----------------------------------------------------
if command -v playwright >/dev/null; then
  echo "→ downloading the Chromium build Playwright drives (~150 MB)"
  playwright install chromium || echo "  (playwright download failed — install Chrome manually)"
fi

# 4. OCR engine ----------------------------------------------------------
if ! command -v tesseract >/dev/null; then
  echo "→ tesseract not found. Install it for screen reading:"
  echo "    Debian/Ubuntu : sudo apt-get install -y tesseract-ocr xdotool wmctrl"
  echo "    macOS         : brew install tesseract"
  echo "    Windows       : winget install UB-Mannheim.TesseractOCR"
fi

# 5. .env ----------------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ created .env (add your free API keys there)"
fi

# 6. verify --------------------------------------------------------------
echo "→ verifying the install"
python -m jarvis doctor || true

cat <<'MSG'

✅ Setup finished. Next steps:

   source .venv/bin/activate
   ollama pull hermes3:3b        # local brain (unlimited, private)
   python -m jarvis              # start chatting

   # or use a free cloud key instead of Ollama (add to .env):
   #   GROQ_API_KEY=...  CEREBRAS_API_KEY=...  GEMINI_API_KEY=...

MSG
