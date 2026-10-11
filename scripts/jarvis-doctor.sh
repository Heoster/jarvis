#!/usr/bin/env bash
# Run JARVIS's capability report and save it to logs/doctor-<date>.txt
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
OUT="logs/doctor-$(date +%Y%m%d-%H%M%S).txt"
{ python -m jarvis doctor 2>&1 || true; } | tee "$OUT"
echo
echo "saved → $OUT"
