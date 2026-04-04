#!/usr/bin/env bash
# =============================================================================
# pipeline.sh — Unified Harness Pipeline
# =============================================================================
#
# Runs coding agents on benchmark repos and evaluates the results, either
# as a single end-to-end command or as individual stages.
#
# Stages:
#   1. agents   — Run coding agent(s) on dataset repos, produce patches
#   2. evaluate — Host patched sites, measure CWV, visual validation
#
# Usage:
#   # Full pipeline (both stages)
#   ./harness/pipeline.sh --agents agents/template_cwvoptimizer.sh --limit 5
#
#   # Stage 1 only (produce patches)
#   ./harness/pipeline.sh --stage agents --agents agents/template_aider.sh
#
#   # Stage 2 only (evaluate existing patches from a previous run)
#   ./harness/pipeline.sh --stage evaluate --run-dir harness/out/20260403_120000
#
#   # Full pipeline with multiple agents
#   ./harness/pipeline.sh --agents agents/template_aider.sh,agents/template_codex.sh
#
# Options:
#   --stage STAGE           agents|evaluate|all (default: all)
#   --agents AGENTS         Comma-separated agent templates (default: agents/template_cwvoptimizer.sh)
#   --csv PATH              Input CSV (default: SAMPLE/input.csv)
#   --run-dir DIR           Resume evaluation from a previous run (implies --stage evaluate)
#   --auto-snapshot         Clone+zip repos if snapshots are missing
#   --limit N               Only process the first N repos
#   --port PORT             Localhost port for hosting (default: 4000)
#   --num-runs N            CWV measurement runs per device (default: 5)
#   --skip-visual           Skip visual validation in evaluate stage
#   --help                  Show this message
# =============================================================================
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$HARNESS_DIR/scripts"

STAGE="all"
AGENTS="agents/template_cwvoptimizer.sh"
CSV_PATH=""
RUN_DIR=""
AUTO_SNAPSHOT=""
LIMIT=""
PORT=""
NUM_RUNS=""
SKIP_VISUAL=""

usage() {
  sed -n '/^# Usage:/,/^# ====/p' "$0" | grep '^#' | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stage)         shift; STAGE="$1" ;;
    --agents)        shift; AGENTS="$1" ;;
    --csv)           shift; CSV_PATH="$1" ;;
    --run-dir)       shift; RUN_DIR="$1" ;;
    --auto-snapshot) AUTO_SNAPSHOT="--auto-snapshot" ;;
    --limit)         shift; LIMIT="$1" ;;
    --port)          shift; PORT="$1" ;;
    --num-runs)      shift; NUM_RUNS="$1" ;;
    --skip-visual)   SKIP_VISUAL="--skip-visual" ;;
    --help|-h)       usage ;;
    *) echo "ERROR: Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# If --run-dir is given, only evaluate
if [[ -n "$RUN_DIR" && "$STAGE" == "all" ]]; then
  STAGE="evaluate"
fi

RUN_AGENTS=0
RUN_EVALUATE=0
case "$STAGE" in
  all)      RUN_AGENTS=1; RUN_EVALUATE=1 ;;
  agents)   RUN_AGENTS=1 ;;
  evaluate) RUN_EVALUATE=1 ;;
  *) echo "ERROR: Unknown stage '$STAGE'. Valid: agents, evaluate, all"; exit 1 ;;
esac

RUN_TS="$(date +%Y%m%d_%H%M%S)"

echo "═══════════════════════════════════════════"
echo " Harness Pipeline — $RUN_TS"
echo "═══════════════════════════════════════════"
echo " Stage:  $STAGE"
echo " Agents: $AGENTS"
[[ -n "$LIMIT" ]] && echo " Limit:  $LIMIT"
echo "═══════════════════════════════════════════"
echo ""


# =============================================================================
# Stage 1: Run Agents
# =============================================================================
if [[ "$RUN_AGENTS" -eq 1 ]]; then
  AGENT_CMD=(
    bash "$SCRIPTS_DIR/run_agents.sh"
    --agents "$AGENTS"
    --run-ts "$RUN_TS"
  )
  [[ -n "$CSV_PATH" ]]      && AGENT_CMD+=(--csv "$CSV_PATH")
  [[ -n "$AUTO_SNAPSHOT" ]]  && AGENT_CMD+=("$AUTO_SNAPSHOT")
  [[ -n "$LIMIT" ]]          && AGENT_CMD+=(--limit "$LIMIT")

  "${AGENT_CMD[@]}"

  # Set RUN_DIR for Stage 2
  RUN_DIR="$HARNESS_DIR/out/$RUN_TS"
fi


# =============================================================================
# Stage 2: Evaluate
# =============================================================================
if [[ "$RUN_EVALUATE" -eq 1 ]]; then
  if [[ -z "$RUN_DIR" ]]; then
    echo "ERROR: --run-dir is required for evaluate stage (or run agents first)"
    exit 1
  fi

  EVAL_CMD=(
    bash "$SCRIPTS_DIR/run_evaluation.sh"
    --run-dir "$RUN_DIR"
  )
  [[ -n "$CSV_PATH" ]]    && EVAL_CMD+=(--csv "$CSV_PATH")
  [[ -n "$PORT" ]]        && EVAL_CMD+=(--port "$PORT")
  [[ -n "$NUM_RUNS" ]]    && EVAL_CMD+=(--num-runs "$NUM_RUNS")
  [[ -n "$SKIP_VISUAL" ]] && EVAL_CMD+=("$SKIP_VISUAL")
  [[ -n "$LIMIT" ]]       && EVAL_CMD+=(--limit "$LIMIT")
  [[ -n "$AGENTS" && "$STAGE" == "evaluate" ]] && EVAL_CMD+=(--agents "$AGENTS")

  "${EVAL_CMD[@]}"
fi


# =============================================================================
# Done
# =============================================================================
echo ""
echo "═══════════════════════════════════════════"
echo " Pipeline complete."
[[ -n "$RUN_DIR" ]] && echo " Run dir: $RUN_DIR"
echo "═══════════════════════════════════════════"
