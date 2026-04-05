#!/usr/bin/env bash
# =============================================================================
# 01_generate_suggestions.sh
# -----------------------------------------------------------------------------
# Clone a GitHub repo, deploy it, run CWV analysis, and write the structured
# suggestions JSON — WITHOUT applying any code optimizations.
#
# Single-entry mode: outputs the suggestions file path to stdout so it can be
# captured and passed to 02_apply_optimizations.sh.
#
# Batch mode (--all): iterates every row in --dataset, skipping repos that
# already have a dumps/ folder. Logs are written to a timestamped file.
#
# Usage:
#   ./01_generate_suggestions.sh [OPTIONS]
#
# Required (one of):
#   --github-url URL          GitHub repository URL
#   --dataset    PATH         Path to a local CSV or JSONL dataset file
#
# Optional:
#   --framework  NAME         Framework type (default: "Static HTML")
#                             Choices: Hexo, Jekyll, Hugo, "Static HTML",
#                                      Vue, React, Next, Flask, Pelican, Express, Quarto
#   --hf-index   N            Row index inside --dataset for single-entry mode (default: 0)
#   --all                     Process ALL rows in --dataset (batch mode)
#   --limit      N            Cap the number of entries processed in batch mode
#   --workers    N            Parallel workers in batch mode (default: 1)
#   --device     TYPE         mobile|desktop (default: mobile)
#   --cwv-model  MODEL        LLM model for CWV analysis (default: gpt-4.1)
#   --tunnel-provider NAME    Tunnel provider for PSI: bore|cloudflare|ngrok|auto (default: cloudflare)
#   --no-headless             Run browser visibly (headless by default)
#   --verbose                 Verbose logging
#   --help                    Show this message and exit
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Ensure ~/.cargo/bin is on PATH so bore (installed via cargo) is reachable
# ---------------------------------------------------------------------------
if [[ -d "$HOME/.cargo/bin" ]] && [[ ":$PATH:" != *":$HOME/.cargo/bin:"* ]]; then
  export PATH="$HOME/.cargo/bin:$PATH"
fi

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
GITHUB_URL=""
FRAMEWORK="Static HTML"
DATASET=""
HF_INDEX=0
ALL_ENTRIES=0
LIMIT_ARG=""
WORKERS_ARG=""
DEVICE="mobile"
CWV_MODEL="gemini-2.5-pro"
TUNNEL_PROVIDER="cloudflare"
HEADLESS_FLAG="--headless"
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
    --github-url)   shift; GITHUB_URL="$1" ;;
    --framework)    shift; FRAMEWORK="$1" ;;
    --dataset)      shift; DATASET="$1" ;;
    --hf-index)     shift; HF_INDEX="$1" ;;
    --all)          ALL_ENTRIES=1 ;;
    --limit)        shift; LIMIT_ARG="$1" ;;
    --workers)      shift; WORKERS_ARG="$1" ;;
    --device)            shift; DEVICE="$1" ;;
    --cwv-model)         shift; CWV_MODEL="$1" ;;
    --tunnel-provider)   shift; TUNNEL_PROVIDER="$1" ;;
    --no-headless)       HEADLESS_FLAG="--no-headless" ;;
    --verbose)      VERBOSE_FLAG="--verbose" ;;
    --help|-h)      usage ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Validate inputs
# ---------------------------------------------------------------------------
if [[ -z "$GITHUB_URL" && -z "$DATASET" ]]; then
  echo "ERROR: --github-url or --dataset is required"
  exit 1
fi

if [[ "$ALL_ENTRIES" -eq 1 && -z "$DATASET" ]]; then
  echo "ERROR: --all requires --dataset"
  exit 1
fi

# ---------------------------------------------------------------------------
# Shared run timestamp — used for both log file and suggestions subfolder
# ---------------------------------------------------------------------------
RUN_TS="$(date +%Y%m%d_%H%M%S)"

# ---------------------------------------------------------------------------
# Build base cwv-optimizer suggest command
# ---------------------------------------------------------------------------
CMD=(cwv-optimizer suggest)

if [[ -n "$GITHUB_URL" ]]; then
  CMD+=(--github-url "$GITHUB_URL" --framework "$FRAMEWORK")
elif [[ "$ALL_ENTRIES" -eq 1 ]]; then
  CMD+=(--dataset "$DATASET" --framework "$FRAMEWORK" --all)
  [[ -n "$LIMIT_ARG"   ]] && CMD+=(--limit   "$LIMIT_ARG")
  [[ -n "$WORKERS_ARG" ]] && CMD+=(--workers "$WORKERS_ARG")
  CMD+=(--batch-ts "$RUN_TS")
else
  CMD+=(--dataset "$DATASET" --hf-index "$HF_INDEX" --framework "$FRAMEWORK")
fi

CMD+=(
  --device           "$DEVICE"
  --cwv-model        "$CWV_MODEL"
  --tunnel-provider  "$TUNNEL_PROVIDER"
  "$HEADLESS_FLAG"
)
[[ -n "$VERBOSE_FLAG" ]] && CMD+=("$VERBOSE_FLAG")

# ---------------------------------------------------------------------------
# Batch mode — run in background, stream to log file
# ---------------------------------------------------------------------------
if [[ "$ALL_ENTRIES" -eq 1 ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  LOG_DIR="$SCRIPT_DIR/../out/suggest_logs"
  mkdir -p "$LOG_DIR"
  LOG_FILE="$LOG_DIR/suggest_batch_${RUN_TS}.log"

  echo "[01_generate_suggestions] Batch mode — running all entries from: $DATASET"
  echo "[01_generate_suggestions] Command: ${CMD[*]}"
  echo "[01_generate_suggestions] Log: $LOG_FILE"

  "${CMD[@]}" 2>&1 | tee "$LOG_FILE"
  exit ${PIPESTATUS[0]}
fi

# ---------------------------------------------------------------------------
# Single-entry mode — run synchronously, capture output
# ---------------------------------------------------------------------------
echo "[01_generate_suggestions] Running: ${CMD[*]}" >&2

OUTPUT=$("${CMD[@]}" 2>&1)
EXIT_CODE=$?

echo "$OUTPUT" >&2

if [[ $EXIT_CODE -ne 0 ]]; then
  echo "[01_generate_suggestions] ERROR: cwv-optimizer suggest exited with code $EXIT_CODE" >&2
  exit $EXIT_CODE
fi

# ---------------------------------------------------------------------------
# Extract and emit the suggestions file path
# ---------------------------------------------------------------------------
SUGGESTIONS_PATH=$(echo "$OUTPUT" | grep -oP '(?<=Suggestions JSON:\s{0,10})\S+' | tail -1)

if [[ -z "$SUGGESTIONS_PATH" ]]; then
  echo "[01_generate_suggestions] WARN: Could not parse suggestions path from output" >&2
  exit 1
fi

if [[ ! -f "$SUGGESTIONS_PATH" ]]; then
  echo "[01_generate_suggestions] ERROR: Suggestions file not found: $SUGGESTIONS_PATH" >&2
  exit 1
fi

echo "[01_generate_suggestions] Suggestions written to: $SUGGESTIONS_PATH" >&2
echo "$SUGGESTIONS_PATH"
