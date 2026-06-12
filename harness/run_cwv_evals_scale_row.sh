#!/usr/bin/env bash
# Row-wise CWV + visual evaluation for the qwen scale-eval run.
# For each CSV job: clone baseline ONCE, then measure all qwen models sequentially.
#
# Usage (run from project root or anywhere):
#   bash harness/run_cwv_evals_scale_row.sh
#   PARALLEL=8 bash harness/run_cwv_evals_scale_row.sh
#   LIMIT=5 bash harness/run_cwv_evals_scale_row.sh           # test with first 5 jobs
#   bash harness/run_cwv_evals_scale_row.sh --resume          # skip jobs already done
#   MODE=visual_only bash harness/run_cwv_evals_scale_row.sh  # phase 1: visual only
#   MODE=cwv_only    bash harness/run_cwv_evals_scale_row.sh  # phase 2: CWV on non-regressed rows
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"
source "$HARNESS/host_tool_lib.sh"

PARALLEL="${PARALLEL:-16}"
NUM_RUNS="${NUM_RUNS:-5}"
BASE_PORT="${BASE_PORT:-15000}"
CSV="${CSV:-$HARNESS/SAMPLE/input_100.csv}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
# MODE: visual_only | cwv_only | both (default)
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
  qwen3.5-9b
  qwen3.5-27b
  qwen3.5-35b-a3b
  qwen3.5-122b-a10b
  qwen3.5-397b-a17b
)

AGENT_NAME="template_opencode_os"
VISUAL_SCRIPT="$SCRIPT_DIR/src/regression_tool/visual_validate.py"
CWV_SCRIPT="$SCRIPT_DIR/src/cwv_tool/cwv_benchmark.py"
TMP_ROOT="$HARNESS/out/rowwise_scale_tmp"

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"

