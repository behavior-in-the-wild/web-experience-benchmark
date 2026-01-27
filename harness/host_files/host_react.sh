#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_react.log}
cd "$REPO_DIR"

if [ -f package.json ]; then
  echo "[react] package.json -> npm install and npm start" | tee -a "$LOG"
  npm install --silent || true
  export PORT="$PORT"
  npm start >>"$LOG" 2>&1 &
  echo $! > /tmp/host_react.pid
  exit 0
fi

for vf in vite.config.ts vite.config.js; do
  if [ -f "$vf" ]; then
    echo "[react] Vite detected -> npm run dev" | tee -a "$LOG"
    npm install --silent || true
    npm run dev -- --host 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1 &
    echo $! > /tmp/host_react.pid
    exit 0
  fi
done

if [ -f dist/index.html ]; then
  python3 -m http.server "$PORT" --directory dist >>"$LOG" 2>&1 &
  echo $! > /tmp/host_react.pid
  exit 0
fi

if [ -f build/index.html ]; then
  python3 -m http.server "$PORT" --directory build >>"$LOG" 2>&1 &
  echo $! > /tmp/host_react.pid
  exit 0
fi

if [ -f index.html ]; then
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_react.pid
  exit 0
fi

echo "[react] No recognized entrypoint" | tee -a "$LOG"
exit 2
