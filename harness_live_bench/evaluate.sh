#!/usr/bin/env bash
# Row-wise parallel evaluation on mirrored live pages.
# Mirrors harness/evaluate.sh maturity (parallel slot pool, --resume, --mode, etc.)
# while keeping live-bench specifics:
#   • Baseline from MIRRORS_ROOT/MIRROR_DIR (not GitHub clone)
#   • Pre-agent synthetic CWV measurement before agent sees the page
#   • Field CWV (CrUX) from JSONL exported to agent alongside synthetic CWV
#
# Usage:
#   bash evaluate.sh [--limit N] [--parallel N] [--resume] [--mode visual_only|cwv_only|both]
#   bash evaluate.sh --skip-measure         # patch-only: agent runs, no server/CWV
#   bash evaluate.sh --skip-agent           # measure-only: skip agent, use existing patch
#
# Environment overrides:
#   INPUT_JSONL      Path to input JSONL (default: SAMPLE/input.jsonl)
#   MIRRORS_ROOT     Root dir of pre-mirrored pages (default: ../live_assets_eds)
#   EVAL_OUT_DIR     Output root (set by wrapper scripts; default: out/<timestamp>)
#   EVAL_AGENTS      Comma-separated agent paths (overrides AGENTS array)
#   PARALLEL         Parallel jobs
#   NUM_RUNS         CWV runs per device (post-agent)
#   BASELINE_RUNS    CWV runs per device (pre-agent synthetic baseline)
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS/.." && pwd)"

PARALLEL="${PARALLEL:-8}"
NUM_RUNS="${NUM_RUNS:-5}"
BASELINE_RUNS="${BASELINE_RUNS:-3}"
BASE_PORT="${BASE_PORT:-12000}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
# MODE: visual_only | cwv_only | both (default)
MODE="${MODE:-both}"
SKIP_MEASURE="${SKIP_MEASURE:-0}"
SKIP_AGENT="${SKIP_AGENT:-0}"
PATCH_RESULTS_DIR="${PATCH_RESULTS_DIR:-}"

usage() {
  cat <<'EOF'
Usage: evaluate.sh [options]

Options:
  --limit N           Process only first N JSONL rows
  --parallel N        Max concurrent jobs (default: 8)
  --resume            Skip jobs where visual.json already exists
  --mode MODE         visual_only | cwv_only | both (default: both)
  --skip-measure      Run agent only; skip server/visual/CWV measurement
  --skip-agent        Skip agent; reuse existing patches (measurement only)
  --patch-results-dir DIR  Use pre-existing patches from DIR (implies --skip-agent)
  --agents AGENTS     Comma-separated agent script paths (overrides AGENTS array)
  --help, -h          Show this message

Environment:
  INPUT_JSONL         Input JSONL (default: SAMPLE/input.jsonl)
  MIRRORS_ROOT        Mirror root dir (default: ../live_assets_eds)
  EVAL_OUT_DIR        Output root (wrapper sets this; else out/<timestamp>)
  EVAL_AGENTS         Same as --agents
  PARALLEL / NUM_RUNS / BASELINE_RUNS   Numeric tuning knobs
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)       shift; LIMIT="$1"; shift ;;
    --parallel)    shift; PARALLEL="$1"; shift ;;
    --resume)      RESUME=1; shift ;;
    --mode)        shift; MODE="$1"; shift ;;
    --skip-measure) SKIP_MEASURE=1; shift ;;
    --skip-agent)  SKIP_AGENT=1; shift ;;
    --patch-results-dir) shift; PATCH_RESULTS_DIR="$1"; SKIP_AGENT=1; shift ;;
    --agents)      shift; _AGENTS_CLI="$1"; shift ;;
    --help|-h)     usage; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done

if [[ "$MODE" != "visual_only" && "$MODE" != "cwv_only" && "$MODE" != "both" ]]; then
  echo "Invalid MODE: $MODE (must be visual_only|cwv_only|both)" >&2
  exit 1
fi

