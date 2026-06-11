#!/usr/bin/env bash
# =============================================================
#  Run Claude Code Opus 4.6 on the live-bench suggestions eval.
#  No GPU / vLLM needed — uses Anthropic API via Azure Foundry.
#
#  Usage:
#    bash harness_live_bench/run_claudecode_opus.sh
#    bash harness_live_bench/run_claudecode_opus.sh --parallel 10
#    bash harness_live_bench/run_claudecode_opus.sh --resume-dir harness_live_bench/out/suggestions_eval/20260610_120000
# =============================================================
set -euo pipefail

HARNESS_LIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS_LIVE/.." && pwd)"

JSONL_PATH="$HARNESS_LIVE/SAMPLE/live_filtered_top3.jsonl"
MIRRORS_ROOT="${MIRRORS_ROOT:-$SCRIPT_DIR/live_assets_eds}"
PARALLEL="${PARALLEL:-10}"
LIMIT=""
SKIP_MEASURE=1
RESUME_DIR=""
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-6}"

usage() {
  cat <<'EOF'
Usage: harness_live_bench/run_claudecode_opus.sh [options]

Options:
  --parallel N         Parallel jobs (default: 10)
  --jsonl PATH         Input JSONL (default: SAMPLE/live_filtered_top3.jsonl)
  --mirrors-root DIR   Mirror root dir (default: ../live_assets_eds)
  --model MODEL        Claude model ID (default: claude-opus-4-6)
  --limit N            Process only first N rows (for testing)
  --no-skip-measure    Also run visual + CWV measurement after patching
  --resume-dir DIR     Resume into existing output root dir
  --help, -h           Show this message

Output:
  harness_live_bench/out/suggestions_eval/<timestamp>/claude-opus-4-6/
    results/{row_id}_s{0,1,2}_template_claudecode_opus/
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --parallel)       shift; PARALLEL="$1"; shift ;;
    --jsonl)          shift; JSONL_PATH="$1"; shift ;;
    --mirrors-root)   shift; MIRRORS_ROOT="$1"; shift ;;
    --model)          shift; CLAUDE_MODEL="$1"; shift ;;
    --limit)          shift; LIMIT="$1"; shift ;;
    --no-skip-measure) SKIP_MEASURE=0; shift ;;
    --resume-dir)     shift; RESUME_DIR="$1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

[[ -f "$JSONL_PATH" ]] || { echo "Missing JSONL: $JSONL_PATH"; exit 1; }
[[ "$MIRRORS_ROOT" = /* ]] || MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"
[[ -d "$MIRRORS_ROOT" ]] || { echo "Missing MIRRORS_ROOT: $MIRRORS_ROOT"; exit 1; }

# ── Output root ───────────────────────────────────────────────────────────────
if [[ -n "$RESUME_DIR" ]]; then
  [[ "$RESUME_DIR" = /* ]] || RESUME_DIR="$(cd "$RESUME_DIR" && pwd)"
  [[ -d "$RESUME_DIR" ]] || { echo "Error: --resume-dir '$RESUME_DIR' not found"; exit 1; }
  MODEL_DIR="$RESUME_DIR/$CLAUDE_MODEL"
  echo "[claudecode] Resuming into: $MODEL_DIR"
else
  RUN_TS="$(date +%Y%m%d_%H%M%S)"
  MODEL_DIR="$HARNESS_LIVE/out/suggestions_eval/$RUN_TS/$CLAUDE_MODEL"
fi
mkdir -p "$MODEL_DIR"

AGENT_SCRIPT="$HARNESS_LIVE/agents/template_claudecode_opus.sh"
[[ -f "$AGENT_SCRIPT" ]] || { echo "Missing agent: $AGENT_SCRIPT"; exit 1; }

run_args=("--parallel" "$PARALLEL")
[[ "$SKIP_MEASURE" == "1" ]] && run_args+=("--skip-measure")
[[ -n "$LIMIT" ]]            && run_args+=("--limit" "$LIMIT")
[[ -n "$RESUME_DIR" ]]       && run_args+=("--resume")

echo "[claudecode] Model:       $CLAUDE_MODEL"
echo "[claudecode] JSONL:       $JSONL_PATH"
echo "[claudecode] MIRRORS:     $MIRRORS_ROOT"
echo "[claudecode] Output:      $MODEL_DIR"
echo "[claudecode] Parallel:    $PARALLEL"
[[ "$SKIP_MEASURE" == "1" ]] && echo "[claudecode] patch-only (no visual/CWV)"

JSONL="$JSONL_PATH" \
MIRRORS_ROOT="$MIRRORS_ROOT" \
EVAL_OUT_DIR="$MODEL_DIR" \
AGENT_SCRIPT="$AGENT_SCRIPT" \
CLAUDE_MODEL="$CLAUDE_MODEL" \
  bash "$HARNESS_LIVE/run_cwv_evals_suggestions_row.sh" "${run_args[@]}"

echo "[claudecode] Done. Output: $MODEL_DIR"
