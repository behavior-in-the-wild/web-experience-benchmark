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

# Static HTML first (many Express-tagged GitHub Pages repos are actually static)
if [ -f index.html ]; then
  echo "[express] Serving static (index.html)" | tee -a "$LOG"
  exec python3 -m http.server "$PORT" >>"$LOG" 2>&1
fi

# Check common subdirs (gDrive, server, etc.) for Express entrypoints
for subdir in gDrive server app; do
  if [ -f "$subdir/server.js" ] && [ -f "$subdir/package.json" ]; then
    echo "[express] Found $subdir/server.js -> cd $subdir && npm install && node server.js" | tee -a "$LOG"
    (cd "$subdir" && npm install --silent && export PORT="$PORT" && node server.js >>"$LOG" 2>&1 &)
    sleep 1
    exit 0
  fi
  if [ -f "$subdir/app.js" ] && [ -f "$subdir/package.json" ]; then
    echo "[express] Found $subdir/app.js -> cd $subdir && npm install && node app.js" | tee -a "$LOG"
    (cd "$subdir" && npm install --silent && export PORT="$PORT" && node app.js >>"$LOG" 2>&1 &)
    sleep 1
    exit 0
  fi
  if [ -f "$subdir/package.json" ]; then
    echo "[express] Found $subdir/package.json -> cd $subdir && npm start" | tee -a "$LOG"
    (cd "$subdir" && npm install --silent && export PORT="$PORT" && npm run start >>"$LOG" 2>&1 &)
    sleep 1
    exit 0
  fi
done

echo "[express] No recognized entrypoint" | tee -a "$LOG"
exit 2