# ── Paths ────────────────────────────────────────────────────────────────────
INPUT_JSONL="${INPUT_JSONL:-$HARNESS/SAMPLE/input.jsonl}"
MIRRORS_ROOT="${MIRRORS_ROOT:-$SCRIPT_DIR/live_assets_eds}"
[[ "$MIRRORS_ROOT" = /* ]] || MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"

HOST_SCRIPT="$HARNESS/host_files/host_static_mirror.sh"
CWV_SCRIPT="$SCRIPT_DIR/src/cwv_tool/cwv_benchmark.py"
VISUAL_SCRIPT="$SCRIPT_DIR/src/regression_tool/visual_validate.py"
TASK_SPEC="$HARNESS/tasks/optimize_cwv.txt"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
if [[ -n "${EVAL_OUT_DIR:-}" ]]; then
  TMP_ROOT="$EVAL_OUT_DIR/run"
  RESULTS_DIR="$EVAL_OUT_DIR/results"
else
  TMP_ROOT="$HARNESS/out/${RUN_TS}/run"
  RESULTS_DIR="$HARNESS/out/${RUN_TS}/results"
fi

# ── Agents ───────────────────────────────────────────────────────────────────
AGENTS=(
  "agents/template_aider.sh"
  # "agents/template_opencode_os.sh"
)

_AGENTS_OVERRIDE="${_AGENTS_CLI:-${EVAL_AGENTS:-}}"
if [[ -n "$_AGENTS_OVERRIDE" ]]; then
  AGENTS=()
  IFS=',' read -r -a _AP <<< "$_AGENTS_OVERRIDE"
  for _a in "${_AP[@]}"; do
    _a="${_a#"${_a%%[![:space:]]*}"}"; _a="${_a%"${_a##*[![:space:]]}"}"
    [[ -n "$_a" ]] && AGENTS+=("$_a")
  done
fi

# ── Load .env ────────────────────────────────────────────────────────────────
for _env in "$SCRIPT_DIR/.env" "$HARNESS/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"

# ── Sanity checks ────────────────────────────────────────────────────────────
[[ -f "$INPUT_JSONL" ]]   || { echo "Missing INPUT_JSONL: $INPUT_JSONL";  exit 1; }
[[ -f "$TASK_SPEC" ]]     || { echo "Missing task spec: $TASK_SPEC";       exit 1; }
[[ -f "$HOST_SCRIPT" ]]   || { echo "Missing host script: $HOST_SCRIPT";   exit 1; }
[[ -f "$CWV_SCRIPT" ]]    || { echo "Missing cwv_benchmark.py: $CWV_SCRIPT"; exit 1; }
[[ -d "$MIRRORS_ROOT" ]]  || { echo "Missing mirrors root: $MIRRORS_ROOT"; exit 1; }
[[ ${#AGENTS[@]} -gt 0 ]] || { echo "No agents in AGENTS=()"; exit 1; }

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"
echo "[live-eval] INPUT_JSONL: $INPUT_JSONL"
echo "[live-eval] MIRRORS_ROOT: $MIRRORS_ROOT"
echo "[live-eval] Output: $RESULTS_DIR"
echo "[live-eval] Agents: ${AGENTS[*]}"
echo "[live-eval] Parallel=$PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS  BaselineRuns=$BASELINE_RUNS"
echo "[live-eval] MODE=$MODE"
[[ -n "$LIMIT" ]]           && echo "[live-eval] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]]      && echo "[live-eval] --resume: skipping already-evaluated jobs"
[[ "$SKIP_MEASURE" == "1" ]] && echo "[live-eval] --skip-measure: agent only, no CWV"
[[ "$SKIP_AGENT" == "1" ]]  && echo "[live-eval] --skip-agent: measurement only"
echo ""

# ── Helpers ──────────────────────────────────────────────────────────────────
wait_for_server() {
  local port="$1" timeout="${2:-90}" i
  for i in $(seq 1 "$timeout"); do
    curl -fs "http://localhost:${port}/" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

measure_cwv_both() {
  local prefix="$1" port="$2" runs="$3" stderr_log="$4"
  python3 "$CWV_SCRIPT" --device mobile  --num-runs "$runs" --url "http://localhost:${port}/" \
    > "${prefix}_mobile.json"  2>>"$stderr_log" || true
  python3 "$CWV_SCRIPT" --device desktop --num-runs "$runs" --url "http://localhost:${port}/" \
    > "${prefix}_desktop.json" 2>>"$stderr_log" || true
}

# ── Per-job function ──────────────────────────────────────────────────────────
run_job() {
  local ID="$1" DOMAIN="$2" PAGE_URL="$3" MIRROR_DIR_REL="$4"
  local BL_D_LCP="$5" BL_D_CLS="$6" BL_D_INP="$7" BL_D_TTFB="$8"
  local BL_M_LCP="$9" BL_M_CLS="${10}" BL_M_INP="${11}" BL_M_TTFB="${12}"
  local AGENT="${13}" SLOT="${14}"

  local AGENT_NAME PORT JOB_LABEL JOB_DIR RUN_DIR REPO_DIR
  AGENT_NAME="$(basename "$AGENT" .sh)"
  PORT=$(( BASE_PORT + SLOT ))
  JOB_LABEL="${ID}_${AGENT_NAME}"
  RUN_DIR="$TMP_ROOT/${JOB_LABEL}"
  REPO_DIR="$RUN_DIR/repo"
  JOB_DIR="$RESULTS_DIR/$JOB_LABEL"

  echo "====== Job $ID | $PAGE_URL | Agent=$AGENT_NAME | slot=$SLOT port=$PORT ======"

  # Resume: skip if already fully evaluated
  if [[ "$RESUME" == "1" ]]; then
    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ -f "$JOB_DIR/mobile.json" && -f "$JOB_DIR/desktop.json" ]]; then
        echo "[live-eval] SKIP (resume): CWV already done $JOB_LABEL"
        return 0
      fi
    else
      if [[ -f "$JOB_DIR/visual.json" ]]; then
        echo "[live-eval] SKIP (resume): already evaluated $JOB_LABEL"
        return 0
      fi
    fi
  fi

  mkdir -p "$RUN_DIR" "$REPO_DIR" "$JOB_DIR"

  # ── 1) Copy mirror into repo ──────────────────────────────────────────────
  local MIRROR_ABS="$MIRRORS_ROOT/$MIRROR_DIR_REL"
  if [[ ! -d "$MIRROR_ABS" ]]; then
    echo "[live-eval] ERROR: mirror not found: $MIRROR_ABS — skipping $JOB_LABEL"
    rm -rf "$RUN_DIR"
    return 0
  fi
  cp -r "$MIRROR_ABS/." "$REPO_DIR/"

  # ── 2) Git init for baseline + diff capture ────────────────────────────────
  git -C "$REPO_DIR" init -q
  git -C "$REPO_DIR" add -A
  git -C "$REPO_DIR" commit -qm "baseline" 2>/dev/null || true

  # ── 3) Pre-agent synthetic CWV (Step 0) ───────────────────────────────────
  local PRE_PREFIX="$JOB_DIR/pre"
  if [[ "$SKIP_MEASURE" != "1" && "$SKIP_AGENT" != "1" && "$MODE" != "cwv_only" ]]; then
    fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
    for _w in $(seq 1 10); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done
    PORT="$PORT" setsid bash "$HOST_SCRIPT" "$REPO_DIR" "$JOB_DIR/pre_host.log" &
    local PRE_HOST_PID=$!
    if wait_for_server "$PORT" 90; then
      echo "[live-eval] Measuring pre-agent synthetic CWV ($JOB_LABEL) ..."
      measure_cwv_both "$PRE_PREFIX" "$PORT" "$BASELINE_RUNS" "$JOB_DIR/pre_cwv_stderr.txt"
    else
      echo "[live-eval] WARN: pre-agent server never ready ($JOB_LABEL)"
    fi
    kill -- -"$PRE_HOST_PID" 2>/dev/null || kill "$PRE_HOST_PID" 2>/dev/null || true
    wait "$PRE_HOST_PID" 2>/dev/null || true
  fi

  # ── 4) Extract synthetic CWV summaries for agent context ──────────────────
  local CWV_SYNTHETIC_MOBILE CWV_SYNTHETIC_DESKTOP LCP_ENTRIES_MOBILE LCP_ENTRIES_DESKTOP
  CWV_SYNTHETIC_MOBILE="null"; CWV_SYNTHETIC_DESKTOP="null"
  LCP_ENTRIES_MOBILE="null"; LCP_ENTRIES_DESKTOP="null"
  if [[ -f "${PRE_PREFIX}_mobile.json" ]]; then
    CWV_SYNTHETIC_MOBILE="$(python3 -c "
import json, sys
try:
    d = json.load(open('${PRE_PREFIX}_mobile.json'))
    agg = d.get('aggregated', d)
    print(json.dumps({k: agg.get(k) for k in ['LCP_median','CLS_median','INP_median','TTFB_median','valid_runs']}))
except Exception: print('null')
" 2>/dev/null || echo 'null')"
    LCP_ENTRIES_MOBILE="$(python3 -c "
import json
try:
    d = json.load(open('${PRE_PREFIX}_mobile.json'))
    print(json.dumps(d.get('LCP_ENTRIES', [])[:3]))
except Exception: print('null')
" 2>/dev/null || echo 'null')"
  fi
  if [[ -f "${PRE_PREFIX}_desktop.json" ]]; then
    CWV_SYNTHETIC_DESKTOP="$(python3 -c "
import json, sys
try:
    d = json.load(open('${PRE_PREFIX}_desktop.json'))
    agg = d.get('aggregated', d)
    print(json.dumps({k: agg.get(k) for k in ['LCP_median','CLS_median','INP_median','TTFB_median','valid_runs']}))
except Exception: print('null')
" 2>/dev/null || echo 'null')"
    LCP_ENTRIES_DESKTOP="$(python3 -c "
import json
try:
    d = json.load(open('${PRE_PREFIX}_desktop.json'))
    print(json.dumps(d.get('LCP_ENTRIES', [])[:3]))
except Exception: print('null')
" 2>/dev/null || echo 'null')"
  fi

  # ── 5) Build field CWV JSON from JSONL baseline ────────────────────────────
  local CWV_FIELD_MOBILE CWV_FIELD_DESKTOP
  CWV_FIELD_MOBILE="$(python3 -c "
import json
print(json.dumps({'lcp': ${BL_M_LCP:-null}, 'cls': ${BL_M_CLS:-null}, 'inp': ${BL_M_INP:-null}, 'ttfb': ${BL_M_TTFB:-null}}))
" 2>/dev/null || echo 'null')"
  CWV_FIELD_DESKTOP="$(python3 -c "
import json
print(json.dumps({'lcp': ${BL_D_LCP:-null}, 'cls': ${BL_D_CLS:-null}, 'inp': ${BL_D_INP:-null}, 'ttfb': ${BL_D_TTFB:-null}}))
" 2>/dev/null || echo 'null')"

  # ── 6) Write CWV data file for agent ──────────────────────────────────────
  local CWV_DATA_FILE="$JOB_DIR/cwv_data.json"
  python3 -c "
import json
d = {
  'CWV_BASELINE_MOBILE':  json.loads('${CWV_FIELD_MOBILE}'),
  'CWV_BASELINE_DESKTOP': json.loads('${CWV_FIELD_DESKTOP}'),
  'LCP_ENTRIES_MOBILE':   json.loads('${LCP_ENTRIES_MOBILE}'),
  'LCP_ENTRIES_DESKTOP':  json.loads('${LCP_ENTRIES_DESKTOP}'),
}
with open('${CWV_DATA_FILE}', 'w') as f: json.dump(d, f)
" 2>/dev/null || true

  # Export agent env vars
  export PAGE_URL="$PAGE_URL"
  export DOMAIN="$DOMAIN"
  export FRAMEWORK="eds"
  export CWV_FIELD_MOBILE
  export CWV_FIELD_DESKTOP
  export CWV_SYNTHETIC_MOBILE
  export CWV_SYNTHETIC_DESKTOP
  export LCP_ENTRIES_DESKTOP
  export LCP_ENTRIES_MOBILE
  export EVAL_CWV_DATA_FILE="$CWV_DATA_FILE"
  export EVAL_JOB_LABEL="$JOB_LABEL"
  export EVAL_JOB_ID="$ID"
  export EVAL_AGENT_NAME="$AGENT_NAME"

  # ── 7) Run agent ──────────────────────────────────────────────────────────
  local AGENT_LOG="$JOB_DIR/agent.log"
  local PATCH_FILE="$JOB_DIR/${JOB_LABEL}.patch"

  if [[ "$SKIP_AGENT" == "1" ]]; then
    echo "[live-eval] --skip-agent: skipping agent for $JOB_LABEL"
    if [[ -n "$PATCH_RESULTS_DIR" ]]; then
      local _src=""
      if [[ -f "$PATCH_RESULTS_DIR/$JOB_LABEL/${JOB_LABEL}.patch" ]]; then
        _src="$PATCH_RESULTS_DIR/$JOB_LABEL/${JOB_LABEL}.patch"
      elif [[ -f "$PATCH_RESULTS_DIR/${JOB_LABEL}.patch" ]]; then
        _src="$PATCH_RESULTS_DIR/${JOB_LABEL}.patch"
      fi
      if [[ -n "$_src" ]]; then
        cp "$_src" "$PATCH_FILE"
        echo "[live-eval] Using pre-existing patch: $_src"
      else
        echo "[live-eval] WARN: no pre-existing patch for $JOB_LABEL — using empty patch"
        touch "$PATCH_FILE"
      fi
    fi
  else
    local _AGENT_T0=$SECONDS
    bash "$HARNESS/$AGENT" \
      "$REPO_DIR" "$TASK_SPEC" "$AGENT_LOG" "$PATCH_FILE" \
      </dev/null \
      || echo "[live-eval] WARN: agent returned non-zero for $JOB_LABEL"
    local _AGENT_WALL=$(( SECONDS - _AGENT_T0 ))
    echo "[live-eval] Agent wall time: ${_AGENT_WALL}s ($JOB_LABEL)"

    # Capture diff if agent didn't write a patch
    if [[ -d "$REPO_DIR/.git" ]]; then
      (
        set +e; cd "$REPO_DIR"
        if [[ ! -s "$PATCH_FILE" ]]; then
          git add -A >/dev/null 2>&1
          git diff --cached > "$PATCH_FILE" 2>/dev/null || true
        fi
        git reset --hard HEAD >/dev/null 2>&1 || true
        git clean -fd >/dev/null 2>&1 || true
        [[ -s "$PATCH_FILE" ]] && git apply "$PATCH_FILE" >/dev/null 2>&1 || true
      )
    fi
  fi

  # ── Skip measurement if requested ─────────────────────────────────────────
  if [[ "$SKIP_MEASURE" == "1" ]]; then
    echo "[live-eval] --skip-measure: patch saved, skipping server/CWV for $JOB_LABEL"
    rm -rf "$RUN_DIR"
    echo "✓ Done: $JOB_LABEL (patch only)"
    return 0
  fi

  # ── Apply patch to clean working copy ─────────────────────────────────────
  if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
    git -C "$REPO_DIR" apply --whitespace=nowarn "$PATCH_FILE" >/dev/null 2>&1 \
      || echo "[live-eval] WARN: patch apply failed ($JOB_LABEL)"
  else
    echo "[live-eval] WARN: empty/missing patch ($JOB_LABEL) — measuring baseline"
    touch "$PATCH_FILE"
  fi

  # ── 8) Launch post-agent server ────────────────────────────────────────────
  fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
  for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done
  PORT="$PORT" setsid bash "$HOST_SCRIPT" "$REPO_DIR" "$JOB_DIR/host.log" &
  local HOST_PID=$!

  if ! wait_for_server "$PORT" 90; then
    echo "[live-eval] ERROR: post-agent server never ready ($JOB_LABEL)"
    kill -- -"$HOST_PID" 2>/dev/null || kill "$HOST_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    return 0
  fi

  # ── 9) Visual validation ───────────────────────────────────────────────────
  local VISUAL_REGRESSED=0
  if [[ "$MODE" == "visual_only" || "$MODE" == "both" ]]; then
    timeout 480 python3 "$VISUAL_SCRIPT" \
      --url             "http://localhost:$PORT" \
      --screenshot-path "$JOB_DIR/screenshot.png" \
      --repo-id         "$DOMAIN" \
      --commit-id       "" \
      --framework       "eds" \
      --patch-file      "$PATCH_FILE" \
      --output-json     "$JOB_DIR/visual.json" \
      2>>"$JOB_DIR/visual.stderr" \
      || echo "[live-eval] WARN: visual failed ($JOB_LABEL)"

    if [[ -f "$JOB_DIR/visual.json" ]]; then
      VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$JOB_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
    fi
  fi

  # ── 10) CWV measurement ────────────────────────────────────────────────────
  if [[ "$MODE" == "cwv_only" || "$MODE" == "both" ]]; then
    if [[ "$VISUAL_REGRESSED" == "1" ]]; then
      echo "[live-eval] Skipping CWV — visual regression ($JOB_LABEL)"
    else
      echo "[live-eval] Measuring post-agent CWV ($JOB_LABEL) ..."
      python3 "$CWV_SCRIPT" --device mobile  --num-runs "$NUM_RUNS" \
        --url "http://localhost:$PORT" \
        > "$JOB_DIR/mobile.json"  2>>"$JOB_DIR/cwv_stderr.txt" || true
      python3 "$CWV_SCRIPT" --device desktop --num-runs "$NUM_RUNS" \
        --url "http://localhost:$PORT" \
        > "$JOB_DIR/desktop.json" 2>>"$JOB_DIR/cwv_stderr.txt" || true
    fi
  fi

  # ── Teardown ───────────────────────────────────────────────────────────────
  kill -- -"$HOST_PID" 2>/dev/null || kill "$HOST_PID" 2>/dev/null || true
  wait "$HOST_PID" 2>/dev/null || true
  rm -rf "$RUN_DIR"
  echo "✓ Done: $JOB_LABEL"
}

# ── Job pool (slot tracking) ──────────────────────────────────────────────────
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

# Kill any leftover servers on our port range
_MAX_PORT=$(( BASE_PORT + PARALLEL * ${#AGENTS[@]} ))
for _p in $(seq "$BASE_PORT" "$_MAX_PORT"); do
  fuser -k -KILL "$_p/tcp" 2>/dev/null || true
done

# ── Dispatch loop ─────────────────────────────────────────────────────────────
while IFS=$'\t' read -r \
  ID DOMAIN PAGE_URL MIRROR_DIR_REL \
  BL_D_LCP BL_D_CLS BL_D_INP BL_D_TTFB \
  BL_M_LCP BL_M_CLS BL_M_INP BL_M_TTFB
do
  for AGENT in "${AGENTS[@]}"; do
    acquire_slot
    slot=$_SLOT
    (
      run_job \
        "$ID" "$DOMAIN" "$PAGE_URL" "$MIRROR_DIR_REL" \
        "$BL_D_LCP" "$BL_D_CLS" "$BL_D_INP" "$BL_D_TTFB" \
        "$BL_M_LCP" "$BL_M_CLS" "$BL_M_INP" "$BL_M_TTFB" \
        "$AGENT" "$slot"
    ) &
    JOB_SLOT[$!]=$slot
  done
done < <(python3 - "$INPUT_JSONL" "${LIMIT:-}" <<'PY'
import json, sys

jsonl_path = sys.argv[1]
limit_s    = sys.argv[2] if len(sys.argv) > 2 else ""
limit      = int(limit_s) if limit_s else None

def safe(v):
    if v is None: return " "
    s = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return s if s else " "

n = 0
with open(jsonl_path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try:
            r = json.loads(line)
        except Exception:
            continue

        b = r.get("baseline", {})
        desktop = b.get("desktop", {})
        mobile  = b.get("mobile",  {})

        cols = [
            r.get("id", ""),
            r.get("domain", ""),
            r.get("page_url", ""),
            r.get("mirror_dir", ""),
            desktop.get("lcp"),  desktop.get("cls"),  desktop.get("inp"),  desktop.get("ttfb"),
            mobile.get("lcp"),   mobile.get("cls"),   mobile.get("inp"),   mobile.get("ttfb"),
        ]
        print("\t".join(safe(c) for c in cols))
        n += 1
        if limit is not None and n >= limit:
            break
PY
)

wait
echo ""
echo "[live-eval] All jobs complete. Results: $RESULTS_DIR"
