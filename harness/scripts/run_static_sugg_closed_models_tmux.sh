#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
export TMPDIR="${TMPDIR:-$ROOT/.tmp}"
mkdir -p "$TMPDIR"

set -a
[[ -f "$ROOT/.env" ]] && source "$ROOT/.env"
[[ -f "$ROOT/harness/.env" ]] && source "$ROOT/harness/.env"
set +a

export DUMP_DIR="${DUMP_DIR:-$ROOT/final_result_dumps/static_sugg_eval}"
export CSV="${CSV:-$ROOT/harness/SAMPLE/input_100.csv}"
export SUGGESTIONS_JSONL="${SUGGESTIONS_JSONL:-$ROOT/harness/suggestions/local_hosted_filtered_top3.jsonl}"
export MODE="${MODE:-both}"
export PARALLEL="${PARALLEL:-8}"
export RESUME="${RESUME:-1}"
export NUM_RUNS="${NUM_RUNS:-5}"
export OPENCODE_PHASE_TIMEOUT="${OPENCODE_PHASE_TIMEOUT:-7200}"
export OPENCODE_TIMEOUT_KILL_AFTER="${OPENCODE_TIMEOUT_KILL_AFTER:-60s}"
export CLAUDE_PHASE_TIMEOUT="${CLAUDE_PHASE_TIMEOUT:-7200}"
export CLAUDE_TIMEOUT_KILL_AFTER="${CLAUDE_TIMEOUT_KILL_AFTER:-60s}"

log() { echo "[static-sugg-closed] $(date '+%H:%M:%S') $*"; }

run_model() {
  local model_dir="$1"
  local agent_script="$2"
  shift 2

  mkdir -p "$DUMP_DIR/$model_dir/results"
  log "Starting $model_dir with agent $(basename "$agent_script")"
  env "$@" \
    EVAL_OUT_DIR="$DUMP_DIR/$model_dir" \
    AGENT_SCRIPT="$agent_script" \
    bash "$ROOT/harness/run_cwv_evals_suggestions_row.sh" \
      --mode "$MODE" \
      --parallel "$PARALLEL" \
      --csv "$CSV" \
      --suggestions-jsonl "$SUGGESTIONS_JSONL" \
      --resume

  log "Done $model_dir: result_dirs=$(find "$DUMP_DIR/$model_dir/results" -mindepth 1 -maxdepth 1 -type d ! -name scratch 2>/dev/null | wc -l) visual=$(find "$DUMP_DIR/$model_dir/results" -name visual.json 2>/dev/null | wc -l) mobile=$(find "$DUMP_DIR/$model_dir/results" -name mobile.json 2>/dev/null | wc -l) desktop=$(find "$DUMP_DIR/$model_dir/results" -name desktop.json 2>/dev/null | wc -l)"
}

run_model \
  "gemini-2-5-pro" \
  "$ROOT/harness/agents/template_gemini.sh" \
  OPENCODE_MODEL=vertex/gemini-2.5-pro

run_model \
  "claude-sonnet-4-6" \
  "$ROOT/harness/agents/template_claudecode.sh" \
  CLAUDE_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-claude-sonnet-4-6}"

log "All requested static suggestion closed-model runs finished."
