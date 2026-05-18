#!/usr/bin/env bash
# measure_cwv.sh
#
# Standalone CWV measurement for pre-existing patches.
# For each CSV row: git clone → checkout commit → apply patch → local HTTP server
# → measure Core Web Vitals with Playwright (http://localhost:PORT, no bore tunnel).
#
# Two modes:
#
#   Single-agent mode (--patches-dir + --agent):
#     ./measure_cwv.sh \
#       --patches-dir agent_patches/results_gemini_2-5-pro \
#       --agent template_gemini
#
#   Multi-agent mode (--agent-patches-root):
#     ./measure_cwv.sh --agent-patches-root agent_patches/
#     Iterates every sub-directory; agent name is auto-detected from the
#     patch filenames (<ID>_<AGENT>.patch).  Each agent's results land in
#     OUTPUT_DIR/<subdir-name>/.
#
# Outputs per job:
#   <ID>_<AGENT>_mobile.json     — CWV metrics (mobile Playwright run)
#   <ID>_<AGENT>_desktop.json    — CWV metrics (desktop Playwright run)
#   <ID>_<AGENT>_cwv_stderr.txt  — cwv_benchmark.py log
#   <ID>_<AGENT>_host.log        — HTTP server log

set -euo pipefail

# =============================================================================
# Constants
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CWV_SCRIPT="$(cd "$SCRIPT_DIR/../../../cwv-agent-main/cwv-agent/scripts/helper_scripts" && pwd)/cwv_benchmark.py"

# =============================================================================
# Defaults
# =============================================================================
CSV="${CSV:-$SCRIPT_DIR/SAMPLE/input.csv}"
PATCHES_DIR=""
AGENT_PATCHES_ROOT=""
AGENT_NAME=""
OUTPUT_DIR=""
NUM_RUNS=5
PARALLEL=1
BASE_PORT=5000
LIMIT=""

# =============================================================================
# Usage
# =============================================================================
usage() {
  cat <<'EOF'
Usage: measure_cwv.sh [options]

Mode A – single agent directory:
  --patches-dir D   Directory containing <ID>_<AGENT>.patch files
  --agent NAME      Agent name used in patch filenames (e.g. template_gemini)

Mode B – all agents at once:
  --agent-patches-root D
                    Root directory whose immediate sub-directories each contain
                    <ID>_<AGENT>.patch files (e.g. agent_patches/).
                    Agent name is auto-detected from the filenames.
                    Results land in OUTPUT_DIR/<subdir>/.

Common options:
  --csv PATH        Input CSV (default: SAMPLE/input.csv)
  --output-dir D    Root results directory (default: out/<timestamp>_cwv)
  --num-runs N      Playwright CWV runs per site per device (default: 5)
  --parallel N      Concurrent jobs across all agents/rows (default: 1)
  --port N          Base port; jobs use BASE_PORT + slot (default: 5000)
  --limit N         Only process first N CSV rows
  --help, -h        Show this message

If a patch file is absent for a given ID, CWV is measured on the unpatched
baseline (useful as a control run).
EOF
}

# =============================================================================
# Argument parsing
# =============================================================================
while [[ $# -gt 0 ]]; do
  case "$1" in
    --csv)                shift; CSV="$1";                shift ;;
    --patches-dir)        shift; PATCHES_DIR="$1";        shift ;;
    --agent-patches-root) shift; AGENT_PATCHES_ROOT="$1"; shift ;;
    --agent)              shift; AGENT_NAME="$1";         shift ;;
    --output-dir)         shift; OUTPUT_DIR="$1";         shift ;;
    --num-runs)           shift; NUM_RUNS="$1";           shift ;;
    --parallel)           shift; PARALLEL="$1";           shift ;;
    --port)               shift; BASE_PORT="$1";          shift ;;
    --limit)              shift; LIMIT="$1";              shift ;;
    --help|-h)            usage; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done

# =============================================================================
# Validate and build the list of (patches_dir, agent_name, output_subdir) tuples
# =============================================================================

