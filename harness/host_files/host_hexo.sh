#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_hexo.log}
cd "$REPO_DIR"

if [ -f package.json ]; then
  echo "[hexo] Found package.json -> running npm install and hexo server" | tee -a "$LOG"
  npm install --silent || true
  npx hexo server -p "$PORT" --silent >>"$LOG" 2>&1 &
  echo $! > /tmp/host_hexo.pid
  exit 0
fi

if [ -f index.html ]; then
  echo "[hexo] Serving static index.html via python http.server" | tee -a "$LOG"
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_hexo.pid
  exit 0
fi

echo "[hexo] No recognized entrypoint found" | tee -a "$LOG"
exit 2
