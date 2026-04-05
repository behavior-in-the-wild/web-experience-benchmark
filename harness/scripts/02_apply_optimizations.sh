#!/usr/bin/env bash
# =============================================================================
# 02_apply_optimizations.sh
# -----------------------------------------------------------------------------
# Apply CWV code optimizations from a pre-generated suggestions JSON file
# to an already-deployed workspace, then run visual regression and performance
# testing.
#
# This is the second half of the two-stage workflow:
#   Stage 1 — 01_generate_suggestions.sh  (clone + deploy + CWV analysis)
#   Stage 2 — 02_apply_optimizations.sh   (apply changes + test)
#
# Usage:
#   ./02_apply_optimizations.sh [OPTIONS]
#
# Required:
#   --parsed-suggestions PATH   Path to the suggestions JSON from stage 1
#   --url                URL    URL of the deployed site (e.g. http://localhost:4000/)
#   --workspace-dir      DIR    Path to the cloned/deployed repo workspace
#
# Optional:
#   --device      TYPE    mobile|desktop (default: mobile)
#   --model       MODEL   LLM model for code changes (default: azure/gpt-5)
#   --agent       NAME    Coding agent: claude|aider|codex|opencode (default: claude)
#   --num-runs    N       Number of perf-test runs (default: 3)
#   --no-headless         Run browser visibly (headless by default)
#   --stream              Stream output from the pipeline
#   --checkpoint          Enable SQLite checkpointing
#   --verbose             Verbose logging
#   --help                Show this message and exit
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PARSED_SUGGESTIONS=""
URL=""
WORKSPACE_DIR=""
DEVICE="mobile"
MODEL="azure/gpt-5"
AGENT="claude"
NUM_RUNS=3
HEADLESS_FLAG="--headless"
STREAM_FLAG=""
CHECKPOINT_FLAG=""
VERBOSE_FLAG=""

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
  sed -n '/^# Usage/,/^# ====/p' "$0" | grep '^#' | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --parsed-suggestions) shift; PARSED_SUGGESTIONS="$1" ;;
    --url)                shift; URL="$1" ;;
    --workspace-dir)      shift; WORKSPACE_DIR="$1" ;;
    --device)             shift; DEVICE="$1" ;;
    --model)              shift; MODEL="$1" ;;
    --agent)              shift; AGENT="$1" ;;
    --num-runs)           shift; NUM_RUNS="$1" ;;
    --no-headless)        HEADLESS_FLAG="--no-headless" ;;
    --stream)             STREAM_FLAG="--stream" ;;
    --checkpoint)         CHECKPOINT_FLAG="--checkpoint" ;;
    --verbose)            VERBOSE_FLAG="--verbose" ;;
    --help|-h)            usage ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Validate inputs
# ---------------------------------------------------------------------------
if [[ -z "$PARSED_SUGGESTIONS" ]]; then
  echo "ERROR: --parsed-suggestions is required"
  exit 1
fi
if [[ -z "$URL" ]]; then
  echo "ERROR: --url is required"
  exit 1
fi
if [[ -z "$WORKSPACE_DIR" ]]; then
  echo "ERROR: --workspace-dir is required"
  exit 1
fi
if [[ ! -f "$PARSED_SUGGESTIONS" ]]; then
  echo "ERROR: Suggestions file not found: $PARSED_SUGGESTIONS"
  exit 1
fi
if [[ ! -d "$WORKSPACE_DIR" ]]; then
  echo "ERROR: Workspace directory not found: $WORKSPACE_DIR"
  exit 1
fi

# ---------------------------------------------------------------------------
# Build cwv-optimizer optimize command
# ---------------------------------------------------------------------------
CMD=(
  cwv-optimizer optimize
  --parsed-suggestions "$PARSED_SUGGESTIONS"
  --url                "$URL"
  --workspace-dir      "$WORKSPACE_DIR"
  --device             "$DEVICE"
  --model              "$MODEL"
  --coding-agent-provider "$AGENT"
  --num-runs           "$NUM_RUNS"
  "$HEADLESS_FLAG"
)
[[ -n "$STREAM_FLAG"     ]] && CMD+=("$STREAM_FLAG")
[[ -n "$CHECKPOINT_FLAG" ]] && CMD+=("$CHECKPOINT_FLAG")
[[ -n "$VERBOSE_FLAG"    ]] && CMD+=("$VERBOSE_FLAG")

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
echo "[02_apply_optimizations] Running: ${CMD[*]}" >&2

"${CMD[@]}"
EXIT_CODE=$?

if [[ $EXIT_CODE -ne 0 ]]; then
  echo "[02_apply_optimizations] ERROR: cwv-optimizer optimize exited with code $EXIT_CODE" >&2
  exit $EXIT_CODE
fi

echo "[02_apply_optimizations] Done." >&2
