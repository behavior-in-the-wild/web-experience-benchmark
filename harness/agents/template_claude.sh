#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$1"
TASK_SPEC="$2"
LOG="$3"

cd "$REPO_DIR"

echo "[agent_claude] Starting Claude Code agent" > "$LOG"

# Collect relevant files (keep context bounded)
FILES=$(find . -type f \( -name "*.html" -o -name "*.css" -o -name "*.js" \) | head -n 50)

PROMPT_FILE="$(mktemp)"

{
  echo "You are an expert web performance engineer."
  echo
  echo "Task:"
  cat "$TASK_SPEC"
  echo
  echo "Repository files:"
  for f in $FILES; do
    echo
    echo "===== FILE: $f ====="
    sed 's/^/| /' "$f"
  done
} > "$PROMPT_FILE"

# Claude Code invocation
# Assumes `claude` CLI is configured
claude code \
  --apply \
  --input "$PROMPT_FILE" \
  >> "$LOG" 2>&1 || {
    echo "[agent_claude] Claude failed" >> "$LOG"
    exit 1
  }

rm -f "$PROMPT_FILE"

echo "[agent_claude] Completed successfully" >> "$LOG"
exit 0
