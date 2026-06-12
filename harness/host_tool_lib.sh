#!/usr/bin/env bash
# Shared Docker/local hosting helpers for harness scripts.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/git_repo_lib.sh"

export SANDBOX_MAX_SLOTS="${SANDBOX_MAX_SLOTS:-20}"
export MEASURE_PARALLEL="${MEASURE_PARALLEL:-${CWV_PARALLEL:-0}}"
export MEASURE_SEMAPHORE_DIR="${MEASURE_SEMAPHORE_DIR:-${HARNESS_TMPDIR:-/tmp}/web_bench_measure_slots}"

bench_measure_acquire() {
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
        export BENCH_MEASURE_LOCK_PATH="$lock"
        return 0
      fi
    done
    sleep 0.25 || true
  done
}

bench_measure_release() {
  [[ -n "${BENCH_MEASURE_LOCK_PATH:-}" ]] || return 0
  rm -rf "$BENCH_MEASURE_LOCK_PATH" || true
  unset BENCH_MEASURE_LOCK_PATH
}

bench_free_port() {
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
    sleep 0.5
  done
}

bench_slot_json() {
  local SLOT_INDEX="${1:-}"
  local MODE="${2:-docker}"
  local ROOT_DIR
  ROOT_DIR="$(cd "${HARNESS:-$SCRIPT_DIR}/.." && pwd)"

  [[ -z "$SLOT_INDEX" ]] && return 0
  PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool slot \
    --slot-index "$SLOT_INDEX" \
    --slot-count "${PARALLEL:-$SANDBOX_MAX_SLOTS}" \
    --mode "$MODE"
}

bench_start_host() {
  local WORK_DIR="$1"
  local OUT_DIR="$2"
  local HOST_FILE_PATH="$3"
  local FRAMEWORK="$4"
  local PORT="$5"
  local LOG_FILE="${6:-$OUT_DIR/host.log}"
  local SLOT_INDEX="${7:-}"
  local ROOT_DIR
  ROOT_DIR="$(cd "${HARNESS:-$SCRIPT_DIR}/.." && pwd)"

  BENCH_HOST_HANDLE=""
  bench_free_port "$PORT"

  if [[ "${HOST_SANDBOX:-1}" == "0" ]]; then
    PORT="$PORT" setsid bash "${HARNESS:-$SCRIPT_DIR}/$HOST_FILE_PATH" "$WORK_DIR" "$LOG_FILE" &
    BENCH_HOST_HANDLE="$!"
    return 0
  fi

  local json rc
  local slot_json=""
  slot_json="$(bench_slot_json "$SLOT_INDEX" docker 2>>"$OUT_DIR/host_tool.stderr")"
  local slot_args=()
  [[ -n "$slot_json" ]] && slot_args=(--slot-json "$slot_json")
  set +e
  json="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool host \
    --repo-dir "$WORK_DIR" \
    --framework "$FRAMEWORK" \
    --host-file-path "$HOST_FILE_PATH" \
    --port "$PORT" \
    --log "$LOG_FILE" \
    --mode "${SANDBOX_MODE:-auto}" \
    "${slot_args[@]}" 2>>"$OUT_DIR/host_tool.stderr")"
  rc=$?
  set -e
  printf '%s\n' "$json" > "$OUT_DIR/host_result.json"
  if [[ $rc -ne 0 ]]; then
    return 1
  fi
  BENCH_HOST_HANDLE="$(python3 - "$OUT_DIR/host_result.json" <<'PY'
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

bench_stop_host() {
  local handle="${1:-}"
  if [[ "$handle" == docker:* ]]; then
    local cid="${handle#docker:}"
    [[ -n "$cid" ]] && docker rm -f "$cid" >/dev/null 2>&1 || true
    return 0
  fi
  [[ -z "$handle" || "$handle" == "none" ]] && return 0
  kill -- -"$handle" 2>/dev/null || kill "$handle" 2>/dev/null || true
  wait "$handle" 2>/dev/null || true
}

bench_measure_cwv() {
  local URL="$1"
  local DEVICE="$2"
  local NUM_RUNS="$3"
  local OUT_JSON="$4"
  local ERR_LOG="$5"
  local HOST_HANDLE="${6:-}"
  local SLOT_INDEX="${7:-}"
  local ROOT_DIR
  ROOT_DIR="$(cd "${HARNESS:-$SCRIPT_DIR}/.." && pwd)"

  bench_measure_acquire

  if [[ "${CWV_MEASURE_SANDBOX:-docker}" != "local" && "$HOST_HANDLE" == docker:* ]]; then
    local cid="${HOST_HANDLE#docker:}"
    local slot_json=""
    slot_json="$(bench_slot_json "$SLOT_INDEX" docker 2>>"$ERR_LOG")"
    local slot_args=()
    [[ -n "$slot_json" ]] && slot_args=(--slot-json "$slot_json")
    PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool measure \
      --url "$URL" \
      --device "$DEVICE" \
      --num-runs "$NUM_RUNS" \
      --host-container-id "$cid" \
      "${slot_args[@]}" \
      >"$OUT_JSON" 2>>"$ERR_LOG" || { bench_measure_release; return 1; }
    bench_measure_release
    return 0
  fi

  python3 "$CWV_SCRIPT" \
    --device "$DEVICE" --num-runs "$NUM_RUNS" \
    --url "$URL" \
    >"$OUT_JSON" 2>>"$ERR_LOG" || { bench_measure_release; return 1; }
  bench_measure_release
}
