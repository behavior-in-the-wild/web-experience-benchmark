#!/usr/bin/env bash
# Row-wise evaluation for aider and codex model runs.
# Supports three modes:
#   visual_only  — run visual_validate.py only (writes visual.json)
#   cwv_only     — run CWV measurement only on non-regressed jobs
#   both         — visual first, then CWV on non-regressed (default)
#
# Results are read from:
#   closed_model_runs/aider/results/{tid}_template_aider/
#   closed_model_runs/codex/results/{tid}_template_codex/
#
# Usage:
#   bash harness/run_cwv_evals_aider_codex_row.sh
#   MODE=visual_only bash ...
#   MODE=cwv_only    bash ...
#   PARALLEL=4 MODELS=aider bash ...
#   bash harness/run_cwv_evals_aider_codex_row.sh --resume
#   LIMIT=3 bash ...   # test with first 3 templates
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"

source "$HARNESS/row_eval_lib.sh"

PARALLEL="${PARALLEL:-4}"
BASE_PORT="${BASE_PORT:-19200}"
CSV="${CSV:-$HARNESS/SAMPLE/input.csv}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
MODE="${MODE:-both}"   # visual_only | cwv_only | both
NUM_RUNS="${NUM_RUNS:-5}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume)   RESUME=1;  shift ;;
    --csv)      shift; CSV="$1"; shift ;;
    --limit)    shift; LIMIT="$1"; shift ;;
    --parallel) shift; PARALLEL="$1"; shift ;;
    --mode)     shift; MODE="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$MODE" != "visual_only" && "$MODE" != "cwv_only" && "$MODE" != "both" ]]; then
  echo "Invalid MODE=$MODE  (must be visual_only|cwv_only|both)" >&2; exit 1
fi

# Models to evaluate (can be overridden via MODELS env var: MODELS=aider or MODELS=aider,codex)
IFS=',' read -ra MODELS <<< "${MODELS:-aider,codex}"

# Map model name → job suffix
declare -A MODEL_AGENT=(
  [aider]="template_aider"
  [codex]="template_codex"
)

VISUAL_SCRIPT="$SCRIPT_DIR/src/regression_tool/visual_validate.py"
CWV_SCRIPT="$SCRIPT_DIR/src/cwv_tool/cwv_benchmark.py"
TMP_ROOT="$HARNESS/out/rowwise_aider_codex_tmp"

# ── Activate venv + load .env ────────────────────────────────────────────────
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"

