#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-4000}"
REPO_DIR="${1:-.}"
LOG="${2:-/tmp/host_static_html.log}"

cd "$REPO_DIR"

if [[ -f index.html ]]; then
  echo "[static] Serving static HTML on port $PORT" | tee -a "$LOG"
  exec npx http-server -p "$PORT" -c-1 \
    >>"$LOG" 2>&1
fi

echo "[static] ERROR: No index.html found in repo root" | tee -a "$LOG"
exit 2
