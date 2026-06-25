#!/usr/bin/env bash
# Row-wise full-pipeline evaluation using pre-generated suggestions.
# For each CSV row: clone baseline ONCE, then for each of the (up to 3) suggestions
# in local_hosted_filtered_top3.jsonl run the direct-implementation agent and
# immediately evaluate the resulting patch (visual + CWV).
#
# Key differences vs run_cwv_evals_oss_row.sh:
#   * Runs the agent (template_opencode_os_direct.sh) — does not require pre-existing patches
#   * No planning phase: each agent call receives one suggestion and implements it directly
#   * Three runs per site (one per suggestion), labelled {ID}_s0_, {ID}_s1_, {ID}_s2_
#   * All three patches for a site share one port (sequential within a job)
#   * Suggestions are sourced from SUGGESTIONS_JSONL (JSONL, keyed by row_id)
#   * CWV baseline data is serialised per-job to avoid ARG_MAX / E2BIG limits
#
# Usage (run from project root or anywhere):
#   bash harness/run_cwv_evals_suggestions_row.sh
#   PARALLEL=20 bash harness/run_cwv_evals_suggestions_row.sh
#   LIMIT=5     bash harness/run_cwv_evals_suggestions_row.sh
#   bash harness/run_cwv_evals_suggestions_row.sh --resume
#   bash harness/run_cwv_evals_suggestions_row.sh --skip-measure   # patch-only, no server/visual/CWV
#   MODE=visual_only    bash harness/run_cwv_evals_suggestions_row.sh
#   MODE=cwv_only       bash harness/run_cwv_evals_suggestions_row.sh
#   MODE=measure_only   bash harness/run_cwv_evals_suggestions_row.sh  # skip agent, use existing patch
set -euo pipefail
trap 'echo "[suggestions-rowwise] FATAL line=$LINENO status=$?" >&2' ERR

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"
source "$HARNESS/lib/host_tool_lib.sh"

PARALLEL="${PARALLEL:-8}"
NUM_RUNS="${NUM_RUNS:-5}"
BASE_PORT="${BASE_PORT:-14000}"
CSV="${CSV:-$HARNESS/SAMPLE/input_100.csv}"
SUGGESTIONS_JSONL="${SUGGESTIONS_JSONL:-$HARNESS/suggestions/local_hosted_filtered_top3.jsonl}"
EXISTING_PATCH_ROOT="${EXISTING_PATCH_ROOT:-}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
SKIP_MEASURE="${SKIP_MEASURE:-0}"
# MODE: visual_only | cwv_only | cwv_only_all | both (default) | measure_only (skip agent, use existing patch)
MODE="${MODE:-both}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume)       RESUME=1; shift ;;
    --skip-measure) SKIP_MEASURE=1; shift ;;
    --csv)          shift; CSV="$1"; shift ;;
    --limit)        shift; LIMIT="$1"; shift ;;
    --parallel)     shift; PARALLEL="$1"; shift ;;
    --mode)         shift; MODE="$1"; shift ;;
    --suggestions-jsonl) shift; SUGGESTIONS_JSONL="$1"; shift ;;
    --existing-patch-root) shift; EXISTING_PATCH_ROOT="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$MODE" != "visual_only" && "$MODE" != "cwv_only" && "$MODE" != "cwv_only_all" && "$MODE" != "both" && "$MODE" != "measure_only" && "$MODE" != "both_all" ]]; then
  echo "Invalid MODE: $MODE (must be visual_only|cwv_only|cwv_only_all|both|measure_only|both_all)" >&2
  exit 1
fi

AGENT_SCRIPT="${AGENT_SCRIPT:-$HARNESS/agents/template_opencode_os_direct.sh}"
AGENT_NAME="$(basename "$AGENT_SCRIPT" .sh)"
VISUAL_SCRIPT="$SCRIPT_DIR/src/regression_tool/visual_validate.py"
CWV_SCRIPT="$SCRIPT_DIR/src/cwv_tool/cwv_benchmark.py"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
# EVAL_OUT_DIR: set by wrapper (run_os_models_suggestions.sh) to <root>/<model>/
# so results land under a single timestamped root per model.
if [[ -n "${EVAL_OUT_DIR:-}" ]]; then
  OUT_ROOT="$EVAL_OUT_DIR"
