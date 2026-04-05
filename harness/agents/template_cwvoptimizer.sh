#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# CWV-Optimizer agent template for web-experience-benchmark
# ------------------------------------------------------------
# Runs the full cwv-optimizer framework pipeline (clone → deploy
# → analyze → optimize) and extracts the resulting patch.
#
# Arguments (standard harness interface):
#   $1  REPO_DIR   - path to the unzipped repo (harness-managed)
#   $2  TASK_SPEC  - path to the task specification file
#   $3  LOG_FILE   - path for agent logs
#   $4  PATCH_FILE - path to write the patch
#
# Required env vars (exported by evaluate.sh):
#   REPO_ID    - GitHub owner/repo (e.g. "user/repo")
#   FRAMEWORK  - lowercase framework name
# ============================================================

REPO_DIR="$1"
TASK_SPEC="$2"
LOG_FILE="$3"
PATCH_FILE="${4:-/dev/null}"

# evaluate.sh lowercases FRAMEWORK; cwv-optimizer needs title-case
_fw_lower="${FRAMEWORK:-unknown}"
case "$_fw_lower" in
  "static html"|"static_html") FRAMEWORK="Static HTML" ;;
  *)
    # Title-case: capitalize first letter
    FRAMEWORK="$(echo "$_fw_lower" | sed 's/\b\(.\)/\u\1/g')"
    ;;
esac
REPO_ID="${REPO_ID:-}"

if [[ -z "$REPO_ID" ]]; then
  echo "[cwv-optimizer] ERROR: REPO_ID env var is not set" | tee -a "$LOG_FILE"
  exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"
REPO_NAME="${REPO_ID##*/}"
# cwv-optimizer's clone_repo.py strips ".git" from the URL's last segment,
# which mangles names like "foo.github.io" → "foohub.io"
CWV_REPO_NAME="${REPO_NAME//.git/}"

{
  echo "[cwv-optimizer] Starting cwv-optimizer agent"
  echo "[cwv-optimizer] REPO_ID=$REPO_ID"
  echo "[cwv-optimizer] REPO_DIR=$REPO_DIR"
  echo "[cwv-optimizer] FRAMEWORK=$FRAMEWORK"
  date +"[cwv-optimizer] Start time: %Y-%m-%dT%H:%M:%S%z"
} > "$LOG_FILE"

# ============================================================
# Resolve cwv-optimizer project root (relative to this script)
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CWV_OPT_ROOT="$(cd "$SCRIPT_DIR/../../" && pwd)"

# Configurable coding agent and model
CODING_AGENT="${CODING_AGENT:-opencode}"
CODING_MODEL="${CODING_MODEL:-azure/gpt-5.1-codex}"

echo "[cwv-optimizer] Running: cwv-optimizer framework --github-url https://github.com/$REPO_ID --framework $FRAMEWORK --model $CODING_MODEL --coding-agent-provider $CODING_AGENT" >> "$LOG_FILE"

GITHUB_URL="https://github.com/${REPO_ID}"

set +e
PIPELINE_START=$(date +%s)
cwv-optimizer framework \
  --github-url "$GITHUB_URL" \
  --framework "$FRAMEWORK" \
  --num-runs 5 \
  --headless \
  --model "$CODING_MODEL" \
  --coding-agent-provider "$CODING_AGENT" \
  --cwv-model "gemini-2.5-pro" \
  --verbose \
  >> "$LOG_FILE" 2>&1
PIPELINE_EXIT=$?
PIPELINE_END=$(date +%s)
set -e

echo "[cwv-optimizer] Pipeline exit code=$PIPELINE_EXIT, duration=$((PIPELINE_END - PIPELINE_START))s" >> "$LOG_FILE"

if [[ "$PIPELINE_EXIT" -ne 0 ]]; then
  echo "[cwv-optimizer] WARN: Pipeline returned non-zero ($PIPELINE_EXIT), attempting to extract patch anyway" >> "$LOG_FILE"
fi

# ============================================================
# Extract patch from cwv-optimizer workspace
# ============================================================
# cwv-optimizer stores results in: dumps/{repo_name}_{timestamp}/codebase/
DUMPS_DIR="$CWV_OPT_ROOT/dumps"

