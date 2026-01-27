#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_next.log}
cd "$REPO_DIR"

if [ -f package.json ]; then
  echo "[next] package.json -> npm install, build, start" | tee -a "$LOG"
  npm install --silent || true
  npm run build --silent || true
  PORT="$PORT" npm run start >>"$LOG" 2>&1 &
  echo $! > /tmp/host_next.pid
  exit 0
fi

if [ -f website/package.json ]; then
  (cd website && npm install --silent || true && npm run build --silent || true && PORT="$PORT" npm run start >>"$LOG" 2>&1 &)
  echo $! > /tmp/host_next.pid
  exit 0
fi

if [ -f web/package.json ]; then
  (cd web && npm install --silent || true && npm run build --silent || true && PORT="$PORT" npm run start >>"$LOG" 2>&1 &)
  echo $! > /tmp/host_next.pid
  exit 0
fi

if [ -f out/index.html ]; then
  python3 -m http.server "$PORT" --directory out >>"$LOG" 2>&1 &
  echo $! > /tmp/host_next.pid
  exit 0
fi

if [ -d _next/static ]; then
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_next.pid
  exit 0
fi

if [ -f index.html ]; then
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_next.pid
  exit 0
fi

echo "[next] No recognized entrypoint" | tee -a "$LOG"
exit 2
