#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$1"
TASK_SPEC="$2"
LOG="$3"

AIDER_MODEL="${AIDER_MODEL:-azure/gpt-4.1}"

mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"

echo "[agent_aider] Starting aider agent" > "$LOG"

FILES=$(find . -type f \
  \( -name "*.html" -o -name "*.css" -o -name "*.js" \) \
  ! -name "*.min.js" \
  ! -path "./talkmap/leaflet_dist/*" \
  -size -200k \
  | head -n 20)

if [[ -z "$FILES" ]]; then
  echo "[agent_aider] No relevant files found" >> "$LOG"
  exit 0
fi

PROMPT_FILE="$(mktemp)"

cat <<EOF > "$PROMPT_FILE"
You are an expert web performance engineer.

Rules:
- Do not change visible content
- Do not remove pages
- Do not add build systems
- Only edit existing files

Task:
$(cat "$TASK_SPEC")

Return modified files only.
EOF

aider \
  --yes \
  --no-auto-commits \
  --no-pretty \
  --map-tokens 0 \
  --model "$AIDER_MODEL" \
  --message "$(cat "$PROMPT_FILE")" \
  $FILES \
  >> "$LOG" 2>&1 || {
    echo "[agent_aider] aider failed (auth or context)" >> "$LOG"
    rm -f "$PROMPT_FILE"
    exit 0
  }

rm -f "$PROMPT_FILE"
echo "[agent_aider] Done" >> "$LOG"
exit 0
