#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

export PATH="/home/colligo/.opencode/bin:$PATH"
export HOST_SANDBOX="${HOST_SANDBOX:-0}"
export HARNESS_TMPDIR="${HARNESS_TMPDIR:-/dev/shm}"
export GEMINI_KEY_FILE="${GEMINI_KEY_FILE:-${GOOGLE_APPLICATION_CREDENTIALS:-$REPO_ROOT/gemini_key.json}}"

RUN_ROOT="${RUN_ROOT:-$REPO_ROOT/harness/out/live_requested_$(date +%Y%m%d_%H%M%S)}"
JSONL="${JSONL:-$REPO_ROOT/harness/table.jsonl}"
MIRRORS_ROOT="${MIRRORS_ROOT:-$REPO_ROOT/live_assets_eds}"
PARALLEL="${PARALLEL:-20}"
NUM_RUNS="${NUM_RUNS:-5}"
ONLY_MODELS="${ONLY_MODELS:-}"

mkdir -p "$RUN_ROOT/logs"

should_run() {
  local label="$1"
  [[ -z "$ONLY_MODELS" || ",$ONLY_MODELS," == *",$label,"* ]]
}

run_eval() {
  local label="$1"
  local port="$2"
  shift 2

  if ! should_run "$label"; then
    echo "===== $(date -Is) SKIP $label ====="
    return 0
  fi

  echo "===== $(date -Is) START $label ====="
  (
    set -x
    PORT="$port" \
    NUM_RUNS="$NUM_RUNS" \
    EVAL_OUT_DIR="$RUN_ROOT/$label" \
    ./harness/evaluate.sh "$@" \
      --source-config harness/configs/sources/live.env \
      --jsonl "$JSONL" \
      --mirrors-root "$MIRRORS_ROOT" \
      --parallel "$PARALLEL" \
      --skip-init-psi \
      --skip-final-psi
  ) 2>&1 | tee "$RUN_ROOT/logs/$label.log"
  echo "===== $(date -Is) END $label ====="
}

# Closed models.
run_eval gemini-2-5-pro 4000 \
  --config harness/configs/closed/gemini-pro.env

run_eval cc-opus-4.6 4000 \
  --config harness/configs/closed/claude-opus.env \
  --agent-template agents/template_live_claudecode_opus.sh

run_eval gpt-5.1-codex 4000 \
  --config harness/configs/closed/gpt-5.1-codex.env \
  --agent-template agents/template_live_opencode.sh

# Open models. Each evaluate.sh invocation serves one model, runs it, then tears it down.
run_eval qwen3.5-27b 4000 \
  --config harness/configs/open/qwen3.5-27b.env \
  --serve-model

run_eval gemma-4-31b-it 4000 \
  --config harness/configs/open/gemma-4-31b-it.env \
  --serve-model

run_eval minimax-m2.7 4000 \
  --config harness/configs/open/minimax-m2.7.env \
  --serve-model

echo "All requested model runs complete: $RUN_ROOT"
