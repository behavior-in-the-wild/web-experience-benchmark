#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-4000}"
REPO_DIR="${1:-.}"
LOG="${2:-/tmp/host_static_html.log}"

cd "$REPO_DIR"

if [[ -f index.html ]]; then
  if [[ "${STATIC_HTTP2:-0}" == "1" ]]; then
    echo "[static] Serving static HTML on port $PORT with HTTP/2" | tee -a "$LOG"
    # Prefer .cjs (CommonJS-explicit) to avoid "type":"module" in ancestor package.json
    _SERVER="$REPO_DIR/http2_server.cjs"
    [[ -f "$_SERVER" ]] || _SERVER="$REPO_DIR/http2_server.js"
    exec node "$_SERVER" "$REPO_DIR" \
      >>"$LOG" 2>&1
  fi
  echo "[static] Serving static HTML on port $PORT with python http.server" | tee -a "$LOG"
  exec python3 -m http.server "$PORT" --bind 0.0.0.0 \
    >>"$LOG" 2>&1
fi

echo "[static] ERROR: No index.html found in repo root" | tee -a "$LOG"
exit 2
