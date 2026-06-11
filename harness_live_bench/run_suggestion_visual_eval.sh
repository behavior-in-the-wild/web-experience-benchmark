#!/usr/bin/env bash
# Visual + CWV eval for existing live-bench suggestion patches.
# Runs measure_only mode (no agent) across all 3 models for a given run dir.
#
# Usage:
#   bash harness_live_bench/run_suggestion_visual_eval.sh
#   bash harness_live_bench/run_suggestion_visual_eval.sh --patch-results-dir harness_live_bench/out/suggestions_eval/20260604_034545
#   bash harness_live_bench/run_suggestion_visual_eval.sh --models gemma-4-31b-it,minimax-m2.7
set -euo pipefail

HARNESS_LIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS_LIVE/.." && pwd)"

MODELS=(gemma-4-31b-it minimax-m2.7 qwen3.5-27b)
PARALLEL="${PARALLEL:-16}"
JSONL="${JSONL:-$HARNESS_LIVE/SAMPLE/live_filtered_top3.jsonl}"
MIRRORS_ROOT="${MIRRORS_ROOT:-$SCRIPT_DIR/live_assets_eds}"
PATCH_RESULTS_DIR=""
MODE="${MODE:-measure_only}"
models_from_flag=""

usage() {
  cat <<'EOF'
Usage: harness_live_bench/run_suggestion_visual_eval.sh [options]

Options:
  --patch-results-dir DIR  Root dir of a previous patch run (default: latest in out/suggestions_eval/)
  --mode MODE              measure_only | cwv_only | visual_only | both (default: measure_only)
  --models A,B,C           Comma-separated model subset
  --parallel N             Parallel jobs (default: 16)
  --jsonl PATH             Input JSONL (default: SAMPLE/live_filtered_top3.jsonl)
  --mirrors-root DIR       Mirror root (default: ../live_assets_eds)
  --help                   Show this message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --patch-results-dir) shift; PATCH_RESULTS_DIR="$1"; shift ;;
    --mode)              shift; MODE="$1"; shift ;;
    --models)            shift; models_from_flag="$1"; shift ;;
    --parallel)          shift; PARALLEL="$1"; shift ;;
    --jsonl)             shift; JSONL="$1"; shift ;;
    --mirrors-root)      shift; MIRRORS_ROOT="$1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -n "$models_from_flag" ]]; then
  IFS=',' read -r -a MODELS <<< "$models_from_flag"
fi

# Resolve patch results dir — default to latest timestamped run
if [[ -z "$PATCH_RESULTS_DIR" ]]; then
  PATCH_RESULTS_DIR="$(ls -d "$HARNESS_LIVE/out/suggestions_eval"/20* 2>/dev/null | sort | tail -1)"
  if [[ -z "$PATCH_RESULTS_DIR" ]]; then
    echo "[live-visual] ERROR: no patch results dir found in $HARNESS_LIVE/out/suggestions_eval/"
    exit 1
  fi
fi

[[ "$PATCH_RESULTS_DIR" = /* ]] || PATCH_RESULTS_DIR="$(cd "$PATCH_RESULTS_DIR" && pwd)"
[[ -d "$PATCH_RESULTS_DIR" ]] || { echo "[live-visual] ERROR: patch results dir not found: $PATCH_RESULTS_DIR"; exit 1; }

[[ "$MIRRORS_ROOT" = /* ]] || MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"

log() { echo "[live-visual] $(date '+%H:%M:%S') $*"; }

log "Patch results dir: $PATCH_RESULTS_DIR"
log "Models: ${MODELS[*]}"
log "JSONL: $JSONL"
log "MIRRORS_ROOT: $MIRRORS_ROOT"
log "Parallel: $PARALLEL"
echo ""

overall_status=0

for i in "${!MODELS[@]}"; do
  model="${MODELS[$i]}"
  eval_out_dir="$PATCH_RESULTS_DIR/$model"

  if [[ ! -d "$eval_out_dir/results" ]]; then
    log "ERROR: results dir not found: $eval_out_dir/results — skipping $model"
    overall_status=1
    continue
  fi

  total_dirs=$(ls "$eval_out_dir/results" 2>/dev/null | wc -l)
  done_count=$(find "$eval_out_dir/results" -name "visual.json" 2>/dev/null | wc -l)
  log "[$((i+1))/${#MODELS[@]}] $model — $total_dirs result dirs, $done_count visual.json already done"

  run_exit=0
  MODE="$MODE" \
  PARALLEL="$PARALLEL" \
  JSONL="$JSONL" \
  MIRRORS_ROOT="$MIRRORS_ROOT" \
  EVAL_OUT_DIR="$eval_out_dir" \
    bash "$HARNESS_LIVE/run_cwv_evals_suggestions_row.sh" --resume || run_exit=$?

  if [[ "$run_exit" -ne 0 ]]; then
    log "ERROR: visual eval failed for $model (exit $run_exit) — continuing"
    overall_status=$run_exit
  else
    done_now=$(find "$eval_out_dir/results" -name "visual.json" 2>/dev/null | wc -l)
    log "✓ $model complete — $done_now visual.json written"
  fi
  echo ""
done

if [[ "$overall_status" -ne 0 ]]; then
  log "COMPLETED WITH ERRORS (exit $overall_status)"
  exit "$overall_status"
fi
log "All models done. Results in: $PATCH_RESULTS_DIR"
