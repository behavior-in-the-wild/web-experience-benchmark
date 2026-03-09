#!/usr/bin/env bash
# =============================================================================
# harness_live_bench/evaluate.sh
#
# Benchmarks coding agents on mirrored live web pages.
# Mirrors the static harness/evaluate.sh contract but sources pages from
# live_assets_eds/ instead of GitHub repo ZIPs.
#
# Key difference vs harness/evaluate.sh:
#   • No unzip/clone — page dir is copied from MIRRORS_ROOT
#   • Pre-agent synthetic CWV measurement (Step 0) on the PRISTINE mirror
#   • Both field CWV (CrUX) and synthetic CWV are exported to the agent
#   • Single host script: host_files/host_static_mirror.sh
#
# Usage:
#   bash evaluate.sh [--limit N]
# =============================================================================
set -euo pipefail

LIMIT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --limit N"; exit 1; }
      LIMIT="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# =============================================================================
# Resolve paths
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMPLE_DIR="$SCRIPT_DIR/SAMPLE"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
INPUT_JSONL="$SAMPLE_DIR/input.jsonl"
TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv.txt"
HOST_SCRIPT="$SCRIPT_DIR/host_files/host_static_mirror.sh"

TMP_ROOT="$SCRIPT_DIR/out/${RUN_TS}/run"
RESULTS_DIR="$SCRIPT_DIR/out/${RUN_TS}/results"

CWV_SCRIPT="$SCRIPT_DIR/../scripts/helper_scripts/cwv_benchmark.py"

# Save CLI overrides before .env
_OVERRIDE_PORT="${PORT:-}"
_OVERRIDE_DEVICE="${DEVICE:-}"
_OVERRIDE_NUM_RUNS="${NUM_RUNS:-}"
_OVERRIDE_BASELINE_RUNS="${BASELINE_RUNS:-}"

# =============================================================================
# Load environment
# =============================================================================
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Restore CLI overrides; apply defaults
[[ -n "$_OVERRIDE_PORT" ]]          && PORT="$_OVERRIDE_PORT"
[[ -n "$_OVERRIDE_DEVICE" ]]        && DEVICE="$_OVERRIDE_DEVICE"
[[ -n "$_OVERRIDE_NUM_RUNS" ]]      && NUM_RUNS="$_OVERRIDE_NUM_RUNS"
[[ -n "$_OVERRIDE_BASELINE_RUNS" ]] && BASELINE_RUNS="$_OVERRIDE_BASELINE_RUNS"

PORT="${PORT:-4000}"
DEVICE="${DEVICE:-desktop}"       # mobile|desktop
NUM_RUNS="${NUM_RUNS:-5}"         # post-agent measurement runs
BASELINE_RUNS="${BASELINE_RUNS:-3}" # pre-agent synthetic measurement runs

# Mirror root: default to sibling live_assets_eds/
MIRRORS_ROOT="${MIRRORS_ROOT:-$SCRIPT_DIR/../live_assets_eds}"
MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"

# =============================================================================
# Agents to benchmark
# =============================================================================
AGENTS=(
  "agents/template_aider.sh"
  # "agents/template_claudecode.sh"
)

