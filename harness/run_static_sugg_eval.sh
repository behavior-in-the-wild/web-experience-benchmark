#!/usr/bin/env bash
# Run regression + CWV measurement for final_result_dumps/static_sugg_eval patches.
# This wrapper matches the static_sugg_eval dump layout:
#   final_result_dumps/static_sugg_eval/<model>/results/<ID>_<owner>/patches/suggestion_N_run*.patch
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HARNESS/.." && pwd)"
DUMP_DIR="${DUMP_DIR:-$ROOT/final_result_dumps/static_sugg_eval}"
CSV="${CSV:-$HARNESS/SAMPLE/input_100.csv}"
SUGGESTIONS_JSONL="${SUGGESTIONS_JSONL:-$HARNESS/suggestions/local_hosted_filtered_top3.jsonl}"

# measure_only means: skip agent generation, use existing patches, run visual regression,
# then run mobile+desktop CWV unless visual reports a regression.
MODE="${MODE:-measure_only}"
PARALLEL="${PARALLEL:-8}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-1}"

# Docker is not required for this path. These defaults keep hosting, visual, and CWV local.
export SANDBOX_MODE="${SANDBOX_MODE:-local}"
export CWV_MEASURE_SANDBOX="${CWV_MEASURE_SANDBOX:-local}"
export REGRESSION_MEASURE_SANDBOX="${REGRESSION_MEASURE_SANDBOX:-local}"

if [[ -n "${MODELS:-}" ]]; then
  IFS=',' read -r -a MODEL_LIST <<< "$MODELS"
else
  MODEL_LIST=(
    "claude-opus-4-6"
    "gemma-4-31b-it"
    "gpt-5.1-codex"
    "minimax-m2.7"
    "qwen3.5-27b"
  )
fi

log() { echo "[static-sugg-eval] $(date '+%H:%M:%S') $*"; }

[[ -f "$CSV" ]] || { echo "Missing CSV: $CSV" >&2; exit 1; }
[[ -f "$SUGGESTIONS_JSONL" ]] || { echo "Missing suggestions JSONL: $SUGGESTIONS_JSONL" >&2; exit 1; }

overall_status=0

log "Dump dir: $DUMP_DIR"
log "CSV: $CSV"
log "Suggestions: $SUGGESTIONS_JSONL"
log "Mode: $MODE  Parallel: $PARALLEL  Resume: $RESUME"
[[ -n "$LIMIT" ]] && log "Limit: $LIMIT"
log "Models: ${MODEL_LIST[*]}"
echo ""

for model in "${MODEL_LIST[@]}"; do
  model="${model#"${model%%[![:space:]]*}"}"
  model="${model%"${model##*[![:space:]]}"}"
  [[ -n "$model" ]] || continue

  eval_out_dir="$DUMP_DIR/$model"
  patch_root="$eval_out_dir/results"

  if [[ ! -d "$patch_root" ]]; then
    log "ERROR: missing patch/results dir: $patch_root"
    overall_status=1
    continue
  fi

  patch_count="$(find "$patch_root" -path '*/patches/*.patch' -type f | wc -l | tr -d ' ')"
  log "Starting $model ($patch_count patches)"

  args=(
    --mode "$MODE"
    --parallel "$PARALLEL"
    --csv "$CSV"
    --suggestions-jsonl "$SUGGESTIONS_JSONL"
    --existing-patch-root "$patch_root"
  )
  [[ "$RESUME" == "1" ]] && args+=(--resume)
  [[ -n "$LIMIT" ]] && args+=(--limit "$LIMIT")

  run_exit=0
  EVAL_OUT_DIR="$eval_out_dir" bash "$HARNESS/run_cwv_evals_suggestions_row.sh" "${args[@]}" || run_exit=$?
  if [[ "$run_exit" -ne 0 ]]; then
    log "ERROR: eval failed for $model (exit $run_exit)"
    overall_status="$run_exit"
  fi

  visual_count="$(find "$patch_root" -path '*/visual.json' -type f | wc -l | tr -d ' ')"
  mobile_count="$(find "$patch_root" -path '*/mobile.json' -type f | wc -l | tr -d ' ')"
  desktop_count="$(find "$patch_root" -path '*/desktop.json' -type f | wc -l | tr -d ' ')"
  log "Done $model: visual=$visual_count mobile=$mobile_count desktop=$desktop_count"
  echo ""
done

exit "$overall_status"
