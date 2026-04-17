#!/usr/bin/env bash
set -euo pipefail

# =========================
# Parse arguments
# =========================
# Skip flags (also overridable via env before invoking):
#   SKIP_CWV, SKIP_INIT_PSI, SKIP_FINAL_PSI, SKIP_VISUAL, SKIP_CWV_MEASURE
# CLI: --skip-cwv, --skip-init-psi, --skip-final-psi, --skip-visual, --skip-cwv-measure
LIMIT=""
PARALLEL=1
_OVERRIDE_SUGGESTIONS_FILE=""
_OVERRIDE_SUGGESTION_INDICES=""
SKIP_CWV="${SKIP_CWV:-0}"
SKIP_INIT_PSI="${SKIP_INIT_PSI:-0}"
SKIP_FINAL_PSI="${SKIP_FINAL_PSI:-0}"
SKIP_VISUAL="${SKIP_VISUAL:-0}"
SKIP_CWV_MEASURE="${SKIP_CWV_MEASURE:-0}"

usage() {
  cat <<'EOF'
Usage: evaluate.sh [options]

Options:
  --limit N              Process only the first N CSV rows
  --parallel N           Max concurrent jobs (default: 1)
  --skip-cwv             Skip CWV benchmark runs (post-patch; PageSpeed still runs unless skipped)
  --skip-init-psi        Skip baseline PSI (before agent)
  --skip-final-psi       Skip final PSI (after patch)
  --skip-visual          Skip screenshot + AI visual validation
  --skip-cwv-measure     Skip all measurement after the agent (no host/bore/PSI/CWV/visual)
  --suggestions-file F   Optional. JSON with top-level "suggestions" array (see harness/out/suggestions/…).
                         Omit this flag (and SUGGESTIONS_FILE) for the original behavior: one full run
                         per CSV row per agent, with no suggestion injection.
  --suggestion-indices L Optional; only used with --suggestions-file. Comma-separated 0-based indices
                         (default: all suggestions). Example: 0,2,4
  --help, -h             Show this message

Environment:
  CSV                    Input CSV path (default: SAMPLE/input.csv)
  SUGGESTIONS_FILE       Optional; same as --suggestions-file (CLI wins if both are set)
  SUGGESTION_INDICES     Optional; same as --suggestion-indices (CLI wins if both are set)
  SKIP_*                 Same behavior as the matching --skip-* flags (0|1)

When SUGGESTIONS_FILE is set and the file exists, each CSV row is evaluated once per selected
suggestion index. Each such run writes that suggestion to eval_suggestion.json, exports
EVAL_SUGGESTION_FILE for the agent, and prefixes result artifacts with  ID_s<N>_AGENT  instead of  ID_AGENT .
EOF
}

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
    --skip-cwv)
      SKIP_CWV=1
      shift
      ;;
    --skip-init-psi)
      SKIP_INIT_PSI=1
      shift
      ;;
    --skip-final-psi)
      SKIP_FINAL_PSI=1
      shift
      ;;
    --skip-visual)
      SKIP_VISUAL=1
      shift
      ;;
    --skip-cwv-measure)
      SKIP_CWV_MEASURE=1
      shift
      ;;
    --suggestions-file)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --suggestions-file PATH"; exit 1; }
      _OVERRIDE_SUGGESTIONS_FILE="$1"
      shift
      ;;
    --suggestion-indices)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --suggestion-indices 0,1,…"; exit 1; }
      _OVERRIDE_SUGGESTION_INDICES="$1"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done

