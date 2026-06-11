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
#   ROW_COMMIT_FALLBACK     — "true" if fell back to HEAD, "false" otherwise
#   ROW_CHECKOUT_METHOD     — "direct" | "sha_fetch" | "head_fallback"
#   ROW_VISUAL_REGRESSED    — "0" or "1"
#   ROW_EFFECTIVE_PATCH_FILE — actual patch file used (may be empty.patch)
#
# All functions are safe to call under set -euo pipefail.

export SANDBOX_MAX_SLOTS="${SANDBOX_MAX_SLOTS:-20}"

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
  local ROOT_DIR
  ROOT_DIR="$(cd "$HARNESS/.." && pwd)"

  ROW_HOST_HANDLE=""
  fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
  for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done

  if [[ "${HOST_SANDBOX:-1}" == "0" ]]; then
    PORT="$PORT" setsid bash "$HARNESS/$HOST_FILE_PATH" "$WORK_DIR" "$OUT_DIR/host.log" &
    ROW_HOST_HANDLE="$!"
    return 0
  fi

  local json rc
  set +e
  json="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool host \
    --repo-dir "$WORK_DIR" \
    --framework "${FRAMEWORK:-Static HTML}" \
    --host-file-path "$HOST_FILE_PATH" \
    --port "$PORT" \
    --log "$OUT_DIR/host.log" \
    --mode "${SANDBOX_MODE:-auto}" 2>>"$OUT_DIR/host_tool.stderr")"
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
# retrying once on failure. Checks out COMMIT_ID using SHA-fetch fallback if
# the direct checkout fails. Commits a baseline snapshot and moves the clone
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
  echo "$LOG_TAG Cloning $REPO_ID ..."
  if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
       git -c credential.helper='' -c http.extraHeader='' \
       clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
    echo "$LOG_TAG Retry clone in 10s (ID=$_JOB_ID) ..."
    sleep 10
    rm -rf "$CLONE_TMP"; CLONE_TMP="$(mktemp -d -p "$SCRATCH_DIR")"
    if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -c credential.helper='' -c http.extraHeader='' \
         clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
      echo "$LOG_TAG ERROR: clone failed after retry (ID=$_JOB_ID)"
      rm -rf "$CLONE_TMP"
      return 1
    fi
  fi

  # Checkout pinned commit with SHA-fetch fallback
  local COMMIT_CLEAN="$COMMIT_ID"
  [[ "$COMMIT_CLEAN" == " " || "$COMMIT_CLEAN" == "null" ]] && COMMIT_CLEAN=""
  ROW_COMMIT_FALLBACK="false"
  ROW_CHECKOUT_METHOD="direct"

  if [[ -n "$COMMIT_CLEAN" ]]; then
    if ! git -C "$CLONE_TMP" checkout "$COMMIT_CLEAN" >/dev/null 2>&1; then
      echo "$LOG_TAG direct checkout failed; trying explicit SHA fetch (ID=$_JOB_ID)"
      if GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -C "$CLONE_TMP" \
           -c credential.helper='' -c http.extraHeader='' \
           fetch --quiet --depth 1 --no-tags origin "$COMMIT_CLEAN" >/dev/null 2>&1 \
         && git -C "$CLONE_TMP" checkout "$COMMIT_CLEAN" >/dev/null 2>&1; then
        ROW_CHECKOUT_METHOD="sha_fetch"
        echo "$LOG_TAG SHA-fetch succeeded (ID=$_JOB_ID)"
      else
        echo "$LOG_TAG WARN: commit $COMMIT_CLEAN not reachable, falling back to HEAD (ID=$_JOB_ID)"
        ROW_COMMIT_FALLBACK="true"
        ROW_CHECKOUT_METHOD="head_fallback"
      fi
    fi
  fi

  ROW_ACTUAL_COMMIT="$(git -C "$CLONE_TMP" rev-parse HEAD 2>/dev/null || echo "unknown")"
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
  local COMMIT_CLEAN="$2"
  printf '{"requested_commit":"%s","actual_commit":"%s","commit_fallback":%s,"checkout_method":"%s"}\n' \
    "$COMMIT_CLEAN" "$ROW_ACTUAL_COMMIT" "$ROW_COMMIT_FALLBACK" "$ROW_CHECKOUT_METHOD" \
    > "$OUT_DIR/baseline_meta.json"
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

  if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
    git -C "$WORK_DIR" apply --whitespace=nowarn "$PATCH_FILE" >/dev/null 2>&1 \
      || echo "$LOG_TAG WARN: patch apply failed"
  else
    echo "$LOG_TAG WARN: empty/missing patch — measuring baseline"
    ROW_EFFECTIVE_PATCH_FILE="$OUT_DIR/empty.patch"
    touch "$ROW_EFFECTIVE_PATCH_FILE"
  fi
}

# ---------------------------------------------------------------------------
# row_measure_visual OUT_DIR REPO_ID COMMIT_CLEAN FW PATCH_FILE PORT [TIMEOUT_S]
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

  local _cmd=(python3 "$VISUAL_SCRIPT"
    --url              "http://localhost:$PORT"
    --screenshot-path  "$OUT_DIR/screenshot.png"
    --repo-id          "$REPO_ID"
    --commit-id        "${COMMIT_CLEAN:-}"
    --framework        "${FW:-static html}"
    --patch-file       "$PATCH_FILE"
    --output-json      "$OUT_DIR/visual.json")

  if [[ -n "$TIMEOUT_S" ]]; then
    timeout "$TIMEOUT_S" "${_cmd[@]}" 2>>"$OUT_DIR/visual.stderr" \
      || echo "[rowwise] WARN: visual failed"
  else
    "${_cmd[@]}" 2>>"$OUT_DIR/visual.stderr" \
      || echo "[rowwise] WARN: visual failed"
  fi

  ROW_VISUAL_REGRESSED="0"
  if [[ -f "$OUT_DIR/visual.json" ]]; then
    ROW_VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
  fi
}

# ---------------------------------------------------------------------------
# row_measure_cwv OUT_DIR PORT NUM_RUNS
# Runs cwv_benchmark.py for mobile + desktop.
# Requires: CWV_SCRIPT global.
# ---------------------------------------------------------------------------
row_measure_cwv() {
  local OUT_DIR="$1"
  local PORT="$2"
  local NUM_RUNS="$3"

  python3 "$CWV_SCRIPT" \
    --device mobile  --num-runs "$NUM_RUNS" \
    --url "http://localhost:$PORT" \
    > "$OUT_DIR/mobile.json"  2>>"$OUT_DIR/cwv_stderr.txt" || true
  python3 "$CWV_SCRIPT" \
    --device desktop --num-runs "$NUM_RUNS" \
    --url "http://localhost:$PORT" \
    > "$OUT_DIR/desktop.json" 2>>"$OUT_DIR/cwv_stderr.txt" || true
}
