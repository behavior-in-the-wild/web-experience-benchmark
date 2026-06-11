#!/usr/bin/env bash
# =============================================================
#  Run OpenCode + GPT-5.1 Codex (Azure) on the live-bench
#  suggestions eval.  No GPU / vLLM needed.
#
#  Usage:
#    bash harness_live_bench/run_opencode_gpt51codex.sh
#    bash harness_live_bench/run_opencode_gpt51codex.sh --parallel 8
#    bash harness_live_bench/run_opencode_gpt51codex.sh --resume-dir harness_live_bench/out/suggestions_eval/20260611_...
# =============================================================
set -euo pipefail

HARNESS_LIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS_LIVE/.." && pwd)"

# Source .env for Azure OpenAI credentials
ENV_FILE="$SCRIPT_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
else
  echo "[opencode-codex] WARNING: .env not found at $ENV_FILE"
fi

JSONL_PATH="$HARNESS_LIVE/SAMPLE/live_filtered_top3.jsonl"
MIRRORS_ROOT="${MIRRORS_ROOT:-$SCRIPT_DIR/live_assets_eds}"
PARALLEL="${PARALLEL:-8}"
LIMIT=""
SKIP_MEASURE=1
RESUME_DIR=""
MODEL_LABEL="gpt-5.1-codex"

usage() {
  cat <<'EOF'
Usage: harness_live_bench/run_opencode_gpt51codex.sh [options]

Options:
  --parallel N         Parallel jobs (default: 8)
  --jsonl PATH         Input JSONL (default: SAMPLE/live_filtered_top3.jsonl)
  --mirrors-root DIR   Mirror root dir (default: ../live_assets_eds)
  --limit N            Process only first N rows (for testing)
  --no-skip-measure    Also run visual + CWV measurement after patching
  --resume-dir DIR     Resume into existing output root dir
  --help, -h           Show this message

Output:
  harness_live_bench/out/suggestions_eval/<timestamp>/gpt-5.1-codex/
    results/{row_uuid}_s{0,1,2}_template_opencode/
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --parallel)        shift; PARALLEL="$1"; shift ;;
    --jsonl)           shift; JSONL_PATH="$1"; shift ;;
    --mirrors-root)    shift; MIRRORS_ROOT="$1"; shift ;;
    --limit)           shift; LIMIT="$1"; shift ;;
    --no-skip-measure) SKIP_MEASURE=0; shift ;;
    --resume-dir)      shift; RESUME_DIR="$1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

[[ -f "$JSONL_PATH" ]]  || { echo "Missing JSONL: $JSONL_PATH"; exit 1; }
[[ "$MIRRORS_ROOT" = /* ]] || MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"
[[ -d "$MIRRORS_ROOT" ]] || { echo "Missing MIRRORS_ROOT: $MIRRORS_ROOT"; exit 1; }

[[ -n "${AZURE_OPENAI_API_KEY:-}" ]] || {
  echo "[opencode-codex] ERROR: AZURE_OPENAI_API_KEY not set. Check .env"; exit 1
}
# OpenCode uses AZURE_API_KEY (not AZURE_OPENAI_API_KEY)
export AZURE_API_KEY="${AZURE_OPENAI_API_KEY}"

# ── Output root ───────────────────────────────────────────────────────────────
if [[ -n "$RESUME_DIR" ]]; then
  [[ "$RESUME_DIR" = /* ]] || RESUME_DIR="$(cd "$RESUME_DIR" && pwd)"
  [[ -d "$RESUME_DIR" ]] || { echo "Error: --resume-dir '$RESUME_DIR' not found"; exit 1; }
  MODEL_DIR="$RESUME_DIR/$MODEL_LABEL"
  echo "[opencode-codex] Resuming into: $MODEL_DIR"
else
  RUN_TS="$(date +%Y%m%d_%H%M%S)"
  MODEL_DIR="$HARNESS_LIVE/out/suggestions_eval/$RUN_TS/$MODEL_LABEL"
fi
mkdir -p "$MODEL_DIR"

AGENT_SCRIPT="$HARNESS_LIVE/agents/template_opencode.sh"
[[ -f "$AGENT_SCRIPT" ]] || { echo "Missing agent: $AGENT_SCRIPT"; exit 1; }

run_args=("--parallel" "$PARALLEL")
[[ "$SKIP_MEASURE" == "1" ]] && run_args+=("--skip-measure")
[[ -n "$LIMIT" ]]             && run_args+=("--limit" "$LIMIT")
[[ -n "$RESUME_DIR" ]]        && run_args+=("--resume")

echo "[opencode-codex] Model:       azure/${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5.1-codex}"
echo "[opencode-codex] JSONL:       $JSONL_PATH"
echo "[opencode-codex] MIRRORS:     $MIRRORS_ROOT"
echo "[opencode-codex] Output:      $MODEL_DIR"
echo "[opencode-codex] Parallel:    $PARALLEL"
[[ "$SKIP_MEASURE" == "1" ]] && echo "[opencode-codex] patch-only (no visual/CWV)"

JSONL="$JSONL_PATH" \
MIRRORS_ROOT="$MIRRORS_ROOT" \
EVAL_OUT_DIR="$MODEL_DIR" \
AGENT_SCRIPT="$AGENT_SCRIPT" \
  bash "$HARNESS_LIVE/run_cwv_evals_suggestions_row.sh" "${run_args[@]}"

echo "[opencode-codex] Done. Output: $MODEL_DIR"
