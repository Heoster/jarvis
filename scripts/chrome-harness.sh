#!/usr/bin/env bash
# Launch Chrome the way JARVIS's harness expects it:
#   a real window + a remote debugging port, so both DOM control and
#   screen control work against the same browser.
set -euo pipefail

PORT="${1:-9222}"
PROFILE="${JARVIS_PROFILE:-$HOME/.jarvis-chrome-profile}"
URL="${2:-about:blank}"

find_chrome() {
  for candidate in \
      "$JARVIS_CHROME_PATH" \
      "$(command -v google-chrome || true)" \
      "$(command -v google-chrome-stable || true)" \
      "$(command -v chromium || true)" \
      "$(command -v chromium-browser || true)" \
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
      "/c/Program Files/Google/Chrome/Application/chrome.exe"; do
    if [ -n "${candidate:-}" ] && [ -x "$candidate" ]; then echo "$candidate"; return 0; fi
  done
  return 1
}

CHROME="$(find_chrome || true)"
if [ -z "${CHROME:-}" ]; then
  echo "❌ Chrome/Chromium not found. Install Chrome, or run:" >&2
  echo "   pip install playwright && playwright install chromium" >&2
  exit 1
fi

echo "🚀 Chrome: $CHROME"
echo "   debug port : $PORT"
echo "   profile    : $PROFILE"
echo "   opening    : $URL"

"$CHROME" \
  --remote-debugging-port="$PORT" \
  --remote-allow-origins='*' \
  --user-data-dir="$PROFILE" \
  --no-first-run --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --disable-features=TranslateUI \
  --window-size=1440,900 \
  "$URL" &

sleep 2
if command -v curl >/dev/null; then
  echo -n "   CDP check  : "
  curl -s "http://127.0.0.1:$PORT/json/version" | head -c 200 || echo "(no response yet)"
  echo
fi
echo "✅ Chrome is running — JARVIS can attach with: python -m jarvis browser"
