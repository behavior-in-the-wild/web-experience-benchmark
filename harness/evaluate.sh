#!/usr/bin/env bash
set -euo pipefail

# =========================
# Parse arguments
# =========================
LIMIT=""
PARALLEL=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --limit N"; exit 1; }
      LIMIT="$1"
      shift
      ;;
    --parallel)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N"; exit 1; }
      PARALLEL="$1"
      shift
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# =========================
# Resolve paths
# =========================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
CSV="$SCRIPT_DIR/SAMPLE/input.csv"
TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv_debug.txt"

TMP_ROOT="$SCRIPT_DIR/out/${RUN_TS}/run"
RESULTS_DIR="$SCRIPT_DIR/out/${RUN_TS}/results"

CWV_SCRIPT="$SCRIPT_DIR/../scripts/helper_scripts/cwv_benchmark.py"
VISUAL_SCRIPT="$SCRIPT_DIR/visual_validate.py"
PSI_SCRIPT="$SCRIPT_DIR/psi_report.py"

# Save command-line overrides before .env (so DEVICE=desktop ./evaluate.sh wins over .env)
_OVERRIDE_DEVICE="${DEVICE:-}"
_OVERRIDE_PORT="${PORT:-}"
_OVERRIDE_NUM_RUNS="${NUM_RUNS:-}"

# =========================
# Load environment
# =========================
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Restore command-line overrides; then apply defaults for any still unset
[[ -n "$_OVERRIDE_DEVICE" ]] && DEVICE="$_OVERRIDE_DEVICE"
[[ -n "$_OVERRIDE_PORT" ]]   && PORT="$_OVERRIDE_PORT"
[[ -n "$_OVERRIDE_NUM_RUNS" ]] && NUM_RUNS="$_OVERRIDE_NUM_RUNS"
BASE_PORT="${PORT:-4000}"
export AZURE_DEPLOYMENT="gpt-4.1"
DEVICE="${DEVICE:-desktop}"     # mobile|desktop
NUM_RUNS="${NUM_RUNS:-5}"

# =========================
# Agents to benchmark
# =========================
AGENTS=(
  # "agents/template_null.sh"
  # "agents/template_codex.sh"   # requires: npm install -g @openai/codex
  # "agents/template_aider.sh"
  # "agents/template_opencode.sh"
  "agents/template_opencodegpt51codex.sh"
  # "agents/template_gemini.sh"
  # "agents/template_claudecode.sh"
  # "agents/template_cwvoptimizer.sh"
)

# =========================
# Sanity checks
# =========================
[[ -f "$CSV" ]]           || { echo "Missing CSV: $CSV"; exit 1; }
[[ -f "$TASK_SPEC" ]]     || { echo "Missing task spec: $TASK_SPEC"; exit 1; }
[[ -f "$CWV_SCRIPT" ]]    || { echo "Missing cwv_benchmark.py: $CWV_SCRIPT"; exit 1; }
[[ -f "$VISUAL_SCRIPT" ]] || { echo "Missing visual_validate.py: $VISUAL_SCRIPT"; exit 1; }
[[ -f "$PSI_SCRIPT" ]]    || { echo "Missing psi_report.py: $PSI_SCRIPT"; exit 1; }

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"
echo "[run] Input:    $CSV"
echo "[run] Output:   $RESULTS_DIR"
echo "[run] Parallel: $PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$LIMIT" ]] && echo "[run] LIMIT=$LIMIT"

# =========================
# Helpers
# =========================

