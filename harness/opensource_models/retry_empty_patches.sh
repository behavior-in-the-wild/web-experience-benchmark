#!/usr/bin/env bash
# =============================================================
#  Retry suggestions eval on rows that still have empty/missing
#  patches, enriching the existing per-model output dirs.
#
#  Resumes into:
#    gemma-4-31b-it  → harness/out/suggestions_eval/20260601_030534
#    minimax-m2.7    → harness/out/suggestions_eval/20260602_001239
#    qwen3.5-27b     → harness/out/suggestions_eval/20260602_224629
#
#  Usage:
#    bash harness/opensource_models/retry_empty_patches.sh
#    bash harness/opensource_models/retry_empty_patches.sh --parallel 20
#    bash harness/opensource_models/retry_empty_patches.sh --models minimax-m2.7,qwen3.5-27b
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

declare -A RESUME_DIRS=(
  [gemma-4-31b-it]="$HARNESS_DIR/out/suggestions_eval/20260601_030534"
  [minimax-m2.7]="$HARNESS_DIR/out/suggestions_eval/20260602_001239"
  [qwen3.5-27b]="$HARNESS_DIR/out/suggestions_eval/20260602_224629"
)

MODELS=(gemma-4-31b-it minimax-m2.7 qwen3.5-27b)
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --models) shift; IFS=',' read -r -a MODELS <<< "$1"; shift ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

drain_all_gpus() {
  local gpu_pids
  gpu_pids="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null \
    | tr -d ' ' | grep -v '^$' | sort -u)" || gpu_pids=""
  if [[ -n "$gpu_pids" ]]; then
    echo "[retry] Killing GPU processes before start: $(echo "$gpu_pids" | tr '\n' ' ')"
    echo "$gpu_pids" | xargs kill -9 2>/dev/null || true
    sleep 10
  fi
  local used_mb
  used_mb="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
    | awk 'BEGIN{s=0}{s+=$1}END{print s}')" || used_mb="?"
  echo "[retry] GPU state before launch: ${used_mb} MiB total used"
}

echo "[retry] Models: ${MODELS[*]}"
echo "[retry] Draining GPUs before first model..."
drain_all_gpus

for model in "${MODELS[@]}"; do
  resume_dir="${RESUME_DIRS[$model]:-}"
  if [[ -z "$resume_dir" || ! -d "$resume_dir" ]]; then
    echo "[retry] ERROR: no resume dir found for $model (expected: $resume_dir)"
    exit 1
  fi

  # Count empty patches before
  results_dir="$resume_dir/$model/results"
  empty_before=0
  if [[ -d "$results_dir" ]]; then
    for d in "$results_dir"/*/; do
      patch=$(find "$d" -maxdepth 1 -name "*.patch" 2>/dev/null | head -1)
      if [[ -z "$patch" || ! -s "$patch" ]]; then
        empty_before=$((empty_before+1))
      fi
    done
  fi
  echo ""
  echo "[retry] === $model: $empty_before empty patches to retry ==="
  echo "[retry] Resume dir: $resume_dir"

  run_status=0
  bash "$SCRIPT_DIR/run_os_models_suggestions.sh" \
    --models "$model" \
    --resume-dir "$resume_dir" \
    "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}" || run_status=$?
  if [[ "$run_status" -ne 0 ]]; then
    echo "[retry] ERROR: run_os_models_suggestions.sh exited $run_status for $model — continuing"
  fi

  # Count empty patches after
  empty_after=0
  if [[ -d "$results_dir" ]]; then
    for d in "$results_dir"/*/; do
      patch=$(find "$d" -maxdepth 1 -name "*.patch" 2>/dev/null | head -1)
      if [[ -z "$patch" || ! -s "$patch" ]]; then
        empty_after=$((empty_after+1))
      fi
    done
  fi
  echo "[retry] $model: empty patches before=$empty_before → after=$empty_after"
done

echo ""
echo "[retry] All done."
