#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_flask.log}
cd "$REPO_DIR"

if [ -f app.py ]; then
  echo "[flask] app.py found -> pip install and flask run" | tee -a "$LOG"
  pip install -r requirements.txt || true
  export FLASK_APP=app.py
  export FLASK_ENV=development
  export FLASK_RUN_PORT="$PORT"
  flask run --host=0.0.0.0 >>"$LOG" 2>&1 &
  echo $! > /tmp/host_flask.pid
  exit 0
fi

if [ -f wsgi.py ]; then
  pip install -r requirements.txt || true
  export FLASK_APP=wsgi.py
  export FLASK_ENV=development
  export FLASK_RUN_PORT="$PORT"
  flask run --host=0.0.0.0 >>"$LOG" 2>&1 &
  echo $! > /tmp/host_flask.pid
  exit 0
fi

if [ -f static/index.html ]; then
  python3 -m http.server "$PORT" --directory static >>"$LOG" 2>&1 &
  echo $! > /tmp/host_flask.pid
  exit 0
fi

echo "[flask] No recognized entrypoint" | tee -a "$LOG"
exit 2
