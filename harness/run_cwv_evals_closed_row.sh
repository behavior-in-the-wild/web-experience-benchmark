#!/usr/bin/env bash
# Row-wise CWV + visual evaluation for ALL closed-source model runs.
# For each CSV job: clone baseline ONCE, then measure every model sequentially.
# Patches read from closed_model_runs/<model>/results/<JOB_LABEL>/<JOB_LABEL>.patch
# (mirrors oss_model_runs/ layout).
#
# Usage (run from project root or anywhere):
#   bash harness/run_cwv_evals_closed_row.sh
#   PARALLEL=8 bash harness/run_cwv_evals_closed_row.sh
#   LIMIT=5 bash harness/run_cwv_evals_closed_row.sh           # test with first 5 jobs
#   bash harness/run_cwv_evals_closed_row.sh --resume          # skip jobs already done
#   MODE=visual_only bash harness/run_cwv_evals_closed_row.sh  # phase 1: visual only
#   MODE=cwv_only    bash harness/run_cwv_evals_closed_row.sh  # phase 2: CWV on non-regressed rows
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"

source "$HARNESS/row_eval_lib.sh"

PARALLEL="${PARALLEL:-5}"
NUM_RUNS="${NUM_RUNS:-5}"
BASE_PORT="${BASE_PORT:-18000}"
CSV="${CSV:-$HARNESS/SAMPLE/input_100.csv}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
# MODE: visual_only | cwv_only | both (default)
#   visual_only — run only visual regression; skip CWV
#   cwv_only    — skip visual; only measure CWV on rows whose visual.json
#                 says overall_regression != true (i.e. visual passed)
#   both        — original behaviour
MODE="${MODE:-both}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume) RESUME=1; shift ;;
    --csv) shift; CSV="$1"; shift ;;
    --limit) shift; LIMIT="$1"; shift ;;
    --parallel) shift; PARALLEL="$1"; shift ;;
    --mode) shift; MODE="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$MODE" != "visual_only" && "$MODE" != "cwv_only" && "$MODE" != "both" ]]; then
  echo "Invalid MODE: $MODE (must be visual_only|cwv_only|both)" >&2
  exit 1
fi
echo "[rowwise] MODE=$MODE PARALLEL=$PARALLEL"

MODELS=(
  gemini-2-5-flash
  gemini-2-5-pro
  cc-opus-4.6
  cc-sonnet-4.6
  gpt-4.1
  gpt-5
  gpt-5.1-codex
)

# Per-model agent suffix (used to locate patch/result dirs)
declare -A MODEL_AGENT=(
  [gemini-2-5-flash]="template_gemini"
  [gemini-2-5-pro]="template_gemini"
  [cc-opus-4.6]="template_claudecode"
  [cc-sonnet-4.6]="template_claudecode"
  [gpt-4.1]="template_opencodegpt41"
  [gpt-5]="template_opencode"
  [gpt-5.1-codex]="template_opencodegpt51codex"
)
VISUAL_SCRIPT="$SCRIPT_DIR/src/regression_tool/visual_validate.py"
CWV_SCRIPT="$SCRIPT_DIR/src/cwv_tool/cwv_benchmark.py"
TMP_ROOT="$HARNESS/out/rowwise_closed_tmp"

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"

