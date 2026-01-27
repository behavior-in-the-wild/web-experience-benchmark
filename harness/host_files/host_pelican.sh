#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_pelican.log}
cd "$REPO_DIR"

if [ -f pelicanconf.py ]; then
  echo "[pelican] Building site -> pip install && pelican content" | tee -a "$LOG"
  pip install -r requirements.txt || true
  pelican content || true
  python3 -m http.server "$PORT" --directory output >>"$LOG" 2>&1 &
  echo $! > /tmp/host_pelican.pid
  exit 0
fi

if [ -f publishconf.py ]; then
  pip install -r requirements.txt || true
  pelican content -s publishconf.py || true
  python3 -m http.server "$PORT" --directory output >>"$LOG" 2>&1 &
  echo $! > /tmp/host_pelican.pid
  exit 0
fi

if [ -f output/index.html ]; then
  python3 -m http.server "$PORT" --directory output >>"$LOG" 2>&1 &
  echo $! > /tmp/host_pelican.pid
  exit 0
fi

if [ -f index.html ]; then
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_pelican.pid
  exit 0
fi

echo "[pelican] No recognized entrypoint" | tee -a "$LOG"
exit 2
