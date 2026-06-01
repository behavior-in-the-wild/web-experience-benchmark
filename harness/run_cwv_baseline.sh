#!/usr/bin/env bash
# Measure CWV for every baseline (unpatched) site in the same conditions
# as the model runs:
#   - Same clone-then-host pipeline (host_files/<framework>.sh)
#   - Same NUM_RUNS (5 by default) on mobile + desktop
#   - PARALLEL=8 by default
#   - Same cwv_benchmark.py with CDP throttling
#
# Output layout (mirrors model runs but flatter — no model/template layer):
#   cwv_baseline_scores/<ID>/
#     mobile.json
#     desktop.json
#     cwv_stderr.txt
#     host.log
#     baseline_meta.json   { requested_commit, actual_commit, commit_fallback }
#
# Usage (run from project root or anywhere):
#   bash harness/run_cwv_baseline.sh
#   PARALLEL=8 bash harness/run_cwv_baseline.sh
#   LIMIT=5    bash harness/run_cwv_baseline.sh
#   bash harness/run_cwv_baseline.sh --resume        # skip jobs already done
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"

PARALLEL="${PARALLEL:-8}"
NUM_RUNS="${NUM_RUNS:-5}"
BASE_PORT="${BASE_PORT:-21000}"     # gap from model-run ranges (12k/15k/18k)
CSV="${CSV:-$HARNESS/SAMPLE/input_100.csv}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
OUT_ROOT="$SCRIPT_DIR/cwv_baseline_scores"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume)   RESUME=1; shift ;;
    --csv)      shift; CSV="$1"; shift ;;
    --limit)    shift; LIMIT="$1"; shift ;;
    --parallel) shift; PARALLEL="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

CWV_SCRIPT="$SCRIPT_DIR/scripts/helper_scripts/cwv_benchmark.py"
TMP_ROOT="$HARNESS/out/cwv_baseline_tmp"

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env (so NUM_RUNS/AZURE_DEPLOYMENT match the model-run environment)
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done

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
  local OUT_DIR="$OUT_ROOT/$ID"
  local PORT=$(( BASE_PORT + SLOT ))

  mkdir -p "$JOB_TMP" "$OUT_DIR"

  # Resume: skip if both mobile + desktop already measured
  if [[ "$RESUME" == "1" && -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
    echo "[baseline] SKIP: already measured ID=$ID"
    rm -rf "$JOB_TMP"
    return 0
  fi

  echo "====== Baseline Job $ID | $REPO_ID | slot=$SLOT port=$PORT ======"

  # -------------------------
  # 1) Clone baseline
  # -------------------------
  local CLONE_TMP
  CLONE_TMP="$(mktemp -d -p "$TMP_ROOT")"
  echo "[baseline] Cloning $REPO_ID ..."
  if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
       git -c credential.helper='' -c http.extraHeader='' \
       clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
    echo "[baseline] Retry clone in 10s (ID=$ID) ..."
    sleep 10
    rm -rf "$CLONE_TMP"; CLONE_TMP="$(mktemp -d -p "$TMP_ROOT")"
    if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -c credential.helper='' -c http.extraHeader='' \
         clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
      echo "[baseline] ERROR: clone failed after retry (ID=$ID)"
      rm -rf "$JOB_TMP" "$CLONE_TMP"
      return 1
    fi
  fi

  # -------------------------
  # 2) Checkout pinned commit (HEAD fallback if commit is gone)
  # -------------------------
  local COMMIT_CLEAN="$COMMIT_ID"
  [[ "$COMMIT_CLEAN" == " " || "$COMMIT_CLEAN" == "null" ]] && COMMIT_CLEAN=""
  local COMMIT_FALLBACK="false"
  if [[ -n "$COMMIT_CLEAN" ]]; then
    if ! git -C "$CLONE_TMP" checkout "$COMMIT_CLEAN" >/dev/null 2>&1; then
      echo "[baseline] WARN: commit $COMMIT_CLEAN not found (force-pushed?), falling back to HEAD (ID=$ID)"
      COMMIT_FALLBACK="true"
    fi
  fi
  local ACTUAL_COMMIT
  ACTUAL_COMMIT="$(git -C "$CLONE_TMP" rev-parse HEAD 2>/dev/null || echo "unknown")"

  printf '{"requested_commit":"%s","actual_commit":"%s","commit_fallback":%s}\n' \
    "$COMMIT_CLEAN" "$ACTUAL_COMMIT" "$COMMIT_FALLBACK" > "$OUT_DIR/baseline_meta.json"

  # -------------------------
  # 3) Host + measure CWV
  # -------------------------
  fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
  for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done

  PORT="$PORT" setsid bash "$HARNESS/$HOST_FILE_PATH" "$CLONE_TMP" "$OUT_DIR/host.log" &
  local HOST_PID=$!

  if ! wait_for_server "$PORT" 90; then
    echo "[baseline] ERROR: server never ready (ID=$ID)"
    kill -- -"$HOST_PID" 2>/dev/null || kill "$HOST_PID" 2>/dev/null || true
    rm -rf "$CLONE_TMP" "$JOB_TMP"
    return 1
  fi

  python3 "$CWV_SCRIPT" \
    --device mobile  --num-runs "$NUM_RUNS" \
    --url "http://localhost:$PORT" \
    > "$OUT_DIR/mobile.json"  2>>"$OUT_DIR/cwv_stderr.txt" || true
  python3 "$CWV_SCRIPT" \
    --device desktop --num-runs "$NUM_RUNS" \
    --url "http://localhost:$PORT" \
    > "$OUT_DIR/desktop.json" 2>>"$OUT_DIR/cwv_stderr.txt" || true

  kill -- -"$HOST_PID" 2>/dev/null || kill "$HOST_PID" 2>/dev/null || true
  wait "$HOST_PID" 2>/dev/null || true

  rm -rf "$CLONE_TMP" "$JOB_TMP"
  echo "[baseline] ✓ Done: $ID"
}

# =========================
# Slot pool
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
# Dispatch
# =========================
mkdir -p "$TMP_ROOT" "$OUT_ROOT"

# Free any zombie servers in our port range
_MAX_PORT=$(( BASE_PORT + PARALLEL ))
for _p in $(seq "$BASE_PORT" "$_MAX_PORT"); do fuser -k -KILL "$_p/tcp" 2>/dev/null || true; done

echo "[baseline] CSV:      $CSV"
echo "[baseline] Parallel: $PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS  Output=$OUT_ROOT"
[[ -n "$LIMIT" ]]      && echo "[baseline] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]] && echo "[baseline] --resume: skipping already-measured jobs"

while IFS=$'\t' read -r ID REPO_ID FRAMEWORK COMMIT_ID HOST_FILE_PATH; do
  acquire_slot
  slot=$_SLOT
  ( run_job "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" "$slot" ) &
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
echo "[baseline] All jobs complete."
