#!/usr/bin/env bash
# Full sequential evaluation pipeline for aider and codex:
#
#   Phase 1: Visual eval  — aider   (writes visual.json for all 30 jobs)
#   Phase 2: CWV eval     — aider   (measures CWV on non-regressed jobs only)
#   Phase 3: Visual eval  — codex   (writes visual.json for all 30 jobs)
#   Phase 4: CWV eval     — codex   (measures CWV on non-regressed jobs only)
#
# Each phase waits for the previous to finish before starting.
#
# Usage:
#   bash harness/run_aider_codex_full.sh            # run all 4 phases
#   bash harness/run_aider_codex_full.sh --resume   # skip already-done jobs in each phase
#   PARALLEL=4 bash harness/run_aider_codex_full.sh
#   LIMIT=3    bash harness/run_aider_codex_full.sh  # test with first 3 templates
#
# Recommended: launch inside tmux so it survives disconnects:
#   tmux new-session -d -s aider_codex_eval \
#     "bash harness/run_aider_codex_full.sh 2>&1 | tee harness/out/aider_codex_eval.log"
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"
ROW_SCRIPT="$HARNESS/run_cwv_evals_aider_codex_row.sh"
LOG_DIR="$HARNESS/out"
mkdir -p "$LOG_DIR"

# ── Pass-through env vars ────────────────────────────────────────────────────
export PARALLEL="${PARALLEL:-4}"
export BASE_PORT="${BASE_PORT:-19200}"
export LIMIT="${LIMIT:-}"
export NUM_RUNS="${NUM_RUNS:-5}"
export RESUME="${RESUME:-0}"
export CSV="${CSV:-$HARNESS/SAMPLE/input.csv}"

# Forward --resume flag to the row script
RESUME_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume) RESUME=1; RESUME_FLAG="--resume"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOGFILE="$LOG_DIR/aider_codex_eval_${TIMESTAMP}.log"

# ── Logging helper ────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }
sep() { log "══════════════════════════════════════════════════════"; }

log "Starting full aider+codex evaluation pipeline"
log "PARALLEL=$PARALLEL  NUM_RUNS=$NUM_RUNS  CSV=$CSV"
log "Log: $LOGFILE"
sep

# ── Phase 1: Visual eval — aider ─────────────────────────────────────────────
log "PHASE 1/4 ▶ Visual eval — aider"
MODE=visual_only MODELS=aider bash "$ROW_SCRIPT" $RESUME_FLAG \
  2>&1 | tee -a "$LOGFILE"
log "PHASE 1/4 ✓ Visual eval — aider complete"
sep

# ── Phase 2: CWV eval — aider ────────────────────────────────────────────────
log "PHASE 2/4 ▶ CWV measurement — aider (non-regressed only)"
MODE=cwv_only MODELS=aider bash "$ROW_SCRIPT" $RESUME_FLAG \
  2>&1 | tee -a "$LOGFILE"
log "PHASE 2/4 ✓ CWV measurement — aider complete"
sep

# ── Phase 3: Visual eval — codex ─────────────────────────────────────────────
log "PHASE 3/4 ▶ Visual eval — codex"
MODE=visual_only MODELS=codex bash "$ROW_SCRIPT" $RESUME_FLAG \
  2>&1 | tee -a "$LOGFILE"
log "PHASE 3/4 ✓ Visual eval — codex complete"
sep

# ── Phase 4: CWV eval — codex ────────────────────────────────────────────────
log "PHASE 4/4 ▶ CWV measurement — codex (non-regressed only)"
MODE=cwv_only MODELS=codex bash "$ROW_SCRIPT" $RESUME_FLAG \
  2>&1 | tee -a "$LOGFILE"
log "PHASE 4/4 ✓ CWV measurement — codex complete"
sep

log "ALL PHASES COMPLETE"
log "Results:"
log "  aider  → $SCRIPT_DIR/closed_model_runs/aider/results/"
log "  codex  → $SCRIPT_DIR/closed_model_runs/codex/results/"
log "Full log → $LOGFILE"