# =============================================================================
# Per-template job function
# =============================================================================
run_job() {
  local ID="$1" REPO_ID="$2" FRAMEWORK="$3" COMMIT_ID="$4"
  local HOST_FILE_PATH="$5" SLOT="$6"
  local JOB_TMP="$TMP_ROOT/$ID"
  local BASELINE_DIR="$JOB_TMP/baseline"

  echo "====== Job $ID | $REPO_ID | slot=$SLOT ======"
  mkdir -p "$JOB_TMP"

  # ── 1. Clone baseline once ──────────────────────────────────────────────────
  if ! row_clone_baseline "$REPO_ID" "$COMMIT_ID" "$BASELINE_DIR" "$TMP_ROOT" "[rowwise]"; then
    rm -rf "$JOB_TMP"
    return 1
  fi
  local COMMIT_CLEAN="$COMMIT_ID"
  [[ "$COMMIT_CLEAN" == " " || "$COMMIT_CLEAN" == "null" ]] && COMMIT_CLEAN=""

  # ── 3. Run each model against this baseline ─────────────────────────────────
  local FW
  FW="$(echo "${FRAMEWORK:-unknown}" | tr '[:upper:]' '[:lower:]')"

  local _MODEL_IDX=0
  for model in "${MODELS[@]}"; do
    local PORT=$(( BASE_PORT + SLOT + _MODEL_IDX * PARALLEL ))
    local AGENT_NAME="${MODEL_AGENT[$model]}"
    local JOB_LABEL="${ID}_${AGENT_NAME}"
    local OUT_DIR="$SCRIPT_DIR/closed_model_runs/${model}/results/$JOB_LABEL"
    local PATCH_FILE="$OUT_DIR/${JOB_LABEL}.patch"

    if [[ ! -d "$OUT_DIR" ]]; then
      echo "[rowwise] SKIP: no results dir for $model/$ID"
      (( _MODEL_IDX++ )) || true
      continue
    fi

    # Write baseline_meta.json
    row_write_baseline_meta "$OUT_DIR" "$COMMIT_CLEAN"

    # ── Resume checks ────────────────────────────────────────────────────────
    if [[ "$RESUME" == "1" ]]; then
      if [[ "$MODE" == "cwv_only" ]]; then
        if [[ -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
          echo "[rowwise] SKIP (resume): CWV already done ($model/$ID)"
          (( _MODEL_IDX++ )) || true; continue
        fi
      else
        if [[ -f "$OUT_DIR/visual.json" ]]; then
          echo "[rowwise] SKIP (resume): visual.json exists ($model/$ID)"
          (( _MODEL_IDX++ )) || true; continue
        fi
      fi
    fi

    # cwv_only: skip if no visual.json yet (visual must run first)
    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ ! -f "$OUT_DIR/visual.json" ]]; then
        echo "[rowwise] SKIP cwv_only: no visual.json yet ($model/$ID)"
        (( _MODEL_IDX++ )) || true; continue
      fi
      # Also skip if regressed — no point measuring CWV
      local _reg
      _reg=$(python3 -c "
import json,sys
d=json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
      if [[ "$_reg" == "1" ]]; then
        echo "[rowwise] SKIP cwv_only: regressed ($model/$ID)"
        (( _MODEL_IDX++ )) || true; continue
      fi
    fi

    local WORK_DIR="$JOB_TMP/$model"
    rm -rf "$WORK_DIR"
    cp -r --no-preserve=mode "$BASELINE_DIR" "$WORK_DIR"

    # Apply patch
    if ! row_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "[rowwise] ($model/$ID)"; then
      echo "[rowwise] SKIP: patch failed to apply ($model/$ID)"
      rm -rf "$WORK_DIR"
      (( _MODEL_IDX++ )) || true
      continue
    fi
    PATCH_FILE="$ROW_EFFECTIVE_PATCH_FILE"

    # Start HTTP server through the central Docker/local hosting tool.
    if ! row_start_host "$WORK_DIR" "$OUT_DIR" "$HOST_FILE_PATH" "$FRAMEWORK" "$PORT" "$SLOT"; then
      echo "[rowwise] ERROR: host tool failed ($model/$ID)"
      rm -rf "$WORK_DIR"; (( _MODEL_IDX++ )) || true; continue
    fi
    local HOST_PID="$ROW_HOST_HANDLE"

    if ! row_wait_for_server "$PORT" 90; then
      echo "[rowwise] ERROR: server never ready ($model/$ID)"
      row_kill_server "$HOST_PID"
      rm -rf "$WORK_DIR"; (( _MODEL_IDX++ )) || true; continue
    fi

    # ── Visual validation ─────────────────────────────────────────────────────
    if [[ "$MODE" == "visual_only" || "$MODE" == "both" ]]; then
      row_measure_visual "$OUT_DIR" "$REPO_ID" "$COMMIT_CLEAN" "$FW" "$PATCH_FILE" "$PORT" "480" "$SLOT"

      local reg="?"
      if [[ -f "$OUT_DIR/visual.json" ]]; then
        reg=$(python3 -c "
import json
d=json.load(open('$OUT_DIR/visual.json'))
print('REGRESSED' if d.get('overall_regression') is True else 'ok')
" 2>/dev/null || echo "?")
      fi
      echo "[rowwise] visual $model/$ID → $reg"

      # In both mode: skip CWV if regressed
      if [[ "$MODE" == "both" && "$reg" == "REGRESSED" ]]; then
        echo "[rowwise] SKIP CWV (regressed) $model/$ID"
        row_kill_server "$HOST_PID"
        rm -rf "$WORK_DIR"; (( _MODEL_IDX++ )) || true; continue
      fi
    fi

    # ── CWV measurement ───────────────────────────────────────────────────────
    if [[ "$MODE" == "cwv_only" || "$MODE" == "both" ]]; then
      row_measure_cwv "$OUT_DIR" "$PORT" "$NUM_RUNS" "$ROW_HOST_HANDLE" "$SLOT"
      echo "[rowwise] CWV done $model/$ID"
    fi

    row_kill_server "$HOST_PID"
    rm -rf "$WORK_DIR"
    echo "[rowwise] ✓ $model/$ID"
    (( _MODEL_IDX++ )) || true
  done

  rm -rf "$JOB_TMP"
  echo "✓ Done: $ID"
}

# =============================================================================
# Job pool
# =============================================================================
declare -A JOB_SLOT=()
_SLOT=0

# =============================================================================
# Dispatch
# =============================================================================
mkdir -p "$TMP_ROOT" "$HARNESS/out"

# Clear ports
_MAX_PORT=$(( BASE_PORT + PARALLEL * ${#MODELS[@]} ))
for _p in $(seq "$BASE_PORT" "$_MAX_PORT"); do row_free_port "$_p"; done

echo "[rowwise] MODE=$MODE  CSV=$CSV"
echo "[rowwise] Models:    ${MODELS[*]}"
echo "[rowwise] Parallel:  $PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$LIMIT" ]]      && echo "[rowwise] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]] && echo "[rowwise] --resume active"

while IFS=$'\t' read -r ID REPO_ID FRAMEWORK COMMIT_ID HOST_FILE_PATH; do
  acquire_slot
  slot=$_SLOT
  ( run_job "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" "$slot" ) </dev/null &
  JOB_SLOT[$!]=$slot
done < <(python3 - "$CSV" "${LIMIT:-}" <<'PY'
import csv, sys
csv.field_size_limit(10**7)
path  = sys.argv[1]
limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None
want  = ["ID", "REPO_ID", "FRAMEWORK", "COMMIT_ID", "HOST_FILE_PATH"]
n = 0
with open(path, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row.get("ID", "").strip() not in ("", "ID"):
            vals = [(row.get(c) or " ").replace("\t", " ").replace("\n", " ") for c in want]
            print("\t".join(vals))
            n += 1
            if limit and n >= limit:
                break
PY
)

wait
echo ""
echo "[rowwise] All jobs complete."
echo "[rowwise] Results in:"
for m in "${MODELS[@]}"; do
  echo "  closed_model_runs/$m/results/"
done
