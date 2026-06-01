#!/usr/bin/env bash
# Runs visual-regression-only evaluation sequentially across all three
# model run directories. CWV measurements are already in place; this only
# regenerates the visual.json verdicts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[run_all_regression] $(date '+%H:%M:%S') $*"; }

log "Starting VISUAL-ONLY regression eval across all three runs"
log "PARALLEL=${PARALLEL:-8}  MODE=visual_only"
log "==========================================="

log "1/3 — oss_model_runs   (ports 12000+)"
MODE=visual_only bash "$SCRIPT_DIR/run_cwv_evals_oss_row.sh"
log "1/3 done"

log "2/3 — oss_scale_eval_run (ports 15000+)"
MODE=visual_only bash "$SCRIPT_DIR/run_cwv_evals_scale_row.sh"
log "2/3 done"

log "3/3 — closed_model_runs  (ports 18000+)"
MODE=visual_only bash "$SCRIPT_DIR/run_cwv_evals_closed_row.sh"
log "3/3 done"

log "==========================================="
log "All regression evals complete."
