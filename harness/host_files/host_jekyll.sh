#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-4000}"
REPO_DIR="${1:-.}"
LOG="${2:-/tmp/host_jekyll.log}"

cd "$REPO_DIR"

echo "[jekyll] Starting server on port $PORT" | tee -a "$LOG"

if [[ -f Gemfile ]]; then
  echo "[jekyll] Gemfile present -> bundle install + serve" | tee -a "$LOG"
  bundle install
  exec bundle exec jekyll serve \
    --host 0.0.0.0 \
    --port "$PORT" \
    >>"$LOG" 2>&1
fi

if [[ -f _config.yml ]]; then
  echo "[jekyll] _config.yml present -> jekyll serve" | tee -a "$LOG"
  exec jekyll serve \
    --host 0.0.0.0 \
    --port "$PORT" \
    >>"$LOG" 2>&1
fi

if [[ -f index.html ]]; then
  echo "[jekyll] Serving static via python http.server" | tee -a "$LOG"
  exec python3 -m http.server "$PORT" \
    >>"$LOG" 2>&1
fi

echo "[jekyll] ERROR: No recognized entrypoint found" | tee -a "$LOG"
exit 2
