#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$1"
TASK_SPEC="$2"
LOG="${3:-/dev/null}"

mkdir -p "$(dirname "$LOG")"
echo "[agent_null] No-op baseline agent — no files modified" > "$LOG"

exit 0
