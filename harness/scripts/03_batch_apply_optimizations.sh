#!/usr/bin/env bash
# =============================================================================
# 03_batch_apply_optimizations.sh
# -----------------------------------------------------------------------------
# Stage 2 of the harness pipeline: apply CWV code optimizations for all
# suggestions produced by 01_generate_suggestions.sh, then generate git patches.
#
# Does NOT require a live URL — uses the cloned workspace in dumps/ directly
# (patches-only mode).
#
# Usage:
#   ./harness/scripts/03_batch_apply_optimizations.sh [OPTIONS]
#
# Required:
#   --suggestions-dir DIR   Path to the suggestions directory from stage 1
#                           (e.g. harness/out/suggestions/20260320_163830)
#
# Optional:
#   --dumps-dir DIR         Dumps directory (default: dumps)
#   --output-dir DIR        Output for patches (default: harness/out/patches/<ts>)
#   --agent NAME            Coding agent: claude|codex|opencode|aider (default: claude)
#   --model MODEL           LLM model string (used by opencode/aider; ignored for claude)
#   --workers N             Concurrent repos (default: 2)
#   --include SUBSTR        Only process repos whose slug contains SUBSTR
#   --force                 Re-run even if checkpoint exists
#   --dry-run               Show plan without executing
#   --help                  Show this message and exit
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SUGGESTIONS_DIR=""
DUMPS_DIR="dumps"
OUTPUT_DIR=""
AGENT="opencode"
MODEL="gpt-5.1-codex"
WORKERS=2
INCLUDE=""
FORCE_FLAG=""
DRY_RUN_FLAG=""

usage() {
  sed -n '/^# Usage/,/^# ====/p' "$0" | grep '^#' | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --suggestions-dir) shift; SUGGESTIONS_DIR="$1" ;;
    --dumps-dir)       shift; DUMPS_DIR="$1" ;;
    --output-dir)      shift; OUTPUT_DIR="$1" ;;
    --agent)           shift; AGENT="$1" ;;
    --model)           shift; MODEL="$1" ;;
    --workers)         shift; WORKERS="$1" ;;
    --include)         shift; INCLUDE="$1" ;;
    --force)           FORCE_FLAG="--force" ;;
    --dry-run)         DRY_RUN_FLAG="--dry-run" ;;
    --help|-h)         usage ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

if [[ -z "$SUGGESTIONS_DIR" ]]; then
  echo "ERROR: --suggestions-dir is required"
  echo "Example: $0 --suggestions-dir harness/out/suggestions/20260320_163830"
  exit 1
fi

# Activate venv
VENV="$PROJECT_ROOT/.venv"
if [[ -f "$VENV/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$VENV/bin/activate"
fi

export PATH="$HOME/.cargo/bin:$PATH"

cd "$PROJECT_ROOT"

CMD=(
  python3 harness/scripts/03_batch_apply_optimizations.py
  --suggestions-dir "$SUGGESTIONS_DIR"
  --dumps-dir       "$DUMPS_DIR"
  --agent           "$AGENT"
  --parallel        "$WORKERS"
)

[[ -n "$OUTPUT_DIR"    ]] && CMD+=(--output-dir "$OUTPUT_DIR")
[[ -n "$MODEL"         ]] && CMD+=(--model "$MODEL")
[[ -n "$INCLUDE"       ]] && CMD+=(--include "$INCLUDE")
[[ -n "$FORCE_FLAG"    ]] && CMD+=("$FORCE_FLAG")
[[ -n "$DRY_RUN_FLAG"  ]] && CMD+=("$DRY_RUN_FLAG")

echo "[03_batch_apply] Running: ${CMD[*]}" >&2
"${CMD[@]}"