# =========================
# Helpers
# =========================
wait_for_server() {
  local port="$1" timeout="${2:-90}" i
  for i in $(seq 1 "$timeout"); do
    curl -fs "http://localhost:${port}/" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

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
  # 1-2) Fetch pinned commit directly + commit baseline snapshot
  # -------------------------
  local CLONE_TMP
  CLONE_TMP="$(mktemp -d -p "$TMP_ROOT")"
  if ! bench_git_clone_checkout "$REPO_ID" "$COMMIT_ID" "$CLONE_TMP" "[rowwise]" "$ID"; then
    rm -rf "$JOB_TMP" "$CLONE_TMP"
    return 1
  fi
  local COMMIT_CLEAN="$BENCH_GIT_REQUESTED_COMMIT"
  git -C "$CLONE_TMP" add -A >/dev/null 2>&1 || true
  git -C "$CLONE_TMP" commit -qm "baseline" >/dev/null 2>&1 || true
  mv "$CLONE_TMP" "$BASELINE_DIR"

  # -------------------------
  # 3) Measure each model against this baseline
  # -------------------------
  local FW
  FW="$(echo "${FRAMEWORK:-unknown}" | tr '[:upper:]' '[:lower:]')"

  local _MODEL_IDX=0
  for model in "${MODELS[@]}"; do
    local PORT=$(( BASE_PORT + SLOT + _MODEL_IDX * PARALLEL ))
    local JOB_LABEL="${ID}_${AGENT_NAME}"
    local OUT_DIR="$SCRIPT_DIR/oss_scale_eval_run/$model/results/$JOB_LABEL"
    local PATCH_FILE="$OUT_DIR/${JOB_LABEL}.patch"

    if [[ ! -d "$OUT_DIR" ]]; then
      echo "[rowwise] SKIP: no results dir for $model/$ID"
      continue
    fi

    bench_git_write_meta "$OUT_DIR/baseline_meta.json"

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

    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ ! -f "$OUT_DIR/visual.json" ]]; then
        echo "[rowwise] SKIP cwv_only: no visual.json yet ($model/$ID)"
        continue
      fi
      _VR=$(python3 -c "
import json
try:
    d = json.load(open('$OUT_DIR/visual.json'))
    print('1' if d.get('overall_regression') is True else '0')
except Exception:
    print('1')
")
      if [[ "$_VR" == "1" ]]; then
        echo "[rowwise] SKIP cwv_only: visual regressed ($model/$ID)"
        continue
      fi
    fi

    local WORK_DIR="$JOB_TMP/$model"
    rm -rf "$WORK_DIR"
    cp -r --no-preserve=mode "$BASELINE_DIR" "$WORK_DIR"

    if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
      if ! bench_git_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "[rowwise] ($model/$ID)"; then
        echo "[rowwise] SKIP: patch failed to apply ($model/$ID)"
        rm -rf "$WORK_DIR"
        continue
      fi
    else
      echo "[rowwise] WARN: empty/missing patch for $model/$ID — measuring baseline"
      PATCH_FILE="$OUT_DIR/empty.patch"
      touch "$PATCH_FILE"
      bench_git_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "[rowwise] ($model/$ID)" || true
    fi

    if ! bench_start_host "$WORK_DIR" "$OUT_DIR" "$HOST_FILE_PATH" "$FRAMEWORK" "$PORT" "$OUT_DIR/host.log" "$SLOT"; then
      echo "[rowwise] ERROR: host tool failed ($model/$ID)"
      rm -rf "$WORK_DIR"
      continue
    fi
    local HOST_PID="$BENCH_HOST_HANDLE"

    if ! wait_for_server "$PORT" 90; then
      echo "[rowwise] ERROR: server never ready ($model/$ID)"
      bench_stop_host "$HOST_PID"
      rm -rf "$WORK_DIR"
      continue
    fi

    local VISUAL_REGRESSED=0
    if [[ "$MODE" == "visual_only" || "$MODE" == "both" ]]; then
      python3 "$VISUAL_SCRIPT" \
        --url              "http://localhost:$PORT" \
        --screenshot-path  "$OUT_DIR/screenshot.png" \
        --repo-id          "$REPO_ID" \
        --commit-id        "${COMMIT_CLEAN:-}" \
        --framework        "${FW:-static html}" \
        --patch-file       "$PATCH_FILE" \
        --output-json      "$OUT_DIR/visual.json" \
        2>>"$OUT_DIR/visual.stderr" \
        || echo "[rowwise] WARN: visual failed ($model/$ID)"

      if [[ -f "$OUT_DIR/visual.json" ]]; then
        VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
      fi
    fi

    if [[ "$MODE" == "cwv_only" || "$MODE" == "both" ]]; then
      if [[ "$VISUAL_REGRESSED" == "1" ]]; then
        echo "[rowwise] Skipping CWV — visual regression ($model/$ID)"
      else
        bench_measure_cwv "http://localhost:$PORT" mobile "$NUM_RUNS" "$OUT_DIR/mobile.json" "$OUT_DIR/cwv_stderr.txt" "$HOST_PID" "$SLOT" || true
        bench_measure_cwv "http://localhost:$PORT" desktop "$NUM_RUNS" "$OUT_DIR/desktop.json" "$OUT_DIR/cwv_stderr.txt" "$HOST_PID" "$SLOT" || true
      fi
    fi

    bench_stop_host "$HOST_PID"
    wait "$HOST_PID" 2>/dev/null || true
    rm -rf "$WORK_DIR"
    echo "[rowwise] ✓ $model / $ID"
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

acquire_slot() {
  while true; do
    local pid count s used p
    count=${#JOB_SLOT[@]}
    if [[ $count -gt 0 ]]; then
      for pid in "${!JOB_SLOT[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
          _SLOT="${JOB_SLOT[$pid]}"
          unset "JOB_SLOT[$pid]"
          return 0
        fi
      done
    fi
    if [[ $count -lt $PARALLEL ]]; then
      for s in $(seq 0 $((PARALLEL - 1))); do
        used=0
        if [[ $count -gt 0 ]]; then
          for p in "${!JOB_SLOT[@]}"; do
            [[ "${JOB_SLOT[$p]}" == "$s" ]] && used=1 && break
          done
        fi
        [[ $used -eq 0 ]] && { _SLOT="$s"; return 0; }
      done
    fi
    sleep 0.5
  done
}

# =========================
# Dispatch loop
# =========================
mkdir -p "$TMP_ROOT" "$HARNESS/out"

# Kill any zombie servers from previous runs holding our port range
_MAX_PORT=$(( BASE_PORT + PARALLEL * ${#MODELS[@]} ))
for _p in $(seq "$BASE_PORT" "$_MAX_PORT"); do bench_free_port "$_p"; done

echo "[rowwise] CSV:      $CSV"
echo "[rowwise] Models:   ${MODELS[*]}"
echo "[rowwise] Parallel: $PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$LIMIT" ]]      && echo "[rowwise] LIMIT=$LIMIT"
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