# =========================
# Resolve paths
# =========================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
CSV="${CSV:-$SCRIPT_DIR/SAMPLE/input.csv}"
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
# CLI --suggestions-file / --suggestion-indices win; else env before .env wins over .env
[[ -z "$_OVERRIDE_SUGGESTIONS_FILE" ]] && _OVERRIDE_SUGGESTIONS_FILE="${SUGGESTIONS_FILE:-}"
[[ -z "$_OVERRIDE_SUGGESTION_INDICES" ]] && _OVERRIDE_SUGGESTION_INDICES="${SUGGESTION_INDICES:-}"

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
SUGGESTIONS_FILE="${_OVERRIDE_SUGGESTIONS_FILE:-${SUGGESTIONS_FILE:-}}"
SUGGESTION_INDICES="${_OVERRIDE_SUGGESTION_INDICES:-${SUGGESTION_INDICES:-}}"
# Whitespace-only counts as unset → same as legacy harness (no suggestions).
_s="${SUGGESTIONS_FILE:-}"
_s="${_s#"${_s%%[![:space:]]*}"}"
_s="${_s%"${_s##*[![:space:]]}"}"
SUGGESTIONS_FILE="$_s"
_s="${SUGGESTION_INDICES:-}"
_s="${_s#"${_s%%[![:space:]]*}"}"
_s="${_s%"${_s##*[![:space:]]}"}"
SUGGESTION_INDICES="$_s"
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
  # "agents/template_cwvoptimizer.sh"
  # "agents/template_opencodegpt41.sh"
  # "agents/template_claudecode.sh"
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
[[ -n "${SUGGESTIONS_FILE:-}" ]] && echo "[run] Suggestions file: $SUGGESTIONS_FILE  indices=${SUGGESTION_INDICES:-all}"
[[ "$SKIP_CWV" == "1" ]]              && echo "[run] --skip-cwv"
[[ "$SKIP_INIT_PSI" == "1" ]]         && echo "[run] --skip-init-psi"
[[ "$SKIP_FINAL_PSI" == "1" ]]        && echo "[run] --skip-final-psi"
[[ "$SKIP_VISUAL" == "1" ]]           && echo "[run] --skip-visual"
[[ "$SKIP_CWV_MEASURE" == "1" ]]      && echo "[run] --skip-cwv-measure"

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
# Optional args 16–17: suggestions JSON path + 0-based index (see --suggestions-file).
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
  local SUGG_FILE_RAW="${16:- }"
  local SUGG_IDX_RAW="${17:- }"

  local AGENT_NAME PORT RUN_DIR REPO_DIR JOB_LABEL
  AGENT_NAME="$(basename "$AGENT" .sh)"
  PORT=$(( BASE_PORT + SLOT ))
  [[ "$SUGG_FILE_RAW" == " " ]] && SUGG_FILE_RAW=""
  [[ "$SUGG_IDX_RAW" == " " ]] && SUGG_IDX_RAW=""
  if [[ -n "$SUGG_FILE_RAW" && -n "$SUGG_IDX_RAW" ]]; then
    JOB_LABEL="${ID}_s${SUGG_IDX_RAW}_${AGENT_NAME}"
  else
    JOB_LABEL="${ID}_${AGENT_NAME}"
  fi
  RUN_DIR="$TMP_ROOT/${JOB_LABEL}"
  REPO_DIR="$RUN_DIR/repo"

  echo "======================================"
  if [[ -n "$SUGG_IDX_RAW" ]]; then
    echo "ID=$ID Repo=$REPO_ID Agent=$AGENT_NAME SuggestionIndex=$SUGG_IDX_RAW Slot=$SLOT Port=$PORT"
  else
    echo "ID=$ID Repo=$REPO_ID Agent=$AGENT_NAME Slot=$SLOT Port=$PORT"
  fi
  echo "======================================"

  mkdir -p "$RUN_DIR" "$REPO_DIR"

  if [[ -n "$SUGG_FILE_RAW" && -n "$SUGG_IDX_RAW" ]]; then
    python3 - "$SUGG_FILE_RAW" "$SUGG_IDX_RAW" "$RUN_DIR/eval_suggestion.json" <<'PY'
import json, sys
src, idx_s, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(src, encoding="utf-8") as f:
    data = json.load(f)
sugs = data.get("suggestions", [])
if not (0 <= idx_s < len(sugs)):
    print("ERROR: suggestion index out of range for extract", file=sys.stderr)
    sys.exit(1)
obj = sugs[idx_s]
with open(out, "w", encoding="utf-8") as o:
    json.dump(obj, o, indent=2, ensure_ascii=False)
PY
    cp "$RUN_DIR/eval_suggestion.json" "$RESULTS_DIR/${JOB_LABEL}_input_suggestion.json"
    export EVAL_SUGGESTION_FILE="$RUN_DIR/eval_suggestion.json"
    export EVAL_SUGGESTION_INDEX="$SUGG_IDX_RAW"
  else
    unset EVAL_SUGGESTION_FILE EVAL_SUGGESTION_INDEX
  fi

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
  if [[ "${SKIP_CWV_MEASURE:-0}" != "1" && "${SKIP_INIT_PSI:-0}" != "1" ]]; then
    local INIT_HOST_LOG INIT_BORE_LOG INIT_PSI_MOBILE INIT_PSI_DESKTOP INIT_HOST_PID INIT_BORE_PID
    INIT_HOST_LOG="$RESULTS_DIR/${JOB_LABEL}_init_host.log"
    INIT_BORE_LOG="$RESULTS_DIR/${JOB_LABEL}_init_bore.log"
    INIT_PSI_MOBILE="$RESULTS_DIR/${JOB_LABEL}_init_psi_mobile.json"
    INIT_PSI_DESKTOP="$RESULTS_DIR/${JOB_LABEL}_init_psi_desktop.json"

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
  local AGENT_LOG PATCH_FILE USAGE_JSON
  AGENT_LOG="$RESULTS_DIR/${JOB_LABEL}_agent.log"
  PATCH_FILE="$RESULTS_DIR/${JOB_LABEL}.patch"
  USAGE_JSON="$RESULTS_DIR/${JOB_LABEL}_usage.json"

  local _AGENT_T0=$SECONDS
  bash "$SCRIPT_DIR/$AGENT" \
    "$REPO_DIR" \
    "$TASK_SPEC" \
    "$AGENT_LOG" \
    "$PATCH_FILE" \
    </dev/null \
    || echo "[agent] Agent failed (continuing)"
  local _AGENT_WALL=$(( SECONDS - _AGENT_T0 ))

  # Merge wall clock time into usage JSON (agent writes cost/tokens/tool_calls there)
  if [[ -f "$USAGE_JSON" ]]; then
    python3 -c "
import json
with open('$USAGE_JSON') as f:
    d = json.load(f)
d['wall_clock_seconds'] = $_AGENT_WALL
with open('$USAGE_JSON', 'w') as f:
    json.dump(d, f, indent=2)
" 2>/dev/null || true
  else
    echo "{\"wall_clock_seconds\": $_AGENT_WALL}" > "$USAGE_JSON"
  fi
  echo "[run] Agent wall time: ${_AGENT_WALL}s (ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW})"

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
    echo "[run] SKIP_CWV_MEASURE=1; skipping measurement for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
    rm -rf "$RUN_DIR"
    echo "✓ Done: ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
    return 0
  fi

  # -------------------------
  # 7) Launch final HTTP server (patched repo)
  # -------------------------
  local HOST_LOG HOST_PID
  HOST_LOG="$RESULTS_DIR/${JOB_LABEL}_host.log"
  PORT="$PORT" bash "$SCRIPT_DIR/$HOST_FILE_PATH" "$REPO_DIR" "$HOST_LOG" &
  HOST_PID=$!

  if ! wait_for_server "$PORT" 90; then
    echo "ERROR: Patched site never became ready (ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW})"
    tail -n 50 "$HOST_LOG" 2>/dev/null || true
    kill "$HOST_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    return 1
  fi

  # -------------------------
  # 7b) Open final bore tunnel
  # -------------------------
  local BORE_LOG BORE_PID BORE_URL_FINAL
  BORE_LOG="$RESULTS_DIR/${JOB_LABEL}_bore.log"
  RUST_LOG=info bore local "$PORT" --to bore.pub > "$BORE_LOG" 2>&1 &
  BORE_PID=$!

  BORE_URL_FINAL=$(wait_for_bore_url "$BORE_LOG" 30) || BORE_URL_FINAL=""

  if [[ -z "$BORE_URL_FINAL" ]]; then
    echo "ERROR: bore tunnel did not come up for final measurement (ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW})"
    kill "$HOST_PID" "$BORE_PID" 2>/dev/null || true
    wait "$HOST_PID" "$BORE_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    return 1
  fi

  echo "[run] Final bore URL: $BORE_URL_FINAL"

  # -------------------------
  # 8) Final PSI measurement (post-patch)
  # -------------------------
  if [[ "${SKIP_FINAL_PSI:-0}" == "1" ]]; then
    echo "[run] --skip-final-psi set; skipping final PSI for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
  else
    local FINAL_PSI_MOBILE FINAL_PSI_DESKTOP
    FINAL_PSI_MOBILE="$RESULTS_DIR/${JOB_LABEL}_final_psi_mobile.json"
    FINAL_PSI_DESKTOP="$RESULTS_DIR/${JOB_LABEL}_final_psi_desktop.json"

    echo "[run] Running final PSI (mobile) ..."
    python3 "$PSI_SCRIPT" --url "$BORE_URL_FINAL" --strategy mobile  --output "$FINAL_PSI_MOBILE"  || true
    echo "[run] Running final PSI (desktop) ..."
    python3 "$PSI_SCRIPT" --url "$BORE_URL_FINAL" --strategy desktop --output "$FINAL_PSI_DESKTOP" || true
  fi

  # -------------------------
  # 9) Measure CWV (post-patch) — mobile and desktop
  # -------------------------
  if [[ "${SKIP_CWV:-0}" == "1" ]]; then
    echo "[run] --skip-cwv set; skipping CWV measurement for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
  else
    local RESULT_MOBILE RESULT_DESKTOP CWV_STDERR
    RESULT_MOBILE="$RESULTS_DIR/${JOB_LABEL}_mobile.json"
    RESULT_DESKTOP="$RESULTS_DIR/${JOB_LABEL}_desktop.json"
    CWV_STDERR="$RESULTS_DIR/${JOB_LABEL}_cwv_stderr.txt"

    python3 "$CWV_SCRIPT" --device mobile  --num-runs "$NUM_RUNS" --url "$BORE_URL_FINAL" \
      > "$RESULT_MOBILE"  2>> "$CWV_STDERR" || true
    python3 "$CWV_SCRIPT" --device desktop --num-runs "$NUM_RUNS" --url "$BORE_URL_FINAL" \
      > "$RESULT_DESKTOP" 2>> "$CWV_STDERR" || true

    echo "RESULT_MOBILE=$RESULT_MOBILE"
    echo "RESULT_DESKTOP=$RESULT_DESKTOP"
  fi

  # -------------------------
  # 9b) Visual validation (screenshot + AI eval)
  # -------------------------
  if [[ "${SKIP_VISUAL:-0}" == "1" ]]; then
    echo "[run] --skip-visual set; skipping visual validation for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
  else
    local SCREENSHOT_PATH VISUAL_JSON
    SCREENSHOT_PATH="$RESULTS_DIR/${JOB_LABEL}_screenshot.png"
    VISUAL_JSON="$RESULTS_DIR/${JOB_LABEL}_visual.json"
    python3 "$VISUAL_SCRIPT" \
      --url "$BORE_URL_FINAL" \
      --screenshot-path "$SCREENSHOT_PATH" \
      --repo-id "$REPO_ID" \
      --output-json "$VISUAL_JSON" \
      || echo "[visual] Validation failed (continuing)"
  fi

  # -------------------------
  # 10) Teardown
  # -------------------------
  kill "$HOST_PID" "$BORE_PID" 2>/dev/null || true
  wait "$HOST_PID" "$BORE_PID" 2>/dev/null || true
  rm -rf "$RUN_DIR"

  echo "✓ Done: ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
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
# When SUGGESTIONS_FILE is empty, Python emits one line per CSV row with placeholder suggestion
# columns → same job count and artifact names (ID_AGENT) as before the suggestions feature.
while IFS=$'\t' read -r \
  ID REPO_ID FRAMEWORK COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH \
  CWV_MOBILE CWV_DESKTOP LCP_ENTRIES_DESKTOP LCP_ENTRIES_MOBILE \
  CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP \
  SUGG_PATH SUGG_IDX
do
  for AGENT in "${AGENTS[@]}"; do
    acquire_slot          # sets _SLOT; modifies JOB_SLOT in the parent shell
    slot=$_SLOT
    (
      run_job \
        "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" \
        "$CWV_MOBILE" "$CWV_DESKTOP" "$LCP_ENTRIES_DESKTOP" "$LCP_ENTRIES_MOBILE" \
        "$CLS_SHIFTS_MOBILE" "$CLS_SHIFTS_DESKTOP" "$INP_INTERACTIONS_MOBILE" "$INP_INTERACTIONS_DESKTOP" \
        "$AGENT" "$slot" \
        "$SUGG_PATH" "$SUGG_IDX"
    ) &
    JOB_SLOT[$!]=$slot
  done
done < <(python3 - <<'PY' "$CSV" "$LIMIT" "${SUGGESTIONS_FILE:-}" "${SUGGESTION_INDICES:-}"
import csv, json, os, sys

csv.field_size_limit(sys.maxsize)
csv_path = sys.argv[1]
limit_s = sys.argv[2] if len(sys.argv) > 2 else ""
sug_path = (sys.argv[3] if len(sys.argv) > 3 else "").strip()
indices_raw = (sys.argv[4] if len(sys.argv) > 4 else "").strip()
limit = int(limit_s) if limit_s else None

cols = [
  "ID","REPO_ID","FRAMEWORK","COMMIT_ID","ZIP_REPO_PATH","HOST_FILE_PATH",
  "CWV_MOBILE","CWV_DESKTOP","LCP_ENTRIES_DESKTOP","LCP_ENTRIES_MOBILE",
  "CLS_SHIFTS_MOBILE","CLS_SHIFTS_DESKTOP","INP_INTERACTIONS_MOBILE","INP_INTERACTIONS_DESKTOP",
]

def row_tuple(row):
  out = []
  for c in cols:
    v = row.get(c, "")
    if v is None:
      v = ""
    v = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    if v == "":
      v = " "
    out.append(v)
  return out

sug_abs = ""
sug_indices = []
if sug_path:
  if not os.path.isfile(sug_path):
    print(f"ERROR: SUGGESTIONS_FILE not found: {sug_path}", file=sys.stderr)
    sys.exit(1)
  sug_abs = os.path.abspath(sug_path)
  with open(sug_abs, encoding="utf-8") as f:
    data = json.load(f)
  sugs = data.get("suggestions", [])
  if not sugs:
    print("ERROR: JSON has no entries in 'suggestions'", file=sys.stderr)
    sys.exit(1)
  if indices_raw:
    for part in indices_raw.split(","):
      part = part.strip()
      if not part:
        continue
      sug_indices.append(int(part))
  else:
    sug_indices = list(range(len(sugs)))
  for i in sug_indices:
    if i < 0 or i >= len(sugs):
      print(f"ERROR: suggestion index {i} out of range (0..{len(sugs)-1})", file=sys.stderr)
      sys.exit(1)

n = 0
with open(csv_path, newline="", encoding="utf-8") as f:
  r = csv.DictReader(f)
  for row in r:
    base = row_tuple(row)
    if sug_abs:
      for idx in sug_indices:
        print("\t".join(base + [sug_abs, str(idx)]))
    else:
      print("\t".join(base + [" ", " "]))
    n += 1
    if limit is not None and n >= limit:
      break
PY
)

# Wait for all remaining background jobs to finish
wait
echo "[run] All jobs complete."
