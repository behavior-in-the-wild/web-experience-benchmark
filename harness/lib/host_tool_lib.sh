#!/usr/bin/env bash
# Shared Docker/local hosting helpers for harness scripts.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/git_repo_lib.sh"

export SANDBOX_MAX_SLOTS="${SANDBOX_MAX_SLOTS:-20}"
export MEASURE_PARALLEL="${MEASURE_PARALLEL:-${CWV_PARALLEL:-0}}"
export MEASURE_SEMAPHORE_DIR="${MEASURE_SEMAPHORE_DIR:-${HARNESS_TMPDIR:-/tmp}/web_bench_measure_slots}"
export PORT_RETRY_ATTEMPTS="${PORT_RETRY_ATTEMPTS:-4}"

bench_is_browser_unsafe_port() {
  local port="${1:-}"
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  case "$port" in
    1|7|9|11|13|15|17|19|20|21|22|23|25|37|42|43|53|69|77|79|87|95|101|102|103|104|109|110|111|113|115|117|119|123|135|137|138|139|143|161|179|389|427|465|512|513|514|515|526|530|531|532|540|548|554|556|563|587|601|636|989|990|993|995|1719|1720|1723|2049|3659|4045|5060|5061|6000|6566|6665|6666|6667|6668|6669|6697|10080)
      return 0
      ;;
  esac
  return 1
}

bench_next_browser_safe_port() {
  local port="$1"
  while bench_is_browser_unsafe_port "$port"; do
    port=$((port + 1))
  done
  printf '%s\n' "$port"
}

bench_port_for_slot() {
  local base_port="$1"
  local slot_index="$2"
  local attempt="${3:-0}"
  local slot_count="${4:-${PARALLEL:-$SANDBOX_MAX_SLOTS}}"
  local target_index=$((slot_index + attempt * slot_count))
  local port="$base_port"
  local safe_seen=0

  while true; do
    if ! bench_is_browser_unsafe_port "$port"; then
      if [[ "$safe_seen" -eq "$target_index" ]]; then
        printf '%s\n' "$port"
        return 0
      fi
      safe_seen=$((safe_seen + 1))
    fi
    port=$((port + 1))
  done
}

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
  local json
  json="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool slot \
    --slot-index "$SLOT_INDEX" \
    --slot-count "${PARALLEL:-$SANDBOX_MAX_SLOTS}" \
    --mode "$MODE")" || return 0
  python3 - "$json" <<'PY'
import json
import sys

try:
    data = json.loads(sys.argv[1])
except Exception:
    raise SystemExit(0)

required = {"slot_id", "cpuset", "cpu_count", "memory"}
if data.get("status") == "error" or not required.issubset(data):
    raise SystemExit(0)
print(json.dumps(data))
PY
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
    disown "$BENCH_HOST_HANDLE" 2>/dev/null || true
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

  local slot_json=""
  local slot_args=()
  if [[ -n "$SLOT_INDEX" ]]; then
    slot_json="$(bench_slot_json "$SLOT_INDEX" cwv 2>>"$ERR_LOG")"
    [[ -n "$slot_json" ]] && slot_args=(--slot-json "$slot_json")
  fi

  python3 "$CWV_SCRIPT" \
    --device "$DEVICE" --num-runs "$NUM_RUNS" \
    --url "$URL" \
    "${slot_args[@]}" \
    >"$OUT_JSON" 2>>"$ERR_LOG" || { bench_measure_release; return 1; }
  bench_measure_release
}