# =========================
# Per-job function
# =========================
run_job() {
  local ID="$1" REPO_ID="$2" FRAMEWORK="$3" COMMIT_ID="$4"
  local HOST_FILE_PATH="$5" SLOT="$6"
  local JOB_TMP="$TMP_ROOT/$ID"
  local BASELINE_DIR="$JOB_TMP/baseline"

  echo "====== Job $ID | $REPO_ID | slot=$SLOT ======"
  mkdir -p "$JOB_TMP"

  # -------------------------
  # 1) Clone baseline once
  # -------------------------
  if ! row_clone_baseline "$REPO_ID" "$COMMIT_ID" "$BASELINE_DIR" "$TMP_ROOT" "[rowwise]"; then
    rm -rf "$JOB_TMP"
    return 1
  fi
  local COMMIT_CLEAN="$COMMIT_ID"
  [[ "$COMMIT_CLEAN" == " " || "$COMMIT_CLEAN" == "null" ]] && COMMIT_CLEAN=""

  # -------------------------
  # 3) Measure each model against this baseline
  # -------------------------
  local FW
  FW="$(echo "${FRAMEWORK:-unknown}" | tr '[:upper:]' '[:lower:]')"

  local _MODEL_IDX=0
  for model in "${MODELS[@]}"; do
    local PORT=$(( BASE_PORT + SLOT + _MODEL_IDX * PARALLEL ))
    local AGENT_NAME="${MODEL_AGENT[$model]}"
    local JOB_LABEL="${ID}_${AGENT_NAME}"
    local OUT_DIR="$SCRIPT_DIR/closed_model_runs/$model/results/$JOB_LABEL"
    local PATCH_FILE="$OUT_DIR/${JOB_LABEL}.patch"

    # Skip if agent never ran this job for this model
    if [[ ! -d "$OUT_DIR" ]]; then
      echo "[rowwise] SKIP: no results dir for $model/$ID"
      continue
    fi

    # Record which commit was actually used as baseline
    row_write_baseline_meta "$OUT_DIR" "$COMMIT_CLEAN"

    # Skip if already measured (only when --resume is set)
    # Skip condition depends on MODE:
    #   visual_only — skip if visual.json exists
    #   cwv_only    — skip if mobile.json AND desktop.json exist
    #   both        — skip if visual.json exists
    if [[ "$RESUME" == "1" ]]; then
      if [[ "$MODE" == "cwv_only" ]]; then
        if [[ -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
          echo "[rowwise] SKIP: CWV already measured $model/$ID"
          continue
        fi
      else
        if [[ -f "$OUT_DIR/visual.json" ]]; then
          echo "[rowwise] SKIP: already evaluated $model/$ID"
          continue
        fi
      fi
    fi

    # cwv_only mode: skip if no visual.json yet (visual must run first)
    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ ! -f "$OUT_DIR/visual.json" ]]; then
        echo "[rowwise] SKIP cwv_only: no visual.json yet ($model/$ID)"
        continue
      fi
    fi

    local WORK_DIR="$JOB_TMP/$model"
    rm -rf "$WORK_DIR"
    cp -r --no-preserve=mode "$BASELINE_DIR" "$WORK_DIR"

    # Apply patch (use empty file as patch if missing)
    if ! row_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "[rowwise] ($model/$ID)"; then
      echo "[rowwise] SKIP: patch failed to apply ($model/$ID)"
      rm -rf "$WORK_DIR"
      continue
    fi
    PATCH_FILE="$ROW_EFFECTIVE_PATCH_FILE"

    # Start HTTP server through the central Docker/local hosting tool.
    if ! row_start_host "$WORK_DIR" "$OUT_DIR" "$HOST_FILE_PATH" "$FRAMEWORK" "$PORT" "$SLOT"; then
      echo "[rowwise] ERROR: host tool failed ($model/$ID)"
      rm -rf "$WORK_DIR"
      continue
    fi
    local HOST_PID="$ROW_HOST_HANDLE"

    if ! row_wait_for_server "$PORT" 90; then
      echo "[rowwise] ERROR: server never ready ($model/$ID)"
      row_kill_server "$HOST_PID"
      rm -rf "$WORK_DIR"
      continue
    fi

    # ── Visual validation (MODE=visual_only or both) ──
    if [[ "$MODE" == "visual_only" || "$MODE" == "both" ]]; then
      row_measure_visual "$OUT_DIR" "$REPO_ID" "$COMMIT_CLEAN" "$FW" "$PATCH_FILE" "$PORT" "480" "$SLOT"
    fi

    # ── CWV measurement (MODE=cwv_only or both, always run regardless of regression) ──
    if [[ "$MODE" == "cwv_only" || "$MODE" == "both" ]]; then
      row_measure_cwv "$OUT_DIR" "$PORT" "$NUM_RUNS" "$ROW_HOST_HANDLE" "$SLOT"
    fi

    row_kill_server "$HOST_PID"
    rm -rf "$WORK_DIR"
    echo "[rowwise] ✓ $model / $ID"
    (( _MODEL_IDX++ )) || true
  done

  rm -rf "$JOB_TMP"
  echo "✓ Done: $ID (${#MODELS[@]} models)"
}

# =========================
# Job pool (same slot mechanism as evaluate.sh)
# =========================
declare -A JOB_SLOT=()
_SLOT=0

# =========================
# Dispatch loop
# =========================
mkdir -p "$TMP_ROOT" "$HARNESS/out"

# Kill any zombie servers from previous runs holding our port range
_MAX_PORT=$(( BASE_PORT + PARALLEL * ${#MODELS[@]} ))
for _p in $(seq "$BASE_PORT" "$_MAX_PORT"); do row_free_port "$_p"; done

echo "[rowwise] CSV:      $CSV"
echo "[rowwise] Models:   ${MODELS[*]}"
echo "[rowwise] Parallel: $PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$LIMIT" ]]    && echo "[rowwise] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]] && echo "[rowwise] --resume: skipping already-evaluated jobs"

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