# =============================================================================
# Sanity checks
# =============================================================================
[[ -f "$INPUT_JSONL" ]]   || { echo "Missing input: $INPUT_JSONL";    exit 1; }
[[ -f "$TASK_SPEC" ]]     || { echo "Missing task:  $TASK_SPEC";       exit 1; }
[[ -f "$HOST_SCRIPT" ]]   || { echo "Missing host:  $HOST_SCRIPT";     exit 1; }
[[ -f "$CWV_SCRIPT" ]]    || { echo "Missing cwv:   $CWV_SCRIPT";      exit 1; }
[[ -d "$MIRRORS_ROOT" ]]  || { echo "Missing mirrors root: $MIRRORS_ROOT"; exit 1; }
[[ ${#AGENTS[@]} -gt 0 ]] || { echo "No agents configured in AGENTS=()"; exit 1; }

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"
echo "[run] Input:       $INPUT_JSONL"
echo "[run] Mirrors:     $MIRRORS_ROOT"
echo "[run] Output:      $RESULTS_DIR"
echo "[run] Device:      $DEVICE  PORT=$PORT  NUM_RUNS=$NUM_RUNS  BASELINE_RUNS=$BASELINE_RUNS"
[[ -n "$LIMIT" ]] && echo "[run] LIMIT=$LIMIT"

# =============================================================================
# Helper: wait for localhost:PORT to respond
# =============================================================================
wait_for_port() {
  local port="$1" label="$2"
  local ready=0
  for _ in {1..90}; do
    if curl -fs "http://localhost:${port}/" > /dev/null 2>&1; then
      ready=1; break
    fi
    sleep 1
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "ERROR: $label never became ready on port $port"
    return 1
  fi
}

# =============================================================================
# Helper: measure CWV for both devices; return JSON paths
# =============================================================================
measure_cwv() {
  local label="$1"    # e.g. "pre_agent" or "post_agent"
  local prefix="$2"   # full path prefix for output files (without _mobile.json)
  local num_runs="$3"
  local stderr_log="${prefix}_cwv_stderr.txt"

  python3 "$CWV_SCRIPT" \
    --device mobile  --num-runs "$num_runs" --url "http://localhost:${PORT}/" \
    > "${prefix}_mobile.json"  2>> "$stderr_log" || true
  python3 "$CWV_SCRIPT" \
    --device desktop --num-runs "$num_runs" --url "http://localhost:${PORT}/" \
    > "${prefix}_desktop.json" 2>> "$stderr_log" || true

  echo "[${label}] mobile  → ${prefix}_mobile.json"
  echo "[${label}] desktop → ${prefix}_desktop.json"
}

# =============================================================================
# Main loop — iterate input.jsonl
# =============================================================================
while IFS=$'\t' read -r ID DOMAIN PAGE_URL MIRROR_DIR \
    BASELINE_DESKTOP_LCP BASELINE_DESKTOP_CLS BASELINE_DESKTOP_INP BASELINE_DESKTOP_TTFB \
    BASELINE_MOBILE_LCP  BASELINE_MOBILE_CLS  BASELINE_MOBILE_INP  BASELINE_MOBILE_TTFB
do
  for AGENT in "${AGENTS[@]}"; do
    AGENT_NAME="$(basename "$AGENT" .sh)"

    echo "======================================================"
    echo "ID=$ID  Page=$PAGE_URL  Agent=$AGENT_NAME"
    echo "======================================================"

    RUN_DIR="$TMP_ROOT/${ID}_${AGENT_NAME}"
    REPO_DIR="$RUN_DIR/repo"

    # Hard cleanup
    pkill -f "http.server ${PORT}" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    mkdir -p "$RUN_DIR" "$REPO_DIR"

    # ──────────────────────────────────────────────────────────
    # 1) Copy mirror directory into repo
    # ──────────────────────────────────────────────────────────
    MIRROR_ABS="$MIRRORS_ROOT/$MIRROR_DIR"
    if [[ ! -d "$MIRROR_ABS" ]]; then
      echo "ERROR: Mirror not found: $MIRROR_ABS — skipping"
      continue
    fi
    cp -r "$MIRROR_ABS/." "$REPO_DIR/"

    # ──────────────────────────────────────────────────────────
    # 2) Git init for baseline + diff capture
    # ──────────────────────────────────────────────────────────
    git -C "$REPO_DIR" init -q
    git -C "$REPO_DIR" add -A
    git -C "$REPO_DIR" commit -qm "baseline" || true

    # ──────────────────────────────────────────────────────────
    # 3) Spin up the mirror server for pre-agent baseline measure
    # ──────────────────────────────────────────────────────────
    PRE_HOST_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_pre_host.log"
    PORT="$PORT" bash "$HOST_SCRIPT" "$REPO_DIR" "$PRE_HOST_LOG" &
    PRE_HOST_PID=$!

    if ! wait_for_port "$PORT" "pre-agent host"; then
      tail -n 20 "$PRE_HOST_LOG" 2>/dev/null || true
      kill "$PRE_HOST_PID" 2>/dev/null || true
      rm -rf "$RUN_DIR"
      continue
    fi

    # ──────────────────────────────────────────────────────────
    # 4) Pre-agent synthetic CWV measurement (Step 0)
    # ──────────────────────────────────────────────────────────
    echo "[pre-agent] Measuring synthetic CWV baseline on mirror..."
    PRE_PREFIX="$RESULTS_DIR/${ID}_${AGENT_NAME}_pre"
    measure_cwv "pre_agent" "$PRE_PREFIX" "$BASELINE_RUNS"

    # Tear down pre-agent server
    kill "$PRE_HOST_PID" 2>/dev/null || true
    wait "$PRE_HOST_PID" 2>/dev/null || true

    # Extract synthetic CWV summaries for export to agent
    CWV_SYNTHETIC_MOBILE="$(python3 -c "
import json, sys
try:
    d = json.load(open('${PRE_PREFIX}_mobile.json'))
    agg = d.get('aggregated', d)
    print(json.dumps({k: agg.get(k) for k in ['LCP_median','CLS_median','INP_median','TTFB_median','valid_runs']}, indent=2))
except Exception as e:
    print('null', file=sys.stderr); print('null')
" 2>>"$RESULTS_DIR/${ID}_${AGENT_NAME}_cwv_stderr.txt")"
    CWV_SYNTHETIC_DESKTOP="$(python3 -c "
import json, sys
try:
    d = json.load(open('${PRE_PREFIX}_desktop.json'))
    agg = d.get('aggregated', d)
    print(json.dumps({k: agg.get(k) for k in ['LCP_median','CLS_median','INP_median','TTFB_median','valid_runs']}, indent=2))
except Exception as e:
    print('null', file=sys.stderr); print('null')
" 2>>"$RESULTS_DIR/${ID}_${AGENT_NAME}_cwv_stderr.txt")"

    # LCP entries from pre-agent desktop measurement
    LCP_ENTRIES_DESKTOP="$(python3 -c "
import json, sys
try:
    d = json.load(open('${PRE_PREFIX}_desktop.json'))
    entries = d.get('LCP_ENTRIES', [])
    print(json.dumps(entries[:3]))  # first 3 runs
except Exception:
    print('null')
" 2>/dev/null)"
    LCP_ENTRIES_MOBILE="$(python3 -c "
import json, sys
try:
    d = json.load(open('${PRE_PREFIX}_mobile.json'))
    entries = d.get('LCP_ENTRIES', [])
    print(json.dumps(entries[:3]))
except Exception:
    print('null')
" 2>/dev/null)"

    # ──────────────────────────────────────────────────────────
    # 5) Export context for agent
    # ──────────────────────────────────────────────────────────
    export PAGE_URL="$PAGE_URL"
    export DOMAIN="$DOMAIN"
    export FRAMEWORK="eds"

    # Field CWV (CrUX) — from input.jsonl
    export CWV_FIELD_MOBILE
    CWV_FIELD_MOBILE="$(python3 -c "
import json
print(json.dumps({'lcp': ${BASELINE_MOBILE_LCP:-null}, 'cls': ${BASELINE_MOBILE_CLS:-null}, 'inp': ${BASELINE_MOBILE_INP:-null}, 'ttfb': ${BASELINE_MOBILE_TTFB:-null}}, indent=2))
" 2>/dev/null || echo 'null')"

    export CWV_FIELD_DESKTOP
    CWV_FIELD_DESKTOP="$(python3 -c "
import json
print(json.dumps({'lcp': ${BASELINE_DESKTOP_LCP:-null}, 'cls': ${BASELINE_DESKTOP_CLS:-null}, 'inp': ${BASELINE_DESKTOP_INP:-null}, 'ttfb': ${BASELINE_DESKTOP_TTFB:-null}}, indent=2))
" 2>/dev/null || echo 'null')"

    # Synthetic CWV — from Step 0 measurement above
    export CWV_SYNTHETIC_MOBILE
    export CWV_SYNTHETIC_DESKTOP

    # LCP entries
    export LCP_ENTRIES_DESKTOP
    export LCP_ENTRIES_MOBILE

    # ──────────────────────────────────────────────────────────
    # 6) Run agent
    # ──────────────────────────────────────────────────────────
    AGENT_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_agent.log"
    PATCH_FILE="$RESULTS_DIR/${ID}_${AGENT_NAME}.patch"

    # Start tailing the log file in the background so the user can see agent progress
    touch "$AGENT_LOG"
    tail -f "$AGENT_LOG" &
    TAIL_PID=$!

    bash "$SCRIPT_DIR/$AGENT" \
      "$REPO_DIR" \
      "$TASK_SPEC" \
      "$AGENT_LOG" \
      "$PATCH_FILE" \
      </dev/null \
      || echo "[agent] Agent failed (continuing)"

    # Stop tailing once the agent finishes
    kill $TAIL_PID 2>/dev/null || true

    # ──────────────────────────────────────────────────────────
    # 7) Normalize patch (capture diff if agent didn't write one)
    # ──────────────────────────────────────────────────────────
    if [[ -d "$REPO_DIR/.git" ]]; then
      (
        set +e
        cd "$REPO_DIR"
        if [[ ! -s "$PATCH_FILE" ]]; then
          git add -A > /dev/null 2>&1
          git diff --cached > "$PATCH_FILE" 2>/dev/null || true
        fi
        git reset --hard HEAD > /dev/null 2>&1 || true
        git clean -fd > /dev/null 2>&1 || true
        [[ -s "$PATCH_FILE" ]] && git apply "$PATCH_FILE" > /dev/null 2>&1 || true
      )
    fi

    # ──────────────────────────────────────────────────────────
    # 8) Launch post-agent host
    # ──────────────────────────────────────────────────────────
    POST_HOST_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_post_host.log"
    PORT="$PORT" bash "$HOST_SCRIPT" "$REPO_DIR" "$POST_HOST_LOG" &
    POST_HOST_PID=$!

    if ! wait_for_port "$PORT" "post-agent host"; then
      tail -n 20 "$POST_HOST_LOG" 2>/dev/null || true
      kill "$POST_HOST_PID" 2>/dev/null || true
      rm -rf "$RUN_DIR"
      continue
    fi

    # ──────────────────────────────────────────────────────────
    # 9) Post-agent CWV measurement
    # ──────────────────────────────────────────────────────────
    echo "[post-agent] Measuring CWV after agent..."
    POST_PREFIX="$RESULTS_DIR/${ID}_${AGENT_NAME}_post"
    measure_cwv "post_agent" "$POST_PREFIX" "$NUM_RUNS"

    # ──────────────────────────────────────────────────────────
    # 10) Teardown
    # ──────────────────────────────────────────────────────────
    kill "$POST_HOST_PID" 2>/dev/null || true
    wait "$POST_HOST_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"

    echo "✓ Done: ID=$ID  Agent=$AGENT_NAME"
    echo "    pre  mobile  → ${POST_PREFIX}_mobile.json"
    echo "    post desktop → ${POST_PREFIX}_desktop.json"
  done

done < <(python3 - <<'PY' "$INPUT_JSONL" "$LIMIT"
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
            desktop.get("lcp"),   desktop.get("cls"),   desktop.get("inp"),   desktop.get("ttfb"),
            mobile.get("lcp"),    mobile.get("cls"),     mobile.get("inp"),    mobile.get("ttfb"),
        ]
        print("\t".join(safe(c) for c in cols))
        n += 1
        if limit is not None and n >= limit:
            break
PY
)