# Wait up to TIMEOUT seconds for localhost:PORT to respond.
wait_for_server() {
  local port="$1"
  local timeout="${2:-90}"
  local i
  for i in $(seq 1 "$timeout"); do
    if curl -fs "http://localhost:${port}/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# Wait up to TIMEOUT seconds for bore to emit its public URL.
# Prints "http://bore.pub:PORT" on success; returns 1 on timeout.
# Accepts two bore output forms (from patch_bore_parallel.py BoreTunnel):
#   Form 1: "listening at bore.pub:PORT"
#   Form 2: "remote_port = PORT" or "remote_port=PORT"
wait_for_bore_url() {
  local log_file="$1"
  local timeout="${2:-30}"
  local i bore_port
  for i in $(seq 1 "$timeout"); do
    bore_port=$(grep -oP 'bore\.pub:\K\d+' "$log_file" 2>/dev/null | head -1 || true)
    if [[ -z "$bore_port" ]]; then
      bore_port=$(grep -oP 'remote_port\s*[=:]\s*\K\d+' "$log_file" 2>/dev/null | head -1 || true)
    fi
    if [[ -n "$bore_port" ]]; then
      echo "http://bore.pub:${bore_port}"
      return 0
    fi
    sleep 1
  done
  return 1
}

# =========================
# Per-job function
# =========================
# All globals set before the dispatch loop (SCRIPT_DIR, TMP_ROOT, RESULTS_DIR,
# TASK_SPEC, CWV_SCRIPT, VISUAL_SCRIPT, PSI_SCRIPT, NUM_RUNS, BASE_PORT,
# AZURE_DEPLOYMENT) are inherited by the subshell that runs this function.
run_job() {
  local ID="$1"
  local REPO_ID="$2"
  local FRAMEWORK="$3"
  local COMMIT_ID="$4"
  local HOST_FILE_PATH="$5"
  local CWV_MOBILE="$6"
  local CWV_DESKTOP="$7"
  local LCP_ENTRIES_DESKTOP="$8"
  local LCP_ENTRIES_MOBILE="$9"
  local CLS_SHIFTS_MOBILE="${10}"
  local CLS_SHIFTS_DESKTOP="${11}"
  local INP_INTERACTIONS_MOBILE="${12}"
  local INP_INTERACTIONS_DESKTOP="${13}"
  local AGENT="${14}"
  local SLOT="${15}"

  local AGENT_NAME PORT RUN_DIR REPO_DIR
  AGENT_NAME="$(basename "$AGENT" .sh)"
  PORT=$(( BASE_PORT + SLOT ))
  RUN_DIR="$TMP_ROOT/${ID}_${AGENT_NAME}"
  REPO_DIR="$RUN_DIR/repo"

  echo "======================================"
  echo "ID=$ID Repo=$REPO_ID Agent=$AGENT_NAME Slot=$SLOT Port=$PORT"
  echo "======================================"

  mkdir -p "$RUN_DIR" "$REPO_DIR"

  # -------------------------
  # 1) Clone repo fresh from GitHub
  # -------------------------
  echo "[run] Cloning $REPO_ID ..."
  if ! git clone "https://github.com/${REPO_ID}.git" "$REPO_DIR" >/dev/null 2>&1; then
    echo "ERROR: git clone failed (ID=$ID Repo=$REPO_ID)"
    rm -rf "$RUN_DIR"
    return 1
  fi

  # -------------------------
  # 2) Checkout pinned commit
  # -------------------------
  local COMMIT_ID_CLEAN="${COMMIT_ID:-}"
  [[ "$COMMIT_ID_CLEAN" == " " ]] && COMMIT_ID_CLEAN=""
  if [[ -n "$COMMIT_ID_CLEAN" && "$COMMIT_ID_CLEAN" != "null" ]]; then
    echo "[run] Checking out $COMMIT_ID_CLEAN ..."
    if ! git -C "$REPO_DIR" checkout "$COMMIT_ID_CLEAN" >/dev/null 2>&1; then
      echo "ERROR: git checkout $COMMIT_ID_CLEAN failed (ID=$ID)"
      rm -rf "$RUN_DIR"
      return 1
    fi
  fi

  # -------------------------
  # 3) Commit baseline so agent diff is unambiguous
  # -------------------------
  git -C "$REPO_DIR" add -A >/dev/null 2>&1 || true
  git -C "$REPO_DIR" commit -qm "baseline" >/dev/null 2>&1 || true

  # -------------------------
  # 4) Export context env vars (CSV baselines) for agent
  # -------------------------
  export FRAMEWORK="$(echo "${FRAMEWORK:-unknown}" | tr '[:upper:]' '[:lower:]')"
  export REPO_ID
  export CWV_BASELINE_MOBILE="${CWV_MOBILE:-}"
  export LCP_ENTRIES_MOBILE="${LCP_ENTRIES_MOBILE:-}"
  export CWV_BASELINE_DESKTOP="${CWV_DESKTOP:-}"
  export LCP_ENTRIES_DESKTOP="${LCP_ENTRIES_DESKTOP:-}"
  export CLS_SHIFTS_MOBILE="${CLS_SHIFTS_MOBILE:-}"
  export CLS_SHIFTS_DESKTOP="${CLS_SHIFTS_DESKTOP:-}"
  export INP_INTERACTIONS_MOBILE="${INP_INTERACTIONS_MOBILE:-}"
  export INP_INTERACTIONS_DESKTOP="${INP_INTERACTIONS_DESKTOP:-}"

  # -------------------------
  # 4b) Initial PSI measurement (baseline, before agent runs)
  # -------------------------
  if [[ "${SKIP_CWV_MEASURE:-0}" != "1" ]]; then
    local INIT_HOST_LOG INIT_BORE_LOG INIT_PSI_MOBILE INIT_PSI_DESKTOP INIT_HOST_PID INIT_BORE_PID
    INIT_HOST_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_init_host.log"
    INIT_BORE_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_init_bore.log"
    INIT_PSI_MOBILE="$RESULTS_DIR/${ID}_${AGENT_NAME}_init_psi_mobile.json"
    INIT_PSI_DESKTOP="$RESULTS_DIR/${ID}_${AGENT_NAME}_init_psi_desktop.json"

    echo "[run] Starting baseline HTTP server on port $PORT ..."
    PORT="$PORT" bash "$SCRIPT_DIR/$HOST_FILE_PATH" "$REPO_DIR" "$INIT_HOST_LOG" &
    INIT_HOST_PID=$!

    if wait_for_server "$PORT" 90; then
      echo "[run] Starting baseline bore tunnel ..."
      RUST_LOG=info bore local "$PORT" --to bore.pub > "$INIT_BORE_LOG" 2>&1 &
      INIT_BORE_PID=$!

      local BORE_URL_INIT=""
      BORE_URL_INIT=$(wait_for_bore_url "$INIT_BORE_LOG" 30) || BORE_URL_INIT=""

      if [[ -n "$BORE_URL_INIT" ]]; then
        echo "[run] Baseline bore URL: $BORE_URL_INIT"
        echo "[run] Running initial PSI (mobile) ..."
        python3 "$PSI_SCRIPT" --url "$BORE_URL_INIT" --strategy mobile  --output "$INIT_PSI_MOBILE"  || true
        echo "[run] Running initial PSI (desktop) ..."
        python3 "$PSI_SCRIPT" --url "$BORE_URL_INIT" --strategy desktop --output "$INIT_PSI_DESKTOP" || true
      else
        echo "[run] WARN: bore tunnel did not come up for baseline PSI (ID=$ID) — skipping"
      fi

      kill "$INIT_BORE_PID" 2>/dev/null || true
      wait "$INIT_BORE_PID" 2>/dev/null || true
    else
      echo "[run] WARN: Baseline server never became ready (ID=$ID) — skipping initial PSI"
    fi

    kill "$INIT_HOST_PID" 2>/dev/null || true
    wait "$INIT_HOST_PID" 2>/dev/null || true
  fi

  # -------------------------
  # 5) Run agent
  # -------------------------
  local AGENT_LOG PATCH_FILE
  AGENT_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_agent.log"
  PATCH_FILE="$RESULTS_DIR/${ID}_${AGENT_NAME}.patch"

  bash "$SCRIPT_DIR/$AGENT" \
    "$REPO_DIR" \
    "$TASK_SPEC" \
    "$AGENT_LOG" \
    "$PATCH_FILE" \
    </dev/null \
    || echo "[agent] Agent failed (continuing)"

  # -------------------------
  # 6) Normalize patch (reset to baseline + apply patch only)
  # -------------------------
  if [[ -d "$REPO_DIR/.git" ]]; then
    (
      set +e
      cd "$REPO_DIR" || exit 0

      # If agent didn't write patch, capture diff
      if [[ ! -s "$PATCH_FILE" ]]; then
        git add -A >/dev/null 2>&1
        git diff --cached > "$PATCH_FILE" 2>/dev/null || true
      fi

      git reset --hard HEAD >/dev/null 2>&1 || true
      git clean -fd >/dev/null 2>&1 || true

      [[ -s "$PATCH_FILE" ]] && git apply "$PATCH_FILE" >/dev/null 2>&1 || true
    )
  fi

  # Skip measurement phases if requested
  if [[ "${SKIP_CWV_MEASURE:-0}" == "1" ]]; then
    echo "[run] SKIP_CWV_MEASURE=1; skipping measurement for ID=$ID Agent=$AGENT_NAME"
    rm -rf "$RUN_DIR"
    echo "✓ Done: ID=$ID Agent=$AGENT_NAME"
    return 0
  fi

  # -------------------------
  # 7) Launch final HTTP server (patched repo)
  # -------------------------
  local HOST_LOG HOST_PID
  HOST_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_host.log"
  PORT="$PORT" bash "$SCRIPT_DIR/$HOST_FILE_PATH" "$REPO_DIR" "$HOST_LOG" &
  HOST_PID=$!

  if ! wait_for_server "$PORT" 90; then
    echo "ERROR: Patched site never became ready (ID=$ID Agent=$AGENT_NAME)"
    tail -n 50 "$HOST_LOG" 2>/dev/null || true
    kill "$HOST_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    return 1
  fi

  # -------------------------
  # 7b) Open final bore tunnel
  # -------------------------
  local BORE_LOG BORE_PID BORE_URL_FINAL
  BORE_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_bore.log"
  RUST_LOG=info bore local "$PORT" --to bore.pub > "$BORE_LOG" 2>&1 &
  BORE_PID=$!

  BORE_URL_FINAL=$(wait_for_bore_url "$BORE_LOG" 30) || BORE_URL_FINAL=""

  if [[ -z "$BORE_URL_FINAL" ]]; then
    echo "ERROR: bore tunnel did not come up for final measurement (ID=$ID Agent=$AGENT_NAME)"
    kill "$HOST_PID" "$BORE_PID" 2>/dev/null || true
    wait "$HOST_PID" "$BORE_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    return 1
  fi

  echo "[run] Final bore URL: $BORE_URL_FINAL"

  # -------------------------
  # 8) Final PSI measurement (post-patch)
  # -------------------------
  local FINAL_PSI_MOBILE FINAL_PSI_DESKTOP
  FINAL_PSI_MOBILE="$RESULTS_DIR/${ID}_${AGENT_NAME}_final_psi_mobile.json"
  FINAL_PSI_DESKTOP="$RESULTS_DIR/${ID}_${AGENT_NAME}_final_psi_desktop.json"

  echo "[run] Running final PSI (mobile) ..."
  python3 "$PSI_SCRIPT" --url "$BORE_URL_FINAL" --strategy mobile  --output "$FINAL_PSI_MOBILE"  || true
  echo "[run] Running final PSI (desktop) ..."
  python3 "$PSI_SCRIPT" --url "$BORE_URL_FINAL" --strategy desktop --output "$FINAL_PSI_DESKTOP" || true

  # -------------------------
  # 9) Measure CWV (post-patch) — mobile and desktop
  # -------------------------
  local RESULT_MOBILE RESULT_DESKTOP CWV_STDERR
  RESULT_MOBILE="$RESULTS_DIR/${ID}_${AGENT_NAME}_mobile.json"
  RESULT_DESKTOP="$RESULTS_DIR/${ID}_${AGENT_NAME}_desktop.json"
  CWV_STDERR="$RESULTS_DIR/${ID}_${AGENT_NAME}_cwv_stderr.txt"

  python3 "$CWV_SCRIPT" --device mobile  --num-runs "$NUM_RUNS" --url "$BORE_URL_FINAL" \
    > "$RESULT_MOBILE"  2>> "$CWV_STDERR" || true
  python3 "$CWV_SCRIPT" --device desktop --num-runs "$NUM_RUNS" --url "$BORE_URL_FINAL" \
    > "$RESULT_DESKTOP" 2>> "$CWV_STDERR" || true

  echo "RESULT_MOBILE=$RESULT_MOBILE"
  echo "RESULT_DESKTOP=$RESULT_DESKTOP"

  # -------------------------
  # 9b) Visual validation (screenshot + AI eval)
  # -------------------------
  local SCREENSHOT_PATH VISUAL_JSON
  SCREENSHOT_PATH="$RESULTS_DIR/${ID}_${AGENT_NAME}_screenshot.png"
  VISUAL_JSON="$RESULTS_DIR/${ID}_${AGENT_NAME}_visual.json"
  python3 "$VISUAL_SCRIPT" \
    --url "$BORE_URL_FINAL" \
    --screenshot-path "$SCREENSHOT_PATH" \
    --repo-id "$REPO_ID" \
    --output-json "$VISUAL_JSON" \
    || echo "[visual] Validation failed (continuing)"

  # -------------------------
  # 10) Teardown
  # -------------------------
  kill "$HOST_PID" "$BORE_PID" 2>/dev/null || true
  wait "$HOST_PID" "$BORE_PID" 2>/dev/null || true
  rm -rf "$RUN_DIR"

  echo "✓ Done: ID=$ID Agent=$AGENT_NAME"
}

# =========================
# Job pool (slot tracking)
# =========================
declare -A JOB_SLOT=()   # pid -> slot index (0..PARALLEL-1)
_SLOT=0               # output variable for acquire_slot (avoids subshell)

# Block until a slot is available; sets _SLOT to the acquired index.
# Must be called directly (not via $()) so it can modify JOB_SLOT in-place.
# Polls every 0.5 s; reaps finished jobs to reclaim their slots.
acquire_slot() {
  while true; do
    local pid count s used p
    count=${#JOB_SLOT[@]}
    # Reap any finished jobs
    if [[ $count -gt 0 ]]; then
      for pid in "${!JOB_SLOT[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
          _SLOT="${JOB_SLOT[$pid]}"
          unset "JOB_SLOT[$pid]"
          return 0
        fi
      done
    fi
    # If under capacity, find the first unused slot
    if [[ $count -lt $PARALLEL ]]; then
      for s in $(seq 0 $((PARALLEL - 1))); do
        used=0
        if [[ $count -gt 0 ]]; then
          for p in "${!JOB_SLOT[@]}"; do
            [[ "${JOB_SLOT[$p]}" == "$s" ]] && used=1 && break
          done
        fi
        if [[ $used -eq 0 ]]; then
          _SLOT="$s"
          return 0
        fi
      done
    fi
    sleep 0.5
  done
}

# =========================
# Dispatch loop
# =========================
while IFS=$'\t' read -r \
  ID REPO_ID FRAMEWORK COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH \
  CWV_MOBILE CWV_DESKTOP LCP_ENTRIES_DESKTOP LCP_ENTRIES_MOBILE \
  CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP
do
  for AGENT in "${AGENTS[@]}"; do
    acquire_slot          # sets _SLOT; modifies JOB_SLOT in the parent shell
    slot=$_SLOT
    (
      run_job \
        "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" \
        "$CWV_MOBILE" "$CWV_DESKTOP" "$LCP_ENTRIES_DESKTOP" "$LCP_ENTRIES_MOBILE" \
        "$CLS_SHIFTS_MOBILE" "$CLS_SHIFTS_DESKTOP" "$INP_INTERACTIONS_MOBILE" "$INP_INTERACTIONS_DESKTOP" \
        "$AGENT" "$slot"
    ) &
    JOB_SLOT[$!]=$slot
  done
done < <(python3 - <<'PY' "$CSV" "$LIMIT"
import csv, sys
csv_path = sys.argv[1]
limit_s = sys.argv[2] if len(sys.argv) > 2 else ""
limit = int(limit_s) if limit_s else None
cols = [
  "ID","REPO_ID","FRAMEWORK","COMMIT_ID","ZIP_REPO_PATH","HOST_FILE_PATH",
  "CWV_MOBILE","CWV_DESKTOP","LCP_ENTRIES_DESKTOP","LCP_ENTRIES_MOBILE",
  "CLS_SHIFTS_MOBILE","CLS_SHIFTS_DESKTOP","INP_INTERACTIONS_MOBILE","INP_INTERACTIONS_DESKTOP"
]
n = 0
with open(csv_path, newline="", encoding="utf-8") as f:
  r = csv.DictReader(f)
  for row in r:
    out = []
    for c in cols:
      v = row.get(c, "")
      if v is None:
        v = ""
      v = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
      # Use placeholder for empty to avoid consecutive tabs (bash coalesces "\t\t")
      if v == "":
        v = " "
      out.append(v)
    print("\t".join(out))
    n += 1
    if limit is not None and n >= limit:
      break
PY
)

# Wait for all remaining background jobs to finish
wait
echo "[run] All jobs complete."