# detect_agent <dir>  — returns the agent name inferred from the first .patch
# file in <dir>.  Patch files are named <ID>_<AGENT>.patch where ID is numeric.
detect_agent() {
  local dir="$1"
  local first
  first=$(ls "$dir"/*.patch 2>/dev/null | head -1) || true
  [[ -n "$first" ]] || { echo ""; return; }
  basename "$first" .patch | sed 's/^[0-9]*_//'
}

# BATCH_PATCHES / BATCH_AGENTS / BATCH_OUTDIRS are parallel arrays.
declare -a BATCH_PATCHES=()
declare -a BATCH_AGENTS=()
declare -a BATCH_OUTDIRS=()

if [[ -n "$AGENT_PATCHES_ROOT" ]]; then
  # ---- Mode B: iterate sub-directories of root ----
  [[ "$AGENT_PATCHES_ROOT" = /* ]] || AGENT_PATCHES_ROOT="$(cd "$AGENT_PATCHES_ROOT" && pwd)"
  [[ -d "$AGENT_PATCHES_ROOT" ]] || { echo "ERROR: --agent-patches-root not found: $AGENT_PATCHES_ROOT"; exit 1; }

  for subdir in "$AGENT_PATCHES_ROOT"/*/; do
    [[ -d "$subdir" ]] || continue
    subname="$(basename "$subdir")"
    detected="$(detect_agent "$subdir")"
    if [[ -z "$detected" ]]; then
      echo "WARN: No .patch files found in $subdir — skipping"
      continue
    fi
    BATCH_PATCHES+=("$subdir")
    BATCH_AGENTS+=("$detected")
    BATCH_OUTDIRS+=("$subname")
  done

  [[ ${#BATCH_PATCHES[@]} -gt 0 ]] || { echo "ERROR: No patch sub-directories found under $AGENT_PATCHES_ROOT"; exit 1; }

elif [[ -n "$PATCHES_DIR" && -n "$AGENT_NAME" ]]; then
  # ---- Mode A: single directory ----
  [[ "$PATCHES_DIR" = /* ]] || PATCHES_DIR="$(cd "$PATCHES_DIR" && pwd)"
  [[ -d "$PATCHES_DIR" ]] || { echo "ERROR: --patches-dir not found: $PATCHES_DIR"; exit 1; }
  BATCH_PATCHES+=("$PATCHES_DIR")
  BATCH_AGENTS+=("$AGENT_NAME")
  BATCH_OUTDIRS+=("")   # empty → results go directly into OUTPUT_DIR
else
  echo "ERROR: Specify either --agent-patches-root, or both --patches-dir and --agent"
  usage; exit 1
fi

[[ -f "$CSV" ]]        || { echo "ERROR: CSV not found: $CSV"; exit 1; }
[[ -f "$CWV_SCRIPT" ]] || { echo "ERROR: cwv_benchmark.py not found: $CWV_SCRIPT"; exit 1; }

RUN_TS="$(date +%Y%m%d_%H%M%S)"
OUTPUT_DIR="${OUTPUT_DIR:-$SCRIPT_DIR/out/${RUN_TS}_cwv}"
# Resolve to absolute path so host scripts that cd into the repo dir can still write logs
[[ "$OUTPUT_DIR" = /* ]] || OUTPUT_DIR="$(mkdir -p "$OUTPUT_DIR" && cd "$OUTPUT_DIR" && pwd)"
mkdir -p "$OUTPUT_DIR"

echo "[cwv] CSV:         $CSV"
echo "[cwv] Output root: $OUTPUT_DIR"
echo "[cwv] Num runs:    $NUM_RUNS"
echo "[cwv] Parallel:    $PARALLEL  BasePort=$BASE_PORT"
[[ -n "$LIMIT" ]] && echo "[cwv] Limit:       $LIMIT rows"
for i in "${!BATCH_PATCHES[@]}"; do
  echo "[cwv] Agent batch: ${BATCH_AGENTS[$i]}  →  ${BATCH_PATCHES[$i]}"
done

# =============================================================================
# Helpers
# =============================================================================
wait_for_server() {
  local port="$1" timeout="${2:-90}" i
  for i in $(seq 1 "$timeout"); do
    curl -fs "http://localhost:${port}/" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# Strip log lines emitted to stdout before the JSON object.
# cwv_optimizer loggers use StreamHandler(sys.stdout); JSON follows them.
extract_json() {
  python3 -c "
import sys
raw = sys.stdin.read()
start = raw.find('{')
if start >= 0:
    sys.stdout.write(raw[start:])
else:
    sys.stdout.write(raw)
"
}

# =============================================================================
# Per-job function
# run_job ID REPO_ID COMMIT_ID HOST_FILE_PATH PORT PATCHES_DIR AGENT_NAME OUT_DIR
# PORT is a unique never-reused value assigned by the dispatch loop.
# =============================================================================
run_job() {
  local ID="$1"
  local REPO_ID="$2"
  local COMMIT_ID="$3"
  local HOST_FILE_PATH="$4"
  local PORT="$5"
  local JOB_PATCHES_DIR="$6"
  local JOB_AGENT="$7"
  local JOB_OUT_DIR="$8"

  local JOB_LABEL WORK_DIR REPO_DIR
  JOB_LABEL="${ID}_${JOB_AGENT}"
  WORK_DIR="$(mktemp -d)"
  REPO_DIR="$WORK_DIR/repo"

  echo "=============================="
  echo "ID=$ID  Agent=$JOB_AGENT  Repo=$REPO_ID  Port=$PORT"
  echo "=============================="

  trap "rm -rf '$WORK_DIR'" EXIT

  # ------------------------------------------------------------------
  # 1) Clone
  # ------------------------------------------------------------------
  echo "[cwv] Cloning $REPO_ID ..."
  if ! git clone "https://github.com/${REPO_ID}.git" "$REPO_DIR" >/dev/null 2>&1; then
    echo "ERROR: git clone failed (ID=$ID  Repo=$REPO_ID)"
    return 1
  fi

  # ------------------------------------------------------------------
  # 2) Checkout pinned commit
  # ------------------------------------------------------------------
  local COMMIT_CLEAN="${COMMIT_ID:-}"
  [[ "$COMMIT_CLEAN" == " " ]] && COMMIT_CLEAN=""
  if [[ -n "$COMMIT_CLEAN" && "$COMMIT_CLEAN" != "null" ]]; then
    echo "[cwv] Checking out $COMMIT_CLEAN ..."
    if ! git -C "$REPO_DIR" checkout "$COMMIT_CLEAN" >/dev/null 2>&1; then
      echo "ERROR: git checkout failed (ID=$ID  commit=$COMMIT_CLEAN)"
      return 1
    fi
  fi

  # ------------------------------------------------------------------
  # 3) Apply patch (if present)
  # ------------------------------------------------------------------
  local PATCH_FILE="$JOB_PATCHES_DIR/${JOB_LABEL}.patch"
  if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
    echo "[cwv] Applying patch: $(basename "$PATCH_FILE")"
    if ! git -C "$REPO_DIR" apply "$PATCH_FILE" >/dev/null 2>&1; then
      echo "WARN: git apply failed — measuring unpatched baseline"
    fi
  else
    echo "[cwv] No patch at $(basename "$PATCH_FILE") — measuring unpatched baseline"
  fi

  # ------------------------------------------------------------------
  # 4) Start local HTTP server
  # ------------------------------------------------------------------
  # Wait until the port is confirmed free (handles TIME_WAIT from prior runs)
  local _wait_i
  for _wait_i in $(seq 1 30); do
    if ! lsof -ti tcp:"$PORT" >/dev/null 2>&1; then break; fi
    echo "[cwv] Port $PORT still in use (TIME_WAIT?), waiting... (${_wait_i}s)"
    sleep 2
  done

  local HOST_LOG="$JOB_OUT_DIR/${JOB_LABEL}_host.log"
  echo "[cwv] Starting HTTP server on port $PORT ..."
  PORT="$PORT" bash "$SCRIPT_DIR/$HOST_FILE_PATH" "$REPO_DIR" "$HOST_LOG" &
  local HOST_PID=$!

  if ! wait_for_server "$PORT" 90; then
    echo "ERROR: Server never became ready (ID=$ID)"
    kill "$HOST_PID" 2>/dev/null || true
    return 1
  fi

  # ------------------------------------------------------------------
  # 5) Measure CWV — mobile then desktop
  # ------------------------------------------------------------------
  local LOCAL_URL="http://localhost:${PORT}"
  local RESULT_MOBILE="$JOB_OUT_DIR/${JOB_LABEL}_mobile.json"
  local RESULT_DESKTOP="$JOB_OUT_DIR/${JOB_LABEL}_desktop.json"
  local CWV_STDERR="$JOB_OUT_DIR/${JOB_LABEL}_cwv_stderr.txt"

  echo "[cwv] Measuring CWV (mobile,  $NUM_RUNS runs) at $LOCAL_URL ..."
  python3 "$CWV_SCRIPT" --device mobile  --num-runs "$NUM_RUNS" --url "$LOCAL_URL" \
    2>>"$CWV_STDERR" | extract_json > "$RESULT_MOBILE" || true

  echo "[cwv] Measuring CWV (desktop, $NUM_RUNS runs) at $LOCAL_URL ..."
  python3 "$CWV_SCRIPT" --device desktop --num-runs "$NUM_RUNS" --url "$LOCAL_URL" \
    2>>"$CWV_STDERR" | extract_json > "$RESULT_DESKTOP" || true

  # ------------------------------------------------------------------
  # 6) Teardown
  # ------------------------------------------------------------------
  kill "$HOST_PID" 2>/dev/null || true
  wait "$HOST_PID" 2>/dev/null || true
  # Force-kill any child processes still holding the port
  # (Node/Ruby servers often spawn children that outlive the parent PID)
  lsof -ti tcp:"$PORT" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
  trap - EXIT
  rm -rf "$WORK_DIR"

  echo "[cwv] RESULT_MOBILE=$RESULT_MOBILE"
  echo "[cwv] RESULT_DESKTOP=$RESULT_DESKTOP"
  echo "✓ Done: ID=$ID  Agent=$JOB_AGENT"
}

# =============================================================================
# Parallel job pool
# Each job gets a unique, never-reused port to avoid TCP TIME_WAIT collisions.
# _SLOT   — output: an available concurrency slot (0..PARALLEL-1), used only
#            for tracking live job count; NOT used for port assignment.
# _PORT   — output: the unique port for this job (BASE_PORT + monotonic counter)
# =============================================================================
declare -A JOB_PIDS=()   # pid -> 1 (just tracks live count)
_SLOT=0
_PORT=$BASE_PORT
_PORT_COUNTER=0

acquire_slot() {
  while true; do
    local pid count
    count=${#JOB_PIDS[@]}
    # Reap finished jobs
    for pid in "${!JOB_PIDS[@]}"; do
      if ! kill -0 "$pid" 2>/dev/null; then
        unset "JOB_PIDS[$pid]"
        count=$(( count - 1 ))
      fi
    done
    if [[ $count -lt $PARALLEL ]]; then
      _PORT=$(( BASE_PORT + _PORT_COUNTER ))
      _PORT_COUNTER=$(( _PORT_COUNTER + 1 ))
      _SLOT=0   # slot is unused for port; kept for API compat
      return 0
    fi
    sleep 0.5
  done
}

# =============================================================================
# Read CSV rows once into a temp file (reused for each agent batch)
# =============================================================================
CSV_ROWS_FILE="$(mktemp)"
trap "rm -f '$CSV_ROWS_FILE'" EXIT

python3 - <<'PY' "$CSV" "$LIMIT" > "$CSV_ROWS_FILE"
import csv, sys
csv.field_size_limit(sys.maxsize)
csv_path = sys.argv[1]
limit_s  = sys.argv[2] if len(sys.argv) > 2 else ""
limit    = int(limit_s) if limit_s else None

n = 0
with open(csv_path, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        def _col(c):
            v = str(row.get(c) or "").replace("\t", " ").replace("\n", " ").strip()
            return v or " "
        id_val = _col("ID")
        if id_val == " " or not id_val.strip():
            continue   # skip blank/empty rows
        print("\t".join([
            id_val,
            _col("REPO_ID"),
            _col("COMMIT_ID"),
            _col("HOST_FILE_PATH"),
        ]))
        n += 1
        if limit is not None and n >= limit:
            break
PY

# =============================================================================
# Dispatch loop — one batch per (patches_dir, agent_name)
# =============================================================================
for batch_idx in "${!BATCH_PATCHES[@]}"; do
  batch_patches="${BATCH_PATCHES[$batch_idx]}"
  batch_agent="${BATCH_AGENTS[$batch_idx]}"
  batch_subdir="${BATCH_OUTDIRS[$batch_idx]}"

  if [[ -n "$batch_subdir" ]]; then
    batch_out="$OUTPUT_DIR/$batch_subdir"
  else
    batch_out="$OUTPUT_DIR"
  fi
  mkdir -p "$batch_out"

  echo ""
  echo "[cwv] ===== Batch: $batch_agent  (${batch_patches})  →  $batch_out ====="

  while IFS=$'\t' read -r ID REPO_ID COMMIT_ID HOST_FILE_PATH; do
    acquire_slot
    job_port=$_PORT
    (
      run_job "$ID" "$REPO_ID" "$COMMIT_ID" "$HOST_FILE_PATH" \
              "$job_port" "$batch_patches" "$batch_agent" "$batch_out"
    ) &
    JOB_PIDS[$!]=1
  done < "$CSV_ROWS_FILE"

  # Wait for this batch to finish before starting the next batch
  wait
done

echo ""
echo "[cwv] All batches complete.  Results in: $OUTPUT_DIR"
