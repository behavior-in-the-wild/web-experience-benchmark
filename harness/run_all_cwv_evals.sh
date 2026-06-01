#!/usr/bin/env bash
# Runs CWV-only measurement sequentially across all three model run directories.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[run_all_cwv] $(date '+%H:%M:%S') $*"; }

log "Starting CWV-only eval across all three runs"
log "==========================================="

log "1/3 — oss_model_runs"
MODE=cwv_only bash "$SCRIPT_DIR/run_cwv_evals_oss_row.sh"
log "1/3 done"

log "2/3 — oss_scale_eval_run"
MODE=cwv_only bash "$SCRIPT_DIR/run_cwv_evals_scale_row.sh"
log "2/3 done"

log "3/3 — closed_model_runs"
MODE=cwv_only bash "$SCRIPT_DIR/run_cwv_evals_closed_row.sh"
log "3/3 done"

log "==========================================="
log "All CWV evals complete."
