#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-4000}"
REPO_DIR="${1:-.}"
LOG="${2:-/tmp/host_jekyll.log}"

cd "$REPO_DIR"

echo "[jekyll] Starting server on port $PORT" | tee -a "$LOG"

if [[ -f Gemfile ]]; then
  echo "[jekyll] Gemfile present -> bundle install + serve" | tee -a "$LOG"
  bundle install
  exec bundle exec jekyll serve \
    --host 0.0.0.0 \
    --port "$PORT" \
    >>"$LOG" 2>&1
fi

if [[ -f _config.yml ]]; then
  echo "[jekyll] _config.yml present -> jekyll serve" | tee -a "$LOG"
  theme="$(python3 - <<'PY'
from pathlib import Path
import re
cfg = Path("_config.yml")
if cfg.exists():
    m = re.search(r"(?m)^\s*(?:remote_)?theme:\s*['\"]?([^'\"\s#]+)", cfg.read_text(errors="ignore"))
    if m and "/" not in m.group(1):
        print(m.group(1))
PY
)"
  if [[ -n "$theme" ]]; then
    echo "[jekyll] installing theme gem $theme if needed" | tee -a "$LOG"
    gem install "$theme" --user-install --no-document >>"$LOG" 2>&1 || true
  fi
  exec jekyll serve \
    --host 0.0.0.0 \
    --port "$PORT" \
    >>"$LOG" 2>&1
fi

if [[ -f index.html ]]; then
  echo "[jekyll] Serving static via python http.server" | tee -a "$LOG"
  exec python3 -m http.server "$PORT" \
    >>"$LOG" 2>&1
fi

echo "[jekyll] ERROR: No recognized entrypoint found" | tee -a "$LOG"
exit 2
