#!/usr/bin/env bash
# Shared Docker/local hosting helpers for harness scripts.

export SANDBOX_MAX_SLOTS="${SANDBOX_MAX_SLOTS:-20}"

bench_start_host() {
  local WORK_DIR="$1"
  local OUT_DIR="$2"
  local HOST_FILE_PATH="$3"
  local FRAMEWORK="$4"
  local PORT="$5"
  local LOG_FILE="${6:-$OUT_DIR/host.log}"
  local ROOT_DIR
  ROOT_DIR="$(cd "${HARNESS:-$SCRIPT_DIR}/.." && pwd)"

  BENCH_HOST_HANDLE=""
  fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
  for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done

  if [[ "${HOST_SANDBOX:-1}" == "0" ]]; then
    PORT="$PORT" setsid bash "${HARNESS:-$SCRIPT_DIR}/$HOST_FILE_PATH" "$WORK_DIR" "$LOG_FILE" &
    BENCH_HOST_HANDLE="$!"
    return 0
  fi

  local json rc
  set +e
  json="$(PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m docker_tool host \
    --repo-dir "$WORK_DIR" \
    --framework "${FRAMEWORK:-Static HTML}" \
    --host-file-path "$HOST_FILE_PATH" \
    --port "$PORT" \
    --log "$LOG_FILE" \
    --mode "${SANDBOX_MODE:-auto}" 2>>"$OUT_DIR/host_tool.stderr")"
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
