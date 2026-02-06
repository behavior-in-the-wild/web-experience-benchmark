#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_vue.log}
cd "$REPO_DIR"

if [ -f package.json ]; then
  echo "[vue] package.json -> npm install and npm run serve" | tee -a "$LOG"
  npm install --silent || true
  export PORT="$PORT"
  npm run serve >>"$LOG" 2>&1 &
  echo $! > /tmp/host_vue.pid
  exit 0
fi

for vf in vite.config.ts vite.config.js; do
  if [ -f "$vf" ]; then
    npm install --silent || true
    npm run dev -- --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1 &
    echo $! > /tmp/host_vue.pid
    exit 0
  fi
done

if [ -f dist/index.html ]; then
  python3 -m http.server "$PORT" --directory dist >>"$LOG" 2>&1 &
  echo $! > /tmp/host_vue.pid
  exit 0
fi

if [ -f index.html ]; then
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_vue.pid
  exit 0
fi

echo "[vue] No recognized entrypoint" | tee -a "$LOG"
exit 2
