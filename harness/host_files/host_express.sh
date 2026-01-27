#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_express.log}
cd "$REPO_DIR"

if [ -f package.json ]; then
  echo "[express] package.json found -> npm install and try npm start" | tee -a "$LOG"
  npm install --silent || true
  export PORT="$PORT"
  if npm run start --silent >/dev/null 2>&1; then
    npm run start >>"$LOG" 2>&1 & echo $! > /tmp/host_express.pid; exit 0
  fi
fi

for f in server.js app.js index.js; do
  if [ -f "$f" ]; then
    echo "[express] Found $f -> npm install and node $f" | tee -a "$LOG"
    npm install --silent || true
    export PORT="$PORT"
    node "$f" >>"$LOG" 2>&1 &
    echo $! > /tmp/host_express.pid
    exit 0
  fi
done

if [ -f backend/package.json ]; then
  echo "[express] backend package.json -> cd backend && npm install && npm start" | tee -a "$LOG"
  (cd backend && npm install --silent || true && export PORT="$PORT" && npm run start >>"$LOG" 2>&1 &)
  echo $! > /tmp/host_express.pid
  exit 0
fi

echo "[express] No recognized entrypoint" | tee -a "$LOG"
exit 2
