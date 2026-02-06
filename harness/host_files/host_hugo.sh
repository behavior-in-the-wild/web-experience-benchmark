#!/usr/bin/env bash
set -euo pipefail
PORT=${PORT:-8080}
REPO_DIR=${1:-.}
LOG=${2:-/tmp/host_hugo.log}
cd "$REPO_DIR"

for cfg in hugo.toml hugo.yaml hugo.yml config.toml config.yaml config.yml; do
  if [ -f "$cfg" ]; then
    echo "[hugo] Found $cfg -> running hugo server" | tee -a "$LOG"
    hugo server -p "$PORT" --bind 0.0.0.0 >>"$LOG" 2>&1 &
    echo $! > /tmp/host_hugo.pid
    exit 0
  fi
done

if [ -f public/index.html ]; then
  python3 -m http.server "$PORT" --directory public >>"$LOG" 2>&1 &
  echo $! > /tmp/host_hugo.pid
  exit 0
fi

if [ -f docs/index.html ]; then
  python3 -m http.server "$PORT" --directory docs >>"$LOG" 2>&1 &
  echo $! > /tmp/host_hugo.pid
  exit 0
fi

if [ -f index.html ]; then
  python3 -m http.server "$PORT" >>"$LOG" 2>&1 &
  echo $! > /tmp/host_hugo.pid
  exit 0
fi

echo "[hugo] No recognized entrypoint" | tee -a "$LOG"
exit 2
