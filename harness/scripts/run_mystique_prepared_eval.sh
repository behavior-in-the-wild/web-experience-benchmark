#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DUMP_ROOT="${DUMP_ROOT:-$REPO_ROOT/final_result_dumps/mystique_run/claude_opus_sonnet_dumps}"
PREPARED_ROOT="${PREPARED_ROOT:-$REPO_ROOT/final_result_dumps/mystique_run/eval_ready}"
RUN_ROOT="${RUN_ROOT:-$REPO_ROOT/harness/out/mystique_eval_$(date +%Y%m%d_%H%M%S)}"
MODELS="${MODELS:-sonnet,opus}"
TEMPLATE_NAME="${TEMPLATE_NAME:-template_claudecode}"
AGENT_TEMPLATE="${AGENT_TEMPLATE:-agents/template_claudecode.sh}"
PARALLEL="${PARALLEL:-20}"
NUM_RUNS="${NUM_RUNS:-20}"
PREPARE_ONLY="${PREPARE_ONLY:-0}"
VERIFY_PATCHES="${VERIFY_PATCHES:-0}"
SKIP_VISUAL="${SKIP_VISUAL:-0}"
VISUAL_USE_GPU="${VISUAL_USE_GPU:-1}"
VISUAL_GPU_COUNT="${VISUAL_GPU_COUNT:-8}"

export HOST_SANDBOX="${HOST_SANDBOX:-0}"
export HARNESS_TMPDIR="${HARNESS_TMPDIR:-/dev/shm}"
export NUM_RUNS
export VISUAL_USE_GPU
export VISUAL_GPU_COUNT

mkdir -p "$RUN_ROOT/logs"

prepare_args=(
  --dump-root "$DUMP_ROOT"
  --output-root "$PREPARED_ROOT"
  --models "$MODELS"
  --template-name "$TEMPLATE_NAME"
)
if [[ "$VERIFY_PATCHES" == "1" ]]; then
  prepare_args+=(--verify-patches)
fi

echo "===== $(date -Is) PREPARE MYSTIQUE PATCHES ====="
python3 harness/scripts/prepare_mystique_eval.py "${prepare_args[@]}" \
  2>&1 | tee "$RUN_ROOT/logs/prepare.log"

if [[ "$PREPARE_ONLY" == "1" ]]; then
  echo "Prepared Mystique eval inputs under: $PREPARED_ROOT"
  exit 0
fi

run_one() {
  local model_key="$1"
  local model_label="$2"
  local config="$3"
  local model_root="$PREPARED_ROOT/$model_label"
  local jsonl="$model_root/eval_input.jsonl"
  local mirrors="$model_root/mirrors"
  local results="$model_root/results"

  if [[ ! -s "$jsonl" ]]; then
    echo "===== $(date -Is) SKIP $model_label: no prepared nonempty patches ====="
    return 0
  fi

  echo "===== $(date -Is) START $model_label ====="
  local args=(
    --config "$config"
    --agent-template "$AGENT_TEMPLATE"
    --source-config harness/configs/sources/live.env
    --jsonl "$jsonl"
    --mirrors-root "$mirrors"
    --patch-results-dir "$results"
    --parallel "$PARALLEL"
    --skip-init-psi
    --skip-final-psi
  )
  if [[ "$SKIP_VISUAL" == "1" ]]; then
    args+=(--skip-visual)
  fi

  (
    set -x
    EVAL_OUT_DIR="$RUN_ROOT/$model_label" \
      ./harness/evaluate.sh "${args[@]}"
  ) 2>&1 | tee "$RUN_ROOT/logs/$model_key.log"
  echo "===== $(date -Is) END $model_label ====="
}

IFS=',' read -r -a model_list <<< "$MODELS"
for model in "${model_list[@]}"; do
  model="${model//[[:space:]]/}"
  case "$model" in
    sonnet)
      run_one sonnet claude-sonnet-4-6 harness/configs/closed/cc-sonnet-4.6.env
      ;;
    opus)
      run_one opus claude-opus-4-6 harness/configs/closed/claude-opus.env
      ;;
    "")
      ;;
    *)
      echo "Unknown model key: $model" >&2
      exit 2
      ;;
  esac
done

echo "Mystique eval complete: $RUN_ROOT"
