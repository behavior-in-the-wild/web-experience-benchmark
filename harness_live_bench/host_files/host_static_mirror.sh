#!/usr/bin/env bash
# host_static_mirror.sh — serve a mirrored page directory via Python HTTP server
# Usage: PORT=4000 bash host_static_mirror.sh <page_dir> [log_file]
set -euo pipefail

PAGE_DIR="$1"
LOG_FILE="${2:-/dev/null}"
PORT="${PORT:-4000}"

[[ -d "$PAGE_DIR" ]] || { echo "[host] ERROR: directory not found: $PAGE_DIR"; exit 1; }
cd "$PAGE_DIR"

echo "[host] Serving $PAGE_DIR on port $PORT" >> "$LOG_FILE"
exec python3 -m http.server "$PORT" >> "$LOG_FILE" 2>&1
