#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_quarto.log}
cd "$REPO_DIR"

if [ -f _quarto.yml ] || [ -f _quarto.yaml ]; then
  echo "[quarto] Rendering site -> quarto render" | tee -a "$LOG"
  quarto render || true
  python3 -m http.server "$PORT" --directory _site >>"$LOG" 2>&1 &
  echo $! > /tmp/host_quarto.pid
  exit 0
fi

if [ -f _site/index.html ]; then
  python3 -m http.server "$PORT" --directory _site >>"$LOG" 2>&1 &
  echo $! > /tmp/host_quarto.pid
  exit 0
fi

if [ -f docs/index.html ]; then
  python3 -m http.server "$PORT" --directory docs >>"$LOG" 2>&1 &
  echo $! > /tmp/host_quarto.pid
  exit 0
fi

if [ -f index.html ]; then
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_quarto.pid
  exit 0
fi

echo "[quarto] No recognized entrypoint" | tee -a "$LOG"
exit 2
