#!/usr/bin/env bash
# Row-wise visual + CWV evaluation for the autodep model run.
# For each CSV job: clone baseline ONCE, then measure all models sequentially.
#
# Key differences vs run_cwv_evals_oss_row.sh:
#   * All 10 autodep models (5 standalone OSS + 5 Qwen3.5 scale)
#   * Results live in harness/out/autodep_models/<RUN_DIR>/$model/results/
#   * HOST_FILE_PATH is always host_files/host_autodep.sh (ignores CSV column)
#     because autodep sites require npm install + npm run build at serve time.
#   * REPO_ID and AUTODEP_ROOT are exported when launching the server.
#   * Server wait timeout is 300s (npm build can take a while).
#
# Usage (run from project root or anywhere):
#   bash harness/run_cwv_evals_autodep_row.sh
#   RUN_DIR=20260524_082517 bash harness/run_cwv_evals_autodep_row.sh
#   PARALLEL=4 bash harness/run_cwv_evals_autodep_row.sh
#   bash harness/run_cwv_evals_autodep_row.sh --resume
#   MODE=visual_only bash harness/run_cwv_evals_autodep_row.sh
#   MODE=cwv_only    bash harness/run_cwv_evals_autodep_row.sh
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"

source "$HARNESS/row_eval_lib.sh"

PARALLEL="${PARALLEL:-4}"
NUM_RUNS="${NUM_RUNS:-5}"
BASE_PORT="${BASE_PORT:-21000}"
CSV="${CSV:-$HARNESS/SAMPLE/github_47_clean.csv}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
# MODE: visual_only | cwv_only | both (default)
MODE="${MODE:-both}"
# RUN_DIR: timestamp subdir under harness/out/autodep_models/
# Defaults to the most recently modified dir.
RUN_DIR="${RUN_DIR:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume)    RESUME=1; shift ;;
    --csv)       shift; CSV="$1"; shift ;;
    --limit)     shift; LIMIT="$1"; shift ;;
    --parallel)  shift; PARALLEL="$1"; shift ;;
    --mode)      shift; MODE="$1"; shift ;;
    --run-dir)   shift; RUN_DIR="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$MODE" != "visual_only" && "$MODE" != "cwv_only" && "$MODE" != "both" ]]; then
  echo "Invalid MODE: $MODE (must be visual_only|cwv_only|both)" >&2
  exit 1
fi

# Resolve the autodep run output root
AUTODEP_OUT="$HARNESS/out/autodep_models"
if [[ -z "$RUN_DIR" ]]; then
  # Pick the most recently modified subdir
  RUN_DIR="$(ls -1t "$AUTODEP_OUT" 2>/dev/null | grep -v '\.log$' | head -1)"
  [[ -n "$RUN_DIR" ]] || { echo "No run dirs found under $AUTODEP_OUT" >&2; exit 1; }
  echo "[autodep-rowwise] Auto-selected RUN_DIR=$RUN_DIR"
fi
RUN_ROOT="$AUTODEP_OUT/$RUN_DIR"
[[ -d "$RUN_ROOT" ]] || { echo "RUN_ROOT not found: $RUN_ROOT" >&2; exit 1; }

MODELS=(
  gemma-4-31b-it
  glm-4.7-flash
  qwen3-coder-next
  devstral-2-123b
  minimax-m2.7
  qwen3.5-9b
  qwen3.5-27b
  qwen3.5-35b-a3b
  qwen3.5-122b-a10b
  qwen3.5-397b-a17b
)

# Export AUTODEP_ROOT so host_autodep.sh can find per-repo scripts
export AUTODEP_ROOT="${AUTODEP_ROOT:-$(cd "$SCRIPT_DIR" && pwd)/autodep_final_100_host_scripts}"
[[ -d "$AUTODEP_ROOT" ]] || { echo "AUTODEP_ROOT not found: $AUTODEP_ROOT" >&2; exit 1; }

AGENT_NAME="template_opencode_os"
VISUAL_SCRIPT="$SCRIPT_DIR/src/regression_tool/visual_validate.py"
CWV_SCRIPT="$SCRIPT_DIR/src/cwv_tool/cwv_benchmark.py"
TMP_ROOT="$HARNESS/out/rowwise_autodep_tmp"

# Always use the autodep host dispatcher (npm install + build inside)
HOST_FILE_PATH="host_files/host_autodep.sh"

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"

echo "[autodep-rowwise] MODE=$MODE PARALLEL=$PARALLEL"