else
  OUT_ROOT="$HARNESS/out/suggestions_eval/$RUN_TS"
fi
TMP_ROOT="$OUT_ROOT/tmp"
SUGG_INDEX_DIR="$TMP_ROOT/sugg_index"

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"
export WEB_BENCH_REPO_CACHE="${WEB_BENCH_REPO_CACHE:-/dev/shm/ayush/web-experience-benchmark/.cache/web_benchmark_repos}"

mkdir -p "$TMP_ROOT/jobs" "$OUT_ROOT/results" "$SUGG_INDEX_DIR"

# =========================
# Pre-index suggestions JSONL → one JSON file per row_id
# =========================
echo "[suggestions-rowwise] Indexing $SUGGESTIONS_JSONL ..."
python3 - "$SUGGESTIONS_JSONL" "$SUGG_INDEX_DIR" << 'PY'
import json, sys, os

jsonl_path, out_dir = sys.argv[1], sys.argv[2]
count = 0
with open(jsonl_path, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        row_id = str(d.get('row_id', '')).strip()
        if not row_id:
            continue
        ar = d.get('analysis_result', {}) or {}
        out = {
            'row_id':      row_id,
            'suggestions': ar.get('suggestions', []),
            'device_type': ar.get('device_type', ''),
            'source':      d.get('source', {}),
        }
        with open(os.path.join(out_dir, f'{row_id}.json'), 'w', encoding='utf-8') as fo:
            json.dump(out, fo)
        count += 1
print(f'Indexed {count} rows.')
PY

# =========================
# Helpers
# =========================
wait_for_server() {
  local port="$1" timeout="${2:-90}" i
  for i in $(seq 1 "$timeout"); do
    curl -fs "http://localhost:${port}/" >/dev/null 2>&1 && return 0
    curl -fsk "https://localhost:${port}/" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# =========================
# Per-job function
# =========================
run_job() {
  local ID="$1" REPO_ID="$2" FRAMEWORK="$3" COMMIT_ID="$4"
  local HOST_FILE_PATH="$5" CWV_DATA_FILE="$6" SLOT="$7"
  local JOB_TMP
  JOB_TMP="$(mktemp -d -p "$TMP_ROOT/jobs" "${ID}_${SLOT}_XXXXXX")"
  local BASELINE_DIR="$JOB_TMP/baseline"
  local PORT
  PORT="$(bench_port_for_slot "$BASE_PORT" "$SLOT" 0 "$PARALLEL")"

  # Locate suggestions for this row
  local SUGG_FILE="$SUGG_INDEX_DIR/${ID}.json"
  if [[ ! -f "$SUGG_FILE" ]]; then
    echo "[suggestions-rowwise] WARN: no suggestions for ID=$ID — skipping"
    return 0
  fi

  local SUGG_COUNT
  SUGG_COUNT="$(python3 -c "
import json
d = json.load(open('$SUGG_FILE'))
print(len(d.get('suggestions', [])))
" 2>/dev/null || echo 0)"

  if [[ "$SUGG_COUNT" -eq 0 ]]; then
    echo "[suggestions-rowwise] WARN: 0 suggestions for ID=$ID — skipping"
    return 0
  fi

  echo "====== Job $ID | $REPO_ID | sugg_count=$SUGG_COUNT | slot=$SLOT ======"
  mkdir -p "$JOB_TMP"

  # -------------------------
  # 1-2) Fetch pinned commit directly + commit baseline snapshot
  # -------------------------
  local CLONE_TMP
  CLONE_TMP="$(mktemp -d -p "$TMP_ROOT")"
  if ! bench_git_clone_checkout "$REPO_ID" "$COMMIT_ID" "$CLONE_TMP" "[suggestions-rowwise]" "$ID"; then
    rm -rf "$JOB_TMP" "$CLONE_TMP"
    return 1
  fi
  local COMMIT_CLEAN="$BENCH_GIT_REQUESTED_COMMIT"
  local COMMIT_FALLBACK="$BENCH_GIT_COMMIT_FALLBACK"
  local CHECKOUT_METHOD="$BENCH_GIT_CHECKOUT_METHOD"
  local ACTUAL_COMMIT="$BENCH_GIT_ACTUAL_COMMIT"
  git -C "$CLONE_TMP" add -A >/dev/null 2>&1 || true
  git -C "$CLONE_TMP" commit -qm "baseline" >/dev/null 2>&1 || true
  mv "$CLONE_TMP" "$BASELINE_DIR"

  local FW
  FW="$(echo "${FRAMEWORK:-unknown}" | tr '[:upper:]' '[:lower:]')"

  # -------------------------
  # 3) Process each suggestion
  # -------------------------
  local SUGG_IDX
  for SUGG_IDX in $(seq 0 $(( SUGG_COUNT - 1 ))); do
    local JOB_LABEL="${ID}_s${SUGG_IDX}_${AGENT_NAME}"
    local OUT_DIR="$OUT_ROOT/results/$JOB_LABEL"
    local PATCH_FILE="$OUT_DIR/${JOB_LABEL}.patch"

    mkdir -p "$OUT_DIR"

    # Record baseline commit info
    bench_git_write_meta "$OUT_DIR/baseline_meta.json"

    # Resume: skip if already fully evaluated
    if [[ "$RESUME" == "1" ]]; then
      if [[ "$SKIP_MEASURE" == "1" ]]; then
        # patch-only mode: skip if non-empty patch already exists
        if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
          echo "[suggestions-rowwise] SKIP (resume): patch exists $ID s$SUGG_IDX"
          continue
        fi
      elif [[ "$MODE" == "cwv_only" || "$MODE" == "cwv_only_all" ]]; then
        if [[ -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
          echo "[suggestions-rowwise] SKIP (resume): CWV already done $ID s$SUGG_IDX"
          continue
        fi
      elif [[ "$MODE" == "measure_only" || "$MODE" == "both_all" ]]; then
        if [[ -f "$OUT_DIR/visual.json" && -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
          echo "[suggestions-rowwise] SKIP (resume): visual+CWV already done $ID s$SUGG_IDX"
          continue
        fi
      else
        if [[ -f "$OUT_DIR/visual.json" ]]; then
          echo "[suggestions-rowwise] SKIP (resume): visual already done $ID s$SUGG_IDX"
          continue
        fi
      fi
    fi

    # cwv_only mode: only run if visual already passed. cwv_only_all skips
    # visual gating and measures every suggestion.
    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ ! -f "$OUT_DIR/visual.json" ]]; then
        echo "[suggestions-rowwise] SKIP cwv_only: no visual.json yet ($ID s$SUGG_IDX)"
        continue
      fi
    fi

    # Extract this suggestion item
    local SUGG_ITEM_FILE="$JOB_TMP/sugg_${SUGG_IDX}.json"
    python3 - "$SUGG_FILE" "$SUGG_IDX" "$SUGG_ITEM_FILE" << 'PY'
import json, sys
d = json.load(open(sys.argv[1]))
idx = int(sys.argv[2])
suggs = d.get('suggestions', [])
if not (0 <= idx < len(suggs)):
    raise SystemExit(f"Suggestion index {idx} out of range ({len(suggs)} suggestions)")
with open(sys.argv[3], 'w') as f:
    json.dump(suggs[idx], f, indent=2)
PY
    cp "$SUGG_ITEM_FILE" "$OUT_DIR/input_suggestion.json"

    if [[ -n "$EXISTING_PATCH_ROOT" ]]; then
      local OWNER PATCH_INDEX EXISTING_PATCH_DIR EXISTING_PATCH
      OWNER="${REPO_ID%%/*}"
      PATCH_INDEX=$(( SUGG_IDX + 1 ))
      EXISTING_PATCH_DIR="$EXISTING_PATCH_ROOT/${ID}_${OWNER}/patches"
      EXISTING_PATCH="$(find "$EXISTING_PATCH_DIR" -maxdepth 1 -type f -name "suggestion_${PATCH_INDEX}_run*.patch" 2>/dev/null | sort | head -n 1 || true)"
      if [[ -z "$EXISTING_PATCH" ]]; then
        echo "[suggestions-rowwise] SKIP: no existing patch for ID=$ID suggestion_$PATCH_INDEX"
        printf '{"status":"missing_patch","patch_root":"%s","suggestion_index":%d}\n' \
          "$EXISTING_PATCH_ROOT" "$PATCH_INDEX" > "$OUT_DIR/missing_patch.json"
        continue
      fi
      cp "$EXISTING_PATCH" "$PATCH_FILE"
      printf '{"source_patch":"%s"}\n' "$EXISTING_PATCH" > "$OUT_DIR/source_patch.json"
    fi

    # Fresh working copy for this suggestion
    local WORK_DIR="$JOB_TMP/s${SUGG_IDX}"
    rm -rf "$WORK_DIR"
    mkdir -p "$WORK_DIR"
    cp -R "$BASELINE_DIR"/. "$WORK_DIR"/

    # ── Run agent (skip for cwv_only, measure_only, and existing-patch mode) ──
    if [[ -z "$EXISTING_PATCH_ROOT" && "$MODE" != "cwv_only" && "$MODE" != "measure_only" && "$MODE" != "both_all" ]]; then
      export EVAL_SUGGESTION_FILE="$SUGG_ITEM_FILE"
      export EVAL_SUGGESTION_INDEX="$SUGG_IDX"
      export EVAL_JOB_LABEL="$JOB_LABEL"
      export EVAL_JOB_ID="$ID"
      export EVAL_AGENT_NAME="$AGENT_NAME"
      export EVAL_CWV_DATA_FILE="$CWV_DATA_FILE"
      export FRAMEWORK="$FW"
      export REPO_ID

      local _AGENT_T0=$SECONDS
      local _AGENT_EXIT=0
      bash "$AGENT_SCRIPT" \
        "$WORK_DIR" \
        "" \
        "$OUT_DIR/agent.log" \
        "$PATCH_FILE" \
        </dev/null \
        || _AGENT_EXIT=$?
      local _AGENT_WALL=$(( SECONDS - _AGENT_T0 ))
      if [[ "$_AGENT_EXIT" -ne 0 ]] || rg -q 'authentication_failed|invalid subscription key|Resource Not Found|api_error_status":401|api_error_status":403|error_status":401|error_status":403' "$OUT_DIR" -g '*agent.log*' -g '*.ndjson' 2>/dev/null; then
        echo "[suggestions-rowwise] SKIP: agent failed or API/auth error ($ID s$SUGG_IDX)"
        printf '{"status":"agent_failed","exit_code":%d,"wall_clock_seconds":%d}\n' "$_AGENT_EXIT" "$_AGENT_WALL" > "$OUT_DIR/agent_failure.json"
        rm -rf "$WORK_DIR"
        continue
      fi

      # Merge wall time into usage.json
      if [[ -f "$OUT_DIR/usage.json" ]]; then
        python3 -c "
import json
with open('$OUT_DIR/usage.json') as f: d = json.load(f)
d['wall_clock_seconds'] = $_AGENT_WALL
with open('$OUT_DIR/usage.json', 'w') as f: json.dump(d, f, indent=2)
" 2>/dev/null || true
      else
        printf '{"wall_clock_seconds": %d}\n' "$_AGENT_WALL" > "$OUT_DIR/usage.json"
      fi
      echo "[suggestions-rowwise] Agent wall time: ${_AGENT_WALL}s ($ID s$SUGG_IDX)"
    fi

    # ── Patch-only mode: save patch and move on ──
    if [[ "$SKIP_MEASURE" == "1" ]]; then
      echo "[suggestions-rowwise] --skip-measure: patch saved, skipping server/visual/CWV ($ID s$SUGG_IDX)"
      rm -rf "$WORK_DIR"
      echo "[suggestions-rowwise] ✓ $ID s$SUGG_IDX (patch only)"
      continue
    fi

    # ── Apply patch to working copy ──
    if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
      if ! bench_git_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "[suggestions-rowwise]"; then
        echo "[suggestions-rowwise] SKIP: patch failed to apply ($ID s$SUGG_IDX)"
        rm -rf "$WORK_DIR"
        continue
      fi
    else
      echo "[suggestions-rowwise] WARN: empty/missing patch ($ID s$SUGG_IDX) — measuring baseline"
      touch "$PATCH_FILE"
      bench_git_apply_patch "$WORK_DIR" "$PATCH_FILE" "$OUT_DIR" "[suggestions-rowwise]" || true
    fi

    # ── Start HTTP server ──
    # Copy as .cjs so Node treats it as CommonJS regardless of repo's package.json "type":"module"
    [[ -f "$HARNESS/host_files/http2_server.js" ]] && \
      cp "$HARNESS/host_files/http2_server.js" "$WORK_DIR/http2_server.cjs" 2>/dev/null || true
    # SSL certs must live alongside the server script (__dirname resolution)
    [[ -f "$HARNESS/host_files/localhost-key.pem" ]] && \
      cp "$HARNESS/host_files/localhost-key.pem" "$WORK_DIR/" 2>/dev/null || true
    [[ -f "$HARNESS/host_files/localhost-cert.pem" ]] && \
      cp "$HARNESS/host_files/localhost-cert.pem" "$WORK_DIR/" 2>/dev/null || true
    local HOST_PID="" HOST_READY=0 PORT_ATTEMPT HOST_LOG
    for PORT_ATTEMPT in $(seq 0 $((PORT_RETRY_ATTEMPTS - 1))); do
      PORT="$(bench_port_for_slot "$BASE_PORT" "$SLOT" "$PORT_ATTEMPT" "$PARALLEL")"
      HOST_LOG="$OUT_DIR/host.log"
      [[ "$PORT_ATTEMPT" -gt 0 ]] && HOST_LOG="$OUT_DIR/host_retry_${PORT_ATTEMPT}.log"
      echo "[suggestions-rowwise] Starting server on port $PORT (attempt $((PORT_ATTEMPT + 1))/$PORT_RETRY_ATTEMPTS) ($ID s$SUGG_IDX)"
      if ! bench_start_host "$WORK_DIR" "$OUT_DIR" "$HOST_FILE_PATH" "$FRAMEWORK" "$PORT" "$HOST_LOG" "$SLOT"; then
        echo "[suggestions-rowwise] WARN: host tool failed on port $PORT ($ID s$SUGG_IDX)"
        continue
      fi
      HOST_PID="$BENCH_HOST_HANDLE"

      if wait_for_server "$PORT" 90; then
        HOST_READY=1
        break
      fi

      echo "[suggestions-rowwise] WARN: server never ready on port $PORT ($ID s$SUGG_IDX)"
      bench_stop_host "$HOST_PID"
      HOST_PID=""
    done

    if [[ "$HOST_READY" != "1" ]]; then
      echo "[suggestions-rowwise] ERROR: server never ready after $PORT_RETRY_ATTEMPTS port attempts ($ID s$SUGG_IDX)"
      rm -rf "$WORK_DIR"
      continue
    fi

    # ── Visual validation ──
    local VISUAL_REGRESSED=0
    if [[ "$MODE" == "visual_only" || "$MODE" == "both" || "$MODE" == "measure_only" || "$MODE" == "both_all" ]]; then
      local VISUAL_SLOT_JSON=""
      VISUAL_SLOT_JSON="$(bench_slot_json "$SLOT" docker 2>>"$OUT_DIR/visual.stderr")"
      local VISUAL_SLOT_ARGS=()
      local VISUAL_TIMEOUT_CMD=()
      local VISUAL_OK=1
      [[ -n "$VISUAL_SLOT_JSON" ]] && VISUAL_SLOT_ARGS=(--slot-json "$VISUAL_SLOT_JSON")
      command -v timeout >/dev/null 2>&1 && VISUAL_TIMEOUT_CMD=(timeout 480)
      bench_measure_acquire
      if [[ "${REGRESSION_MEASURE_SANDBOX:-docker}" != "local" && "$HOST_PID" == docker:* ]]; then
        PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "${VISUAL_TIMEOUT_CMD[@]}" python3 -m docker_tool visual \
          --url             "http://localhost:$PORT" \
          --screenshot-path "$OUT_DIR/screenshot.png" \
          --repo-id         "$REPO_ID" \
          --commit-id       "${COMMIT_CLEAN:-}" \
          --framework       "${FW:-static html}" \
          --host-file-path  "$HOST_FILE_PATH" \
          --patch-file      "$PATCH_FILE" \
          --output-json     "$OUT_DIR/visual.json" \
          --host-container-id "${HOST_PID#docker:}" \
          "${VISUAL_SLOT_ARGS[@]}" \
          2>>"$OUT_DIR/visual.stderr" || VISUAL_OK=0
      else
        "${VISUAL_TIMEOUT_CMD[@]}" python3 "$VISUAL_SCRIPT" \
          --url             "http://localhost:$PORT" \
          --screenshot-path "$OUT_DIR/screenshot.png" \
          --repo-id         "$REPO_ID" \
          --commit-id       "${COMMIT_CLEAN:-}" \
          --framework       "${FW:-static html}" \
          --host-file-path  "$HOST_FILE_PATH" \
          --patch-file      "$PATCH_FILE" \
          --output-json     "$OUT_DIR/visual.json" \
          "${VISUAL_SLOT_ARGS[@]}" \
          2>>"$OUT_DIR/visual.stderr" || VISUAL_OK=0
      fi
      bench_measure_release
      if [[ "$VISUAL_OK" != "1" ]]; then
        echo "[suggestions-rowwise] ERROR: visual failed ($ID s$SUGG_IDX)"
        bench_stop_host "$HOST_PID"
        rm -rf "$WORK_DIR"
        continue
      fi

      if [[ -f "$OUT_DIR/visual.json" ]]; then
        VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
      fi
    fi

    # ── CWV measurement ──
    if [[ "$MODE" == "cwv_only" || "$MODE" == "cwv_only_all" || "$MODE" == "both" || "$MODE" == "measure_only" || "$MODE" == "both_all" ]]; then
      if [[ "$VISUAL_REGRESSED" == "1" && "$MODE" != "both_all" ]]; then
        echo "[suggestions-rowwise] Skipping CWV — visual regression ($ID s$SUGG_IDX)"
      else
        if ! bench_measure_cwv "http://localhost:$PORT" mobile "$NUM_RUNS" "$OUT_DIR/mobile.json" "$OUT_DIR/cwv_stderr.txt" "$HOST_PID" "$SLOT"; then
          echo "[suggestions-rowwise] ERROR: mobile CWV failed ($ID s$SUGG_IDX)"
          bench_stop_host "$HOST_PID"
          rm -rf "$WORK_DIR"
          continue
        fi
        if ! bench_measure_cwv "http://localhost:$PORT" desktop "$NUM_RUNS" "$OUT_DIR/desktop.json" "$OUT_DIR/cwv_stderr.txt" "$HOST_PID" "$SLOT"; then
          echo "[suggestions-rowwise] ERROR: desktop CWV failed ($ID s$SUGG_IDX)"
          bench_stop_host "$HOST_PID"
          rm -rf "$WORK_DIR"
          continue
        fi
      fi
    fi

    bench_stop_host "$HOST_PID"
    wait "$HOST_PID" 2>/dev/null || true
    rm -rf "$WORK_DIR"
    echo "[suggestions-rowwise] ✓ $ID s$SUGG_IDX"
  done

  rm -rf "$JOB_TMP"
  echo "✓ Done: $ID ($SUGG_COUNT suggestions)"
}

# =========================
# Job pool (same slot mechanism as other row scripts)
# =========================
declare -A JOB_SLOT=()
_SLOT=0
JOB_FAILURES=0

acquire_slot() {
  while true; do
    local pid count s used p
    count=${#JOB_SLOT[@]}
    if [[ $count -gt 0 ]]; then
      for pid in "${!JOB_SLOT[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
          if ! wait "$pid"; then
            echo "[suggestions-rowwise] ERROR: job failed pid=$pid slot=${JOB_SLOT[$pid]}" >&2
            JOB_FAILURES=1
          fi
          _SLOT="${JOB_SLOT[$pid]}"
          unset "JOB_SLOT[$pid]"
          return 0
        fi
      done
    fi
    if [[ $count -lt $PARALLEL ]]; then
      for s in $(seq 0 $(( PARALLEL - 1 ))); do
        used=0
        if [[ $count -gt 0 ]]; then
          for p in "${!JOB_SLOT[@]}"; do
            [[ "${JOB_SLOT[$p]}" == "$s" ]] && used=1 && break
          done
        fi
        [[ $used -eq 0 ]] && { _SLOT="$s"; return 0; }
      done
    fi
    sleep 0.5 || true
  done
}

# =========================
# Dispatch loop
# =========================
# Kill any zombie servers from previous runs in our port range
for _p in $(seq "$BASE_PORT" $(( BASE_PORT + PARALLEL - 1 ))); do
  bench_free_port "$_p"
done

echo "[suggestions-rowwise] CSV:              $CSV"
echo "[suggestions-rowwise] Suggestions JSONL: $SUGGESTIONS_JSONL"
echo "[suggestions-rowwise] Output root:       $OUT_ROOT"
echo "[suggestions-rowwise] Agent:             $AGENT_NAME"
echo "[suggestions-rowwise] MODE=$MODE  PARALLEL=$PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$EXISTING_PATCH_ROOT" ]] && echo "[suggestions-rowwise] Existing patches:  $EXISTING_PATCH_ROOT"
[[ -n "$LIMIT" ]]           && echo "[suggestions-rowwise] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]]      && echo "[suggestions-rowwise] --resume: skipping already-evaluated jobs"
[[ "$SKIP_MEASURE" == "1" ]] && echo "[suggestions-rowwise] --skip-measure: agent + patch only (no server/visual/CWV)"
[[ "$MODE" == "measure_only" ]] && echo "[suggestions-rowwise] measure_only: using existing patches, skipping agent"
[[ "$MODE" == "both_all" ]]    && echo "[suggestions-rowwise] both_all: using existing patches, visual+CWV for all (no regression gate)"

# The dispatch Python also writes a per-job CWV data JSON to SUGG_INDEX_DIR
# to avoid passing huge strings through bash function arguments.
while IFS=$'\t' read -r ID REPO_ID FRAMEWORK COMMIT_ID HOST_FILE_PATH CWV_DATA_FILE; do
  acquire_slot
  slot=$_SLOT
  ( run_job "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" "$CWV_DATA_FILE" "$slot" ) </dev/null &
  JOB_SLOT[$!]=$slot
done < <(python3 - "$CSV" "$SUGG_INDEX_DIR" "${LIMIT:-}" << 'PY'
import csv, sys, json, os

csv.field_size_limit(10**7)
csv_path, cwv_dir, *rest = sys.argv[1:]
limit = int(rest[0]) if rest and rest[0] else None
n = 0

with open(csv_path, newline='', encoding='utf-8') as f:
    for row in csv.DictReader(f):
        def val(c):
            v = (row.get(c) or ' ').replace('\t', ' ').replace('\n', ' ')
            return v or ' '

        row_id = val('ID').strip()

        # Serialise large CWV columns to a file so we never hit ARG_MAX
        cwv = {
            'CWV_BASELINE_MOBILE':       row.get('CWV_MOBILE') or '',
            'CWV_BASELINE_DESKTOP':      row.get('CWV_DESKTOP') or '',
            'LCP_ENTRIES_MOBILE':        row.get('LCP_ENTRIES_MOBILE') or '',
            'LCP_ENTRIES_DESKTOP':       row.get('LCP_ENTRIES_DESKTOP') or '',
            'CLS_SHIFTS_MOBILE':         row.get('CLS_SHIFTS_MOBILE') or '',
            'CLS_SHIFTS_DESKTOP':        row.get('CLS_SHIFTS_DESKTOP') or '',
            'INP_INTERACTIONS_MOBILE':   row.get('INP_INTERACTIONS_MOBILE') or '',
            'INP_INTERACTIONS_DESKTOP':  row.get('INP_INTERACTIONS_DESKTOP') or '',
        }
        cwv_file = os.path.join(cwv_dir, f'{row_id}_cwv.json')
        with open(cwv_file, 'w') as cf:
            json.dump(cwv, cf)

        cols = [val('ID'), val('REPO_ID'), val('FRAMEWORK'), val('COMMIT_ID'),
                val('HOST_FILE_PATH'), cwv_file]
        print('\t'.join(cols))
        n += 1
        if limit and n >= limit:
            break
PY
)

_RUN_STATUS="$JOB_FAILURES"
for _pid in "${!JOB_SLOT[@]}"; do
  if ! wait "$_pid"; then
    echo "[suggestions-rowwise] ERROR: job failed pid=$_pid slot=${JOB_SLOT[$_pid]}" >&2
    _RUN_STATUS=1
  fi
done
echo ""
echo "[suggestions-rowwise] All jobs complete."
echo "[suggestions-rowwise] Results: $OUT_ROOT/results/"
exit "$_RUN_STATUS"