if [[ ! -d "$DUMPS_DIR" ]]; then
  echo "[cwv-optimizer] ERROR: dumps directory not found at $DUMPS_DIR" >> "$LOG_FILE"
  touch "$PATCH_FILE"
  exit 0
fi

# Find the latest workspace for this repo (most recent by timestamp suffix)
LATEST_WORKSPACE=""
for d in "$DUMPS_DIR"/"${CWV_REPO_NAME}"_*/codebase; do
  [[ -d "$d" ]] && LATEST_WORKSPACE="$d"
done

if [[ -z "$LATEST_WORKSPACE" || ! -d "$LATEST_WORKSPACE" ]]; then
  echo "[cwv-optimizer] ERROR: No workspace found for $CWV_REPO_NAME in $DUMPS_DIR" >> "$LOG_FILE"
  touch "$PATCH_FILE"
  exit 0
fi
echo "[cwv-optimizer] Found workspace: $LATEST_WORKSPACE" >> "$LOG_FILE"

# ============================================================
# Copy suggestions JSON into harness results directory
# ============================================================
RUN_DIR="$(dirname "$LATEST_WORKSPACE")"
CWV_RESULTS_DIR="$RUN_DIR/results"

SUGGESTIONS_SRC=""
for candidate in \
  "$CWV_RESULTS_DIR/cwv_suggestions_mobile.json" \
  "$CWV_RESULTS_DIR/cwv_suggestions_desktop.json"
do
  if [[ -f "$candidate" ]]; then
    SUGGESTIONS_SRC="$candidate"
    break
  fi
done

if [[ -n "$SUGGESTIONS_SRC" ]]; then
  # Place suggestions JSON next to the harness log/patch for this run
  SUGGESTIONS_DEST="${LOG_FILE%_agent.log}_suggestions.json"
  cp "$SUGGESTIONS_SRC" "$SUGGESTIONS_DEST"
  echo "[cwv-optimizer] Copied suggestions to: $SUGGESTIONS_DEST" >> "$LOG_FILE"
else
  echo "[cwv-optimizer] WARN: No suggestions file found under $CWV_RESULTS_DIR" >> "$LOG_FILE"
fi

# Extract git diff from the cwv-optimizer workspace
if [[ -d "$LATEST_WORKSPACE/.git" ]]; then
  (
    cd "$LATEST_WORKSPACE"
    # Capture both staged and unstaged changes
    git add -A > /dev/null 2>&1 || true
    git diff --cached > "$PATCH_FILE" 2>/dev/null || true
  )
  PATCH_LINES=$(wc -l < "$PATCH_FILE" 2>/dev/null || echo 0)
  echo "[cwv-optimizer] Extracted patch: $PATCH_LINES lines" >> "$LOG_FILE"
else
  echo "[cwv-optimizer] WARN: No .git in workspace, cannot extract diff" >> "$LOG_FILE"
  touch "$PATCH_FILE"
fi

# ============================================================
# Apply patch to harness REPO_DIR
# ============================================================
if [[ -s "$PATCH_FILE" ]]; then
  echo "[cwv-optimizer] Applying patch to $REPO_DIR" >> "$LOG_FILE"
  (
    cd "$REPO_DIR"
    git apply --allow-empty "$PATCH_FILE" >> "$LOG_FILE" 2>&1 || {
      echo "[cwv-optimizer] WARN: git apply failed, trying with --3way" >> "$LOG_FILE"
      git apply --3way "$PATCH_FILE" >> "$LOG_FILE" 2>&1 || {
        echo "[cwv-optimizer] ERROR: Could not apply patch" >> "$LOG_FILE"
      }
    }
  )
else
  echo "[cwv-optimizer] No changes produced (empty patch)" >> "$LOG_FILE"
fi

# ============================================================
# Done
# ============================================================
{
  date +"[cwv-optimizer] End time: %Y-%m-%dT%H:%M:%S%z"
  echo "[cwv-optimizer] Completed"
} >> "$LOG_FILE"

exit 0