# =========================
# Per-job function
# =========================
run_job() {
  local ID="$1" REPO_ID="$2" FRAMEWORK="$3" COMMIT_ID="$4"
  local _UNUSED_HOST="$5" SLOT="$6"
  local JOB_TMP="$TMP_ROOT/$ID"
  local BASELINE_DIR="$JOB_TMP/baseline"

  echo "====== Job $ID | $REPO_ID | slot=$SLOT ======"
  mkdir -p "$JOB_TMP"

  # -------------------------
  # 1) Clone baseline once
  # -------------------------
  if ! row_clone_baseline "$REPO_ID" "$COMMIT_ID" "$BASELINE_DIR" "$TMP_ROOT" "[autodep-rowwise]"; then
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
    local JOB_LABEL="${ID}_${AGENT_NAME}"
    local OUT_DIR="$RUN_ROOT/$model/results/$JOB_LABEL"
    local PATCH_FILE="$OUT_DIR/${JOB_LABEL}.patch"

    if [[ ! -d "$OUT_DIR" ]]; then
      echo "[autodep-rowwise] SKIP: no results dir for $model/$ID"
      (( _MODEL_IDX++ )) || true
      continue
    fi

    row_write_baseline_meta "$OUT_DIR" "$COMMIT_CLEAN"

    if [[ "$RESUME" == "1" ]]; then
      if [[ "$MODE" == "cwv_only" ]]; then
        if [[ -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
          echo "[autodep-rowwise] SKIP: CWV already measured $model/$ID"
          (( _MODEL_IDX++ )) || true
          continue
        fi
      else
        if [[ -f "$OUT_DIR/visual.json" ]]; then
          echo "[autodep-rowwise] SKIP: already evaluated $model/$ID"
          (( _MODEL_IDX++ )) || true
          continue
        fi
      fi
    fi

    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ ! -f "$OUT_DIR/visual.json" ]]; then
        echo "[autodep-rowwise] SKIP cwv_only: no visual.json yet ($model/$ID)"
        (( _MODEL_IDX++ )) || true
        continue
      fi
    fi

    local WORK_DIR="$JOB_TMP/$model"
    rm -rf "$WORK_DIR"
    cp -r --no-preserve=mode "$BASELINE_DIR" "$WORK_DIR"

    if ! row_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "[autodep-rowwise] ($model/$ID)"; then
      echo "[autodep-rowwise] SKIP: patch failed to apply ($model/$ID)"
      rm -rf "$WORK_DIR"
      (( _MODEL_IDX++ )) || true
      continue
    fi
    PATCH_FILE="$ROW_EFFECTIVE_PATCH_FILE"

    # Export REPO_ID so host_autodep.sh can find the right per-repo autodep script
    export REPO_ID
    if ! row_start_host "$WORK_DIR" "$OUT_DIR" "$HOST_FILE_PATH" "$FRAMEWORK" "$PORT" "$SLOT"; then
      echo "[autodep-rowwise] ERROR: host tool failed ($model/$ID)"
      rm -rf "$WORK_DIR"
      (( _MODEL_IDX++ )) || true
      continue
    fi
    local HOST_PID="$ROW_HOST_HANDLE"

    # Autodep sites run npm install + npm run build before serving — allow 300s
    if ! row_wait_for_server "$PORT" 300; then
      echo "[autodep-rowwise] ERROR: server never ready ($model/$ID)"
      row_kill_server "$HOST_PID"
      rm -rf "$WORK_DIR"
      (( _MODEL_IDX++ )) || true
      continue
    fi

    # ── Visual validation ──
    local VISUAL_REGRESSED=0
    if [[ "$MODE" == "visual_only" || "$MODE" == "both" ]]; then
      row_measure_visual "$OUT_DIR" "$REPO_ID" "$COMMIT_CLEAN" "$FW" "$PATCH_FILE" "$PORT" "480" "$SLOT"
      VISUAL_REGRESSED="$ROW_VISUAL_REGRESSED"
    fi

    # ── CWV measurement ──
    if [[ "$MODE" == "cwv_only" || "$MODE" == "both" ]]; then
      row_measure_cwv "$OUT_DIR" "$PORT" "$NUM_RUNS" "$ROW_HOST_HANDLE" "$SLOT"
    fi

    row_kill_server "$HOST_PID"
    rm -rf "$WORK_DIR"
    echo "[autodep-rowwise] ✓ $model / $ID"
    (( _MODEL_IDX++ )) || true
  done

  rm -rf "$JOB_TMP"
  echo "✓ Done: $ID (${#MODELS[@]} models)"
}

# =========================
# Job pool
# =========================
declare -A JOB_SLOT=()
_SLOT=0

# =========================
# Dispatch loop
# =========================
mkdir -p "$TMP_ROOT" "$HARNESS/out"

# Kill zombie servers from previous runs in our port range
_MAX_PORT=$(( BASE_PORT + PARALLEL * ${#MODELS[@]} ))
for _p in $(seq "$BASE_PORT" "$_MAX_PORT"); do row_free_port "$_p"; done

echo "[autodep-rowwise] CSV:         $CSV"
echo "[autodep-rowwise] RUN_ROOT:    $RUN_ROOT"
echo "[autodep-rowwise] AUTODEP_ROOT:$AUTODEP_ROOT"
echo "[autodep-rowwise] Models:      ${MODELS[*]}"
echo "[autodep-rowwise] Parallel:    $PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$LIMIT" ]]      && echo "[autodep-rowwise] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]] && echo "[autodep-rowwise] --resume: skipping already-evaluated jobs"

while IFS=$'\t' read -r ID REPO_ID FRAMEWORK COMMIT_ID HOST_FILE_PATH_CSV; do
  acquire_slot
  slot=$_SLOT
  # HOST_FILE_PATH_CSV from the CSV is intentionally ignored — autodep sites
  # always use host_autodep.sh. Passed as positional arg but unused in run_job.
  ( run_job "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH_CSV" "$slot" ) </dev/null &
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
echo "[autodep-rowwise] All jobs complete."
