#!/usr/bin/env bash
# row_eval_lib.sh — shared helper functions for row-wise CWV/visual evaluation scripts.
#
# Source this file near the top of each run_cwv_evals_*_row.sh script (after
# HARNESS/SCRIPT_DIR are set):
#
#   source "$(dirname "${BASH_SOURCE[0]}")/row_eval_lib.sh"
#
# Callers must set these globals before calling the relevant functions:
#   VISUAL_SCRIPT  — path to visual_validate.py
#   CWV_SCRIPT     — path to cwv_benchmark.py
#   NUM_RUNS       — number of CWV runs per device
#   HARNESS        — path to the harness directory
#
# Output globals set by functions:
#   ROW_ACTUAL_COMMIT       — actual HEAD commit after clone+checkout
#   ROW_COMMIT_FALLBACK     — "false" for strict checkout; retained for metadata compatibility
#   ROW_CHECKOUT_METHOD     — checkout method reported by git_repo_lib.sh
#   ROW_VISUAL_REGRESSED    — "0" or "1"
#   ROW_EFFECTIVE_PATCH_FILE — actual patch file used (may be empty.patch)
#
# All functions are safe to call under set -euo pipefail.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/git_repo_lib.sh"

export SANDBOX_MAX_SLOTS="${SANDBOX_MAX_SLOTS:-20}"
export MEASURE_PARALLEL="${MEASURE_PARALLEL:-${CWV_PARALLEL:-0}}"
export MEASURE_SEMAPHORE_DIR="${MEASURE_SEMAPHORE_DIR:-${HARNESS_TMPDIR:-/tmp}/web_bench_measure_slots}"

row_measure_acquire() {
  [[ "${MEASURE_PARALLEL:-0}" -gt 0 ]] || return 0
  mkdir -p "$MEASURE_SEMAPHORE_DIR"
  while true; do
    local i lock
    for i in $(seq 0 $((MEASURE_PARALLEL - 1))); do
      lock="$MEASURE_SEMAPHORE_DIR/slot_$i.lockdir"
      if [[ -f "$lock/pid" ]]; then
        local old_pid
        old_pid="$(cat "$lock/pid" 2>/dev/null || true)"
        [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null || rm -rf "$lock"
      fi
      if mkdir "$lock" 2>/dev/null; then
        printf '%s\n' "${BASHPID:-$$}" > "$lock/pid"
        export ROW_MEASURE_LOCK_PATH="$lock"
        return 0
      fi
    done
    sleep 0.25 || true
  done
}

row_measure_release() {
  [[ -n "${ROW_MEASURE_LOCK_PATH:-}" ]] || return 0
  rm -rf "$ROW_MEASURE_LOCK_PATH" || true
  unset ROW_MEASURE_LOCK_PATH
}

row_free_port() {
  local PORT="$1"
  local pids=""

  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null || true)"
    [[ -n "$pids" ]] && kill -KILL $pids >/dev/null 2>&1 || true
  elif command -v fuser >/dev/null 2>&1; then
    fuser -k -KILL "$PORT/tcp" >/dev/null 2>&1 || true
  fi

  for _w in $(seq 1 20); do
    if command -v lsof >/dev/null 2>&1; then
      lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    elif command -v fuser >/dev/null 2>&1; then
      fuser "$PORT/tcp" >/dev/null 2>&1 || break
    else
      break
    fi
    sleep 0.5 || true
  done
}

