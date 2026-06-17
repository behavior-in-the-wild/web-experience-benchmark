#!/usr/bin/env bash
# Serves a Playwright-mirrored static page directory on PORT.
# Usage: PORT=N bash host_static_mirror.sh <mirror_dir> [log_file]
set -euo pipefail

PORT="${PORT:-4000}"
REPO_DIR="${1:-.}"
LOG="${2:-/tmp/host_static_mirror.log}"

mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"

echo "[mirror] Serving static mirror on port $PORT from $REPO_DIR" | tee -a "$LOG"
exec python3 -m http.server "$PORT" --bind 0.0.0.0 >>"$LOG" 2>&1
