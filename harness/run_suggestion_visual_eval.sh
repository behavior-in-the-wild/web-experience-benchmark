#!/usr/bin/env bash
# Visual-only regression eval for static_sugg_eval patches.
# Runs sequentially across all 3 models, writing visual.json into each result dir.
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"
DUMP_DIR="$SCRIPT_DIR/final_result_dumps/static_sugg_eval"

MODELS=(gemma-4-31b-it minimax-m2.7 qwen3.5-27b)
PARALLEL="${PARALLEL:-16}"
CSV="${CSV:-$HARNESS/SAMPLE/input_100.csv}"
MODE="${MODE:-measure_only}"

log() { echo "[sugg-visual] $(date '+%H:%M:%S') $*"; }

log "Starting eval for static_sugg_eval (MODE=$MODE)"
log "Models: ${MODELS[*]}"
log "Parallel: $PARALLEL"
log "CSV: $CSV"
echo ""

overall_status=0

for i in "${!MODELS[@]}"; do
  model="${MODELS[$i]}"
  eval_out_dir="$DUMP_DIR/$model"

  if [[ ! -d "$eval_out_dir/results" ]]; then
    log "ERROR: results dir not found: $eval_out_dir/results"
    overall_status=1
    continue
  fi

  log "[$((i+1))/${#MODELS[@]}] $model — $(ls "$eval_out_dir/results" | wc -l) result dirs"

  run_exit=0
  MODE="$MODE" \
  PARALLEL="$PARALLEL" \
  EVAL_OUT_DIR="$eval_out_dir" \
  CSV="$CSV" \
    bash "$HARNESS/run_cwv_evals_suggestions_row.sh" --resume || run_exit=$?

  if [[ "$run_exit" -ne 0 ]]; then
    log "ERROR: visual eval failed for $model (exit $run_exit) — continuing to next model"
    overall_status=$run_exit
  else
    done_count=$(find "$eval_out_dir/results" -name "visual.json" | wc -l)
    log "✓ $model complete — $done_count visual.json written"
  fi
  echo ""
done

if [[ "$overall_status" -ne 0 ]]; then
  log "COMPLETED WITH ERRORS (exit $overall_status)"
  exit "$overall_status"
fi

log "All 3 models done."