# ---------------------------------------------------------------------------
# acquire_slot — job-pool slot acquisition; sets global _SLOT.
# Requires: JOB_SLOT (declare -A), PARALLEL
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# row_wait_for_server PORT [TIMEOUT]
# Polls http://localhost:PORT/ until it responds or TIMEOUT seconds elapse.
# Returns 0 on success, 1 on timeout.
# ---------------------------------------------------------------------------
row_wait_for_server() {
  local port="$1" timeout="${2:-90}" i
  for i in $(seq 1 "$timeout"); do
    curl -fs "http://localhost:${port}/" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------------------
# row_kill_server PID
# Kills the process group for PID (so all child processes are also killed),
# then waits for PID to exit.
# ---------------------------------------------------------------------------
row_kill_server() {
  local pid="$1"
  if [[ "$pid" == docker:* ]]; then
    local cid="${pid#docker:}"
    [[ -n "$cid" ]] && docker rm -f "$cid" >/dev/null 2>&1 || true
    return 0
  fi
  [[ -z "$pid" || "$pid" == "none" ]] && return 0
  kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

row_slot_json() {
  local SLOT_INDEX="${1:-}"
  local MODE="${2:-docker}"
  local ROOT_DIR
  ROOT_DIR="$(cd "$HARNESS/.." && pwd)"

  [[ -z "$SLOT_INDEX" ]] && return 0
  PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool slot \
    --slot-index "$SLOT_INDEX" \
    --slot-count "${PARALLEL:-$SANDBOX_MAX_SLOTS}" \
    --mode "$MODE"
}

# ---------------------------------------------------------------------------
# row_start_host WORK_DIR OUT_DIR HOST_FILE_PATH FRAMEWORK PORT
# Starts hosting via src/docker_tool by default, falling back to legacy host
# scripts when HOST_SANDBOX=0 or Docker/images are unavailable in auto mode.
#
# Sets ROW_HOST_HANDLE to either a numeric PID or docker:<container_id>.
# ---------------------------------------------------------------------------
row_start_host() {
  local WORK_DIR="$1"
  local OUT_DIR="$2"
  local HOST_FILE_PATH="$3"
  local FRAMEWORK="$4"
  local PORT="$5"
  local SLOT_INDEX="${6:-}"
  local ROOT_DIR
  ROOT_DIR="$(cd "$HARNESS/.." && pwd)"

  ROW_HOST_HANDLE=""
  row_free_port "$PORT"

  if [[ "${HOST_SANDBOX:-1}" == "0" ]]; then
    PORT="$PORT" setsid bash "$HARNESS/$HOST_FILE_PATH" "$WORK_DIR" "$OUT_DIR/host.log" &
    ROW_HOST_HANDLE="$!"
    return 0
  fi

  local json rc
  local slot_json=""
  slot_json="$(row_slot_json "$SLOT_INDEX" docker 2>>"$OUT_DIR/host_tool.stderr")"
  local slot_args=()
  [[ -n "$slot_json" ]] && slot_args=(--slot-json "$slot_json")
  set +e
  json="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool host \
    --repo-dir "$WORK_DIR" \
    --framework "${FRAMEWORK:-Static HTML}" \
    --host-file-path "$HOST_FILE_PATH" \
    --port "$PORT" \
    --log "$OUT_DIR/host.log" \
    --mode "${SANDBOX_MODE:-auto}" \
    "${slot_args[@]}" 2>>"$OUT_DIR/host_tool.stderr")"
  rc=$?
  set -e
  printf '%s\n' "$json" > "$OUT_DIR/host_result.json"
  if [[ $rc -ne 0 ]]; then
    return 1
  fi
  ROW_HOST_HANDLE="$(python3 - "$OUT_DIR/host_result.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
if d.get("container_id"):
    print("docker:" + d["container_id"])
elif d.get("pid"):
    print(d["pid"])
else:
    print("none")
PY
)"
}

# ---------------------------------------------------------------------------
# row_clone_baseline REPO_ID COMMIT_ID DEST_BASELINE_DIR SCRATCH_DIR [LOG_TAG]
#
# Clones https://github.com/REPO_ID.git into a temp dir under SCRATCH_DIR,
# fetching COMMIT_ID directly. Commits a baseline snapshot and moves the clone
# to DEST_BASELINE_DIR.
#
# Sets globals: ROW_ACTUAL_COMMIT, ROW_COMMIT_FALLBACK, ROW_CHECKOUT_METHOD
# Returns 1 on clone failure (caller should return/continue).
# ---------------------------------------------------------------------------
row_clone_baseline() {
  local REPO_ID="$1"
  local COMMIT_ID="$2"
  local DEST_BASELINE_DIR="$3"
  local SCRATCH_DIR="$4"
  local LOG_TAG="${5:-[rowwise]}"

  # Extract job ID from DEST_BASELINE_DIR's parent directory name for error messages
  local _JOB_ID
  _JOB_ID="$(basename "$(dirname "$DEST_BASELINE_DIR")")"

  local CLONE_TMP
  CLONE_TMP="$(mktemp -d -p "$SCRATCH_DIR")"
  if ! bench_git_clone_checkout "$REPO_ID" "$COMMIT_ID" "$CLONE_TMP" "$LOG_TAG" "$_JOB_ID"; then
    rm -rf "$CLONE_TMP"
    return 1
  fi

  ROW_ACTUAL_COMMIT="$BENCH_GIT_ACTUAL_COMMIT"
  ROW_COMMIT_FALLBACK="$BENCH_GIT_COMMIT_FALLBACK"
  ROW_CHECKOUT_METHOD="$BENCH_GIT_CHECKOUT_METHOD"
  ROW_CHECKOUT_ERROR="$BENCH_GIT_CHECKOUT_ERROR"
  git -C "$CLONE_TMP" add -A >/dev/null 2>&1 || true
  git -C "$CLONE_TMP" commit -qm "baseline" >/dev/null 2>&1 || true
  mv "$CLONE_TMP" "$DEST_BASELINE_DIR"
  return 0
}

# ---------------------------------------------------------------------------
# row_write_baseline_meta OUT_DIR COMMIT_CLEAN
# Writes baseline_meta.json using the globals set by row_clone_baseline.
# ---------------------------------------------------------------------------
row_write_baseline_meta() {
  local OUT_DIR="$1"
  BENCH_GIT_REQUESTED_COMMIT="$(bench_git_clean_commit "$2")"
  BENCH_GIT_ACTUAL_COMMIT="$ROW_ACTUAL_COMMIT"
  BENCH_GIT_COMMIT_FALLBACK="$ROW_COMMIT_FALLBACK"
  BENCH_GIT_CHECKOUT_METHOD="$ROW_CHECKOUT_METHOD"
  BENCH_GIT_CHECKOUT_ERROR="${ROW_CHECKOUT_ERROR:-}"
  bench_git_write_meta "$OUT_DIR/baseline_meta.json"
}

# ---------------------------------------------------------------------------
# row_apply_patch WORK_DIR PATCH_FILE OUT_DIR [LOG_TAG]
# Applies PATCH_FILE to WORK_DIR. If PATCH_FILE is missing or empty, creates
# an empty.patch in OUT_DIR and sets ROW_EFFECTIVE_PATCH_FILE to that path.
# ---------------------------------------------------------------------------
row_apply_patch() {
  local WORK_DIR="$1"
  local PATCH_FILE="$2"
  local OUT_DIR="$3"
  local LOG_TAG="${4:-[rowwise]}"

  ROW_EFFECTIVE_PATCH_FILE="$PATCH_FILE"
  ROW_PATCH_APPLIED="1"

  if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
    if ! bench_git_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "$LOG_TAG"; then
      ROW_PATCH_APPLIED="0"
      return 1
    fi
  else
    echo "$LOG_TAG WARN: empty/missing patch — measuring baseline"
    ROW_EFFECTIVE_PATCH_FILE="$OUT_DIR/empty.patch"
    touch "$ROW_EFFECTIVE_PATCH_FILE"
    bench_git_apply_patch "$WORK_DIR" "$ROW_EFFECTIVE_PATCH_FILE" "$OUT_DIR" "$LOG_TAG" || true
  fi
}

# ---------------------------------------------------------------------------
# row_measure_visual OUT_DIR REPO_ID COMMIT_CLEAN FW PATCH_FILE PORT [TIMEOUT_S] [SLOT_INDEX] [HOST_HANDLE]
# Runs visual_validate.py. Sets ROW_VISUAL_REGRESSED to "0" or "1".
# Requires: VISUAL_SCRIPT global.
# If TIMEOUT_S is non-empty, wraps the python call in `timeout TIMEOUT_S`.
# ---------------------------------------------------------------------------
row_measure_visual() {
  local OUT_DIR="$1"
  local REPO_ID="$2"
  local COMMIT_CLEAN="$3"
  local FW="$4"
  local PATCH_FILE="$5"
  local PORT="$6"
  local TIMEOUT_S="${7:-}"
  local SLOT_INDEX="${8:-}"
  local HOST_HANDLE="${9:-${ROW_HOST_HANDLE:-}}"
  local ROOT_DIR
  ROOT_DIR="$(cd "$HARNESS/.." && pwd)"
  local TIMEOUT_CMD=()
  if [[ -n "$TIMEOUT_S" ]] && command -v timeout >/dev/null 2>&1; then
    TIMEOUT_CMD=(timeout "$TIMEOUT_S")
  fi

  local slot_json=""
  if [[ -n "$SLOT_INDEX" ]]; then
    slot_json="$(row_slot_json "$SLOT_INDEX" docker 2>>"$OUT_DIR/visual.stderr")"
  fi

  row_measure_acquire

  if [[ "${REGRESSION_MEASURE_SANDBOX:-docker}" != "local" && "$HOST_HANDLE" == docker:* ]]; then
    local cid="${HOST_HANDLE#docker:}"
    local slot_args=()
    [[ -n "$slot_json" ]] && slot_args=(--slot-json "$slot_json")
    local _cmd=(python3 -m docker_tool visual
      --url              "http://localhost:$PORT"
      --screenshot-path  "$OUT_DIR/screenshot.png"
      --repo-id          "$REPO_ID"
      --commit-id        "${COMMIT_CLEAN:-}"
      --framework        "$FW"
      --host-file-path   "${HOST_FILE_PATH:-}"
      --patch-file       "$PATCH_FILE"
      --output-json      "$OUT_DIR/visual.json"
      --host-container-id "$cid"
      "${slot_args[@]}")
    PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "${TIMEOUT_CMD[@]}" "${_cmd[@]}" 2>>"$OUT_DIR/visual.stderr" || { row_measure_release; return 1; }
    [[ -f "$OUT_DIR/visual.json" ]]
    ROW_VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
")
    row_measure_release
    return 0
  fi

  local _cmd=(python3 "$VISUAL_SCRIPT"
    --url              "http://localhost:$PORT"
    --screenshot-path  "$OUT_DIR/screenshot.png"
    --repo-id          "$REPO_ID"
    --commit-id        "${COMMIT_CLEAN:-}"
    --framework        "$FW"
    --host-file-path   "${HOST_FILE_PATH:-}"
    --patch-file       "$PATCH_FILE"
    --output-json      "$OUT_DIR/visual.json")
  if [[ -n "$slot_json" ]]; then
    _cmd+=(--slot-json "$slot_json")
  fi

  "${TIMEOUT_CMD[@]}" "${_cmd[@]}" 2>>"$OUT_DIR/visual.stderr" || { row_measure_release; return 1; }

  ROW_VISUAL_REGRESSED="0"
  [[ -f "$OUT_DIR/visual.json" ]]
  ROW_VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
")
  row_measure_release
}

# ---------------------------------------------------------------------------
# row_measure_cwv OUT_DIR PORT NUM_RUNS [HOST_HANDLE] [SLOT_INDEX]
# Runs cwv_benchmark.py for mobile + desktop.
# Requires: CWV_SCRIPT global.
# ---------------------------------------------------------------------------
row_measure_cwv() {
  local OUT_DIR="$1"
  local PORT="$2"
  local NUM_RUNS="$3"
  local HOST_HANDLE="${4:-${ROW_HOST_HANDLE:-}}"
  local SLOT_INDEX="${5:-}"
  local ROOT_DIR
  ROOT_DIR="$(cd "$HARNESS/.." && pwd)"

  row_measure_acquire

  if [[ "${CWV_MEASURE_SANDBOX:-docker}" != "local" && "$HOST_HANDLE" == docker:* ]]; then
    local cid="${HOST_HANDLE#docker:}"
    local slot_json=""
    slot_json="$(row_slot_json "$SLOT_INDEX" docker 2>>"$OUT_DIR/cwv_stderr.txt")"
    local slot_args=()
    [[ -n "$slot_json" ]] && slot_args=(--slot-json "$slot_json")
    PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool measure \
      --url "http://localhost:$PORT" \
      --device mobile \
      --num-runs "$NUM_RUNS" \
      --host-container-id "$cid" \
      "${slot_args[@]}" \
      > "$OUT_DIR/mobile.json" 2>>"$OUT_DIR/cwv_stderr.txt" || { row_measure_release; return 1; }
    PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool measure \
      --url "http://localhost:$PORT" \
      --device desktop \
      --num-runs "$NUM_RUNS" \
      --host-container-id "$cid" \
      "${slot_args[@]}" \
      > "$OUT_DIR/desktop.json" 2>>"$OUT_DIR/cwv_stderr.txt" || { row_measure_release; return 1; }
    row_measure_release
    return 0
  fi

  python3 "$CWV_SCRIPT" \
    --device mobile  --num-runs "$NUM_RUNS" \
    --url "http://localhost:$PORT" \
    > "$OUT_DIR/mobile.json"  2>>"$OUT_DIR/cwv_stderr.txt" || { row_measure_release; return 1; }
  python3 "$CWV_SCRIPT" \
    --device desktop --num-runs "$NUM_RUNS" \
    --url "http://localhost:$PORT" \
    > "$OUT_DIR/desktop.json" 2>>"$OUT_DIR/cwv_stderr.txt" || { row_measure_release; return 1; }
  row_measure_release
}
