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
#   MODE=visual_only bash harness/run_cwv_evals_suggestions_row.sh
#   MODE=cwv_only    bash harness/run_cwv_evals_suggestions_row.sh
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"

PARALLEL="${PARALLEL:-8}"
NUM_RUNS="${NUM_RUNS:-5}"
BASE_PORT="${BASE_PORT:-14000}"
CSV="${CSV:-$HARNESS/SAMPLE/input_100.csv}"
SUGGESTIONS_JSONL="${SUGGESTIONS_JSONL:-$HARNESS/suggestions/local_hosted_filtered_top3.jsonl}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
SKIP_MEASURE="${SKIP_MEASURE:-0}"
# MODE: visual_only | cwv_only | both (default)
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
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$MODE" != "visual_only" && "$MODE" != "cwv_only" && "$MODE" != "both" ]]; then
  echo "Invalid MODE: $MODE (must be visual_only|cwv_only|both)" >&2
  exit 1
fi

AGENT_SCRIPT="$HARNESS/agents/template_opencode_os_direct.sh"
AGENT_NAME="$(basename "$AGENT_SCRIPT" .sh)"
VISUAL_SCRIPT="$HARNESS/visual_validate.py"
CWV_SCRIPT="$SCRIPT_DIR/scripts/helper_scripts/cwv_benchmark.py"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
# EVAL_OUT_DIR: set by wrapper (run_os_models_suggestions.sh) to <root>/<model>/
# so results land under a single timestamped root per model.
if [[ -n "${EVAL_OUT_DIR:-}" ]]; then
  OUT_ROOT="$EVAL_OUT_DIR"
else
  OUT_ROOT="$HARNESS/out/suggestions_eval/$RUN_TS"
fi
TMP_ROOT="$HARNESS/out/suggestions_tmp"
SUGG_INDEX_DIR="$TMP_ROOT/sugg_index_${RUN_TS}"

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"

