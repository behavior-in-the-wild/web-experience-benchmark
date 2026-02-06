#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Generic agent template for web-experience-benchmark harness
# ------------------------------------------------------------
# Usage (from harness root):
#   ./agents/agent_template.sh REPO_DIR TASK_SPEC LOG_FILE
#
# Arguments:
#   REPO_DIR  - path to the repo to operate on
#   TASK_SPEC - path to a text file describing the optimization task
#   LOG_FILE  - path to append structured logs for this agent run
#
# This script is intentionally minimal. Replace the "AGENT IMPL"
# section with calls to your own tools / LLM / scripts.
# ============================================================

REPO_DIR="${1:-}"
TASK_SPEC="${2:-}"
LOG="${3:-}"

if [[ -z "$REPO_DIR" || -z "$TASK_SPEC" || -z "$LOG" ]]; then
  echo "Usage: $0 REPO_DIR TASK_SPEC LOG_FILE" >&2
  exit 1
fi

mkdir -p "$(dirname "$LOG")"

{
  echo "[agent_template] Starting generic agent"
  echo "[agent_template] REPO_DIR: $REPO_DIR"
  echo "[agent_template] TASK_SPEC: $TASK_SPEC"
  date +"[agent_template] Start time: %Y-%m-%dT%H:%M:%S%z"
} > "$LOG"

cd "$REPO_DIR"

# ------------------------------------------------------------
# AGENT IMPLEMENTATION GOES HERE
# ------------------------------------------------------------
# Examples of what you might do here:
# - Collect a subset of files to operate on
# - Build a prompt that includes TASK_SPEC + file contents
# - Call an external CLI (OpenAI, Claude, etc.)
# - Apply patches to the repo
#
# Below is a minimal placeholder implementation that:
# - Logs the requested task
# - Does NOT modify any files
# Replace this block with real logic.
# ------------------------------------------------------------

{
  echo
  echo "[agent_template] Task description:"
  echo "---------------- TASK SPEC BEGIN ----------------"
  cat "$TASK_SPEC" || echo "[agent_template] WARNING: could not read TASK_SPEC"
  echo "----------------- TASK SPEC END -----------------"
  echo
  echo "[agent_template] No-op implementation. Replace this section with real logic."
} >> "$LOG"

# ------------------------------------------------------------
# End of agent implementation
# ------------------------------------------------------------

{
  date +"[agent_template] End time: %Y-%m-%dT%H:%M:%S%z"
  echo "[agent_template] Completed successfully (no changes made by template)."
} >> "$LOG"

exit 0

