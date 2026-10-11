#!/usr/bin/env bash
# Headless Linux (server / WSL / CI): give JARVIS a virtual display so screen
# control, screenshots and Chrome windows work without a physical monitor.
set -euo pipefail

DISPLAY_NUM="${1:-99}"
GEO="${2:-1440x900x24}"

if ! command -v Xvfb >/dev/null; then
  echo "Installing Xvfb (needs sudo)…"
  sudo apt-get update -qq && sudo apt-get install -y xvfb x11-utils xdotool wmctrl scrot tesseract-ocr
fi

if pgrep -f "Xvfb :$DISPLAY_NUM" >/dev/null; then
  echo "✅ Xvfb already running on :$DISPLAY_NUM"
else
  Xvfb ":$DISPLAY_NUM" -screen 0 "$GEO" -ac +extension GLX +render -noreset &
  sleep 1
  echo "✅ Xvfb started on :$DISPLAY_NUM ($GEO)"
fi

cat <<MSG

Export these in your shell (or add to ~/.bashrc):

  export DISPLAY=:$DISPLAY_NUM

Then:
  python -m jarvis screen            # should now report a working backend
  python -m jarvis browser --launch  # Chrome renders into the virtual display

MSG