mkdir -p "$TMP_ROOT" "$OUT_ROOT/results" "$SUGG_INDEX_DIR"

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
  local JOB_TMP="$TMP_ROOT/$ID"
  local BASELINE_DIR="$JOB_TMP/baseline"
  local PORT=$(( BASE_PORT + SLOT ))

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
  # 1) Clone baseline once
  # -------------------------
  local CLONE_TMP
  CLONE_TMP="$(mktemp -d -p "$TMP_ROOT")"
  echo "[suggestions-rowwise] Cloning $REPO_ID ..."
  if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
       git -c credential.helper='' -c http.extraHeader='' \
       clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
    echo "[suggestions-rowwise] Retry clone in 10s (ID=$ID) ..."
    sleep 10
    rm -rf "$CLONE_TMP"; CLONE_TMP="$(mktemp -d -p "$TMP_ROOT")"
    if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -c credential.helper='' -c http.extraHeader='' \
         clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
      echo "[suggestions-rowwise] ERROR: clone failed after retry (ID=$ID)"
      rm -rf "$JOB_TMP" "$CLONE_TMP"
      return 1
    fi
  fi

  # -------------------------
  # 2) Checkout pinned commit + commit baseline snapshot
  # -------------------------
  local COMMIT_CLEAN="$COMMIT_ID"
  [[ "$COMMIT_CLEAN" == " " || "$COMMIT_CLEAN" == "null" ]] && COMMIT_CLEAN=""
  local COMMIT_FALLBACK="false"
  local CHECKOUT_METHOD="direct"
  if [[ -n "$COMMIT_CLEAN" ]]; then
    if ! git -C "$CLONE_TMP" checkout "$COMMIT_CLEAN" >/dev/null 2>&1; then
      echo "[suggestions-rowwise] SHA direct checkout failed; trying explicit fetch (ID=$ID)"
      if GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -C "$CLONE_TMP" \
           -c credential.helper='' -c http.extraHeader='' \
           fetch --quiet --depth 1 --no-tags origin "$COMMIT_CLEAN" >/dev/null 2>&1 \
         && git -C "$CLONE_TMP" checkout "$COMMIT_CLEAN" >/dev/null 2>&1; then
        CHECKOUT_METHOD="sha_fetch"
      else
        echo "[suggestions-rowwise] WARN: commit not reachable, falling back to HEAD (ID=$ID)"
        COMMIT_FALLBACK="true"
        CHECKOUT_METHOD="head_fallback"
      fi
    fi
  fi
  local ACTUAL_COMMIT
  ACTUAL_COMMIT="$(git -C "$CLONE_TMP" rev-parse HEAD 2>/dev/null || echo "unknown")"
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
    printf '{"requested_commit":"%s","actual_commit":"%s","commit_fallback":%s,"checkout_method":"%s"}\n' \
      "$COMMIT_CLEAN" "$ACTUAL_COMMIT" "$COMMIT_FALLBACK" "$CHECKOUT_METHOD" \
      > "$OUT_DIR/baseline_meta.json"

    # Resume: skip if already fully evaluated
    if [[ "$RESUME" == "1" ]]; then
      if [[ "$SKIP_MEASURE" == "1" ]]; then
        # patch-only mode: skip if non-empty patch already exists
        if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
          echo "[suggestions-rowwise] SKIP (resume): patch exists $ID s$SUGG_IDX"
          continue
        fi
      elif [[ "$MODE" == "cwv_only" ]]; then
        if [[ -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
          echo "[suggestions-rowwise] SKIP (resume): CWV already done $ID s$SUGG_IDX"
          continue
        fi
      else
        if [[ -f "$OUT_DIR/visual.json" ]]; then
          echo "[suggestions-rowwise] SKIP (resume): visual already done $ID s$SUGG_IDX"
          continue
        fi
      fi
    fi

    # cwv_only mode: only run if visual already passed
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

    # Fresh working copy for this suggestion
    local WORK_DIR="$JOB_TMP/s${SUGG_IDX}"
    rm -rf "$WORK_DIR"
    cp -r --no-preserve=mode "$BASELINE_DIR" "$WORK_DIR"

    # ── Run agent (visual_only or both mode only) ──
    if [[ "$MODE" != "cwv_only" ]]; then
      export EVAL_SUGGESTION_FILE="$SUGG_ITEM_FILE"
      export EVAL_SUGGESTION_INDEX="$SUGG_IDX"
      export EVAL_JOB_LABEL="$JOB_LABEL"
      export EVAL_JOB_ID="$ID"
      export EVAL_AGENT_NAME="$AGENT_NAME"
      export EVAL_CWV_DATA_FILE="$CWV_DATA_FILE"
      export FRAMEWORK="$FW"
      export REPO_ID

      local _AGENT_T0=$SECONDS
      bash "$AGENT_SCRIPT" \
        "$WORK_DIR" \
        "" \
        "$OUT_DIR/agent.log" \
        "$PATCH_FILE" \
        </dev/null \
        || echo "[suggestions-rowwise] WARN: agent returned non-zero ($ID s$SUGG_IDX)"
      local _AGENT_WALL=$(( SECONDS - _AGENT_T0 ))

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
      git -C "$WORK_DIR" apply --whitespace=nowarn "$PATCH_FILE" >/dev/null 2>&1 \
        || echo "[suggestions-rowwise] WARN: patch apply failed ($ID s$SUGG_IDX)"
    else
      echo "[suggestions-rowwise] WARN: empty/missing patch ($ID s$SUGG_IDX) — measuring baseline"
      touch "$PATCH_FILE"
    fi

    # ── Start HTTP server ──
    fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
    for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done
    PORT="$PORT" setsid bash "$HARNESS/$HOST_FILE_PATH" "$WORK_DIR" "$OUT_DIR/host.log" &
    local HOST_PID=$!

    if ! wait_for_server "$PORT" 90; then
      echo "[suggestions-rowwise] ERROR: server never ready ($ID s$SUGG_IDX)"
      kill -- -"$HOST_PID" 2>/dev/null || kill "$HOST_PID" 2>/dev/null || true
      rm -rf "$WORK_DIR"
      continue
    fi

    # ── Visual validation ──
    local VISUAL_REGRESSED=0
    if [[ "$MODE" == "visual_only" || "$MODE" == "both" ]]; then
      timeout 480 python3 "$VISUAL_SCRIPT" \
        --url             "http://localhost:$PORT" \
        --screenshot-path "$OUT_DIR/screenshot.png" \
        --repo-id         "$REPO_ID" \
        --commit-id       "${COMMIT_CLEAN:-}" \
        --framework       "${FW:-static html}" \
        --patch-file      "$PATCH_FILE" \
        --output-json     "$OUT_DIR/visual.json" \
        2>>"$OUT_DIR/visual.stderr" \
        || echo "[suggestions-rowwise] WARN: visual failed ($ID s$SUGG_IDX)"

      if [[ -f "$OUT_DIR/visual.json" ]]; then
        VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
      fi
    fi

    # ── CWV measurement ──
    if [[ "$MODE" == "cwv_only" || "$MODE" == "both" ]]; then
      if [[ "$VISUAL_REGRESSED" == "1" ]]; then
        echo "[suggestions-rowwise] Skipping CWV — visual regression ($ID s$SUGG_IDX)"
      else
        python3 "$CWV_SCRIPT" \
          --device mobile  --num-runs "$NUM_RUNS" \
          --url "http://localhost:$PORT" \
          > "$OUT_DIR/mobile.json"  2>>"$OUT_DIR/cwv_stderr.txt" || true
        python3 "$CWV_SCRIPT" \
          --device desktop --num-runs "$NUM_RUNS" \
          --url "http://localhost:$PORT" \
          > "$OUT_DIR/desktop.json" 2>>"$OUT_DIR/cwv_stderr.txt" || true
      fi
    fi

    kill -- -"$HOST_PID" 2>/dev/null || kill "$HOST_PID" 2>/dev/null || true
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
    sleep 0.5
  done
}

# =========================
# Dispatch loop
# =========================
# Kill any zombie servers from previous runs in our port range
for _p in $(seq "$BASE_PORT" $(( BASE_PORT + PARALLEL - 1 ))); do
  fuser -k -KILL "$_p/tcp" 2>/dev/null || true
done

echo "[suggestions-rowwise] CSV:              $CSV"
echo "[suggestions-rowwise] Suggestions JSONL: $SUGGESTIONS_JSONL"
echo "[suggestions-rowwise] Output root:       $OUT_ROOT"
echo "[suggestions-rowwise] Agent:             $AGENT_NAME"
echo "[suggestions-rowwise] MODE=$MODE  PARALLEL=$PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$LIMIT" ]]           && echo "[suggestions-rowwise] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]]      && echo "[suggestions-rowwise] --resume: skipping already-evaluated jobs"
[[ "$SKIP_MEASURE" == "1" ]] && echo "[suggestions-rowwise] --skip-measure: agent + patch only (no server/visual/CWV)"

# The dispatch Python also writes a per-job CWV data JSON to SUGG_INDEX_DIR
# to avoid passing huge strings through bash function arguments.
while IFS=$'\t' read -r ID REPO_ID FRAMEWORK COMMIT_ID HOST_FILE_PATH CWV_DATA_FILE; do
  acquire_slot
  slot=$_SLOT
  ( run_job "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" "$CWV_DATA_FILE" "$slot" ) &
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

wait
echo ""
echo "[suggestions-rowwise] All jobs complete."
echo "[suggestions-rowwise] Results: $OUT_ROOT/results/"
