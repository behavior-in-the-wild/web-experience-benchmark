#!/usr/bin/env bash
# Row-wise suggestions-based eval for live mirrored pages.
# Adapted from harness/run_cwv_evals_suggestions_row.sh for live bench:
#   • Input: live_filtered_top3.jsonl (suggestions + URL embedded per row)
#   • Baseline: mirror copy from MIRRORS_ROOT (not GitHub clone)
#   • Host: host_static_mirror.sh
#   • Agent: harness/agents/template_opencode_os_direct.sh
#
# Usage:
#   bash run_cwv_evals_suggestions_row.sh [--resume] [--parallel N] [--limit N]
#   MODE=measure_only bash run_cwv_evals_suggestions_row.sh --resume
#   bash run_cwv_evals_suggestions_row.sh --skip-measure    # patch only, no CWV
#
# Environment:
#   JSONL           Input JSONL path (default: SAMPLE/live_filtered_top3.jsonl)
#   MIRRORS_ROOT    Mirror root dir (default: ../live_assets_eds)
#   EVAL_OUT_DIR    Output root (set by wrapper; default: out/suggestions_eval/<ts>)
#   MODE            visual_only | cwv_only | both | measure_only (default: both)
set -euo pipefail

HARNESS_LIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$(cd "$HARNESS_LIVE/../harness" && pwd)"
SCRIPT_DIR="$(cd "$HARNESS_LIVE/.." && pwd)"

PARALLEL="${PARALLEL:-8}"
NUM_RUNS="${NUM_RUNS:-5}"
BASE_PORT="${BASE_PORT:-14000}"
JSONL="${JSONL:-$HARNESS_LIVE/SAMPLE/live_filtered_top3.jsonl}"
MIRRORS_ROOT="${MIRRORS_ROOT:-$SCRIPT_DIR/live_assets_eds}"
LIMIT="${LIMIT:-}"
RESUME="${RESUME:-0}"
SKIP_MEASURE="${SKIP_MEASURE:-0}"
# MODE: visual_only | cwv_only | both (default) | measure_only (skip agent, use existing patch)
MODE="${MODE:-both}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --resume)        RESUME=1; shift ;;
    --skip-measure)  SKIP_MEASURE=1; shift ;;
    --jsonl)         shift; JSONL="$1"; shift ;;
    --limit)         shift; LIMIT="$1"; shift ;;
    --parallel)      shift; PARALLEL="$1"; shift ;;
    --mode)          shift; MODE="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ "$MODE" != "visual_only" && "$MODE" != "cwv_only" && "$MODE" != "both" && "$MODE" != "measure_only" ]]; then
  echo "Invalid MODE: $MODE (must be visual_only|cwv_only|both|measure_only)" >&2
  exit 1
fi

# Allow overriding agent via env var (default: static bench opencode OS direct)
AGENT_SCRIPT="${AGENT_SCRIPT:-$HARNESS/agents/template_opencode_os_direct.sh}"
AGENT_NAME="$(basename "$AGENT_SCRIPT" .sh)"
VISUAL_SCRIPT="$SCRIPT_DIR/src/regression_tool/visual_validate.py"
CWV_SCRIPT="$SCRIPT_DIR/src/cwv_tool/cwv_benchmark.py"
HOST_SCRIPT="$HARNESS_LIVE/host_files/host_static_mirror.sh"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
if [[ -n "${EVAL_OUT_DIR:-}" ]]; then
  OUT_ROOT="$EVAL_OUT_DIR"
else
  OUT_ROOT="$HARNESS_LIVE/out/suggestions_eval/$RUN_TS"
fi
TMP_ROOT="$HARNESS_LIVE/out/suggestions_tmp"
SUGG_INDEX_DIR="$TMP_ROOT/sugg_index_${RUN_TS}"

[[ "$MIRRORS_ROOT" = /* ]] || MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"

# Activate venv
[[ -f "$SCRIPT_DIR/.venv/bin/activate" ]] && source "$SCRIPT_DIR/.venv/bin/activate"

# Load .env
for _env in "$SCRIPT_DIR/.env" "$HARNESS_LIVE/.env"; do
  [[ -f "$_env" ]] && { set -a; source "$_env"; set +a; }
done
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"

mkdir -p "$TMP_ROOT" "$OUT_ROOT/results" "$SUGG_INDEX_DIR"

# ── Pre-index suggestions JSONL → one JSON file per row_id ───────────────────
echo "[live-sugg] Indexing $JSONL ..."
python3 - "$JSONL" "$SUGG_INDEX_DIR" "$MIRRORS_ROOT" << 'PY'
import json, sys, os
from urllib.parse import urlparse

jsonl_path, out_dir, mirrors_root = sys.argv[1], sys.argv[2], sys.argv[3]
count = 0

def url_to_mirror_dir(url):
    """Derive mirror dir using fetch_live_assets.py naming: <domain_slug>/<page_slug>."""
    import re as _re
    parsed = urlparse(url)
    # domain_slug: strip www. exactly (matches _domain_slug in fetch_live_assets.py)
    host = (parsed.netloc or parsed.path).rstrip("/")
    if host.startswith("www."):
        host = host[4:]
    # page_slug: matches _page_slug in fetch_live_assets.py
    path = parsed.path.strip("/") or "home"
    path = _re.sub(r'[^\w.\-/]', '_', path).replace('/', '__')
    if parsed.query:
        import hashlib as _hl
        path += '__' + _hl.md5(parsed.query.encode()).hexdigest()[:6]
    page_slug = path[:100] or "home"

    # Primary: domain/page_slug (what fetch_live_assets.py creates)
    primary = os.path.join(host, page_slug)
    if os.path.isdir(os.path.join(mirrors_root, primary)):
        return primary
    # Fallback: just domain dir (in case page is served at root without subdir)
    if os.path.isdir(os.path.join(mirrors_root, host)):
        return host
    return None

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
        url = d.get('input', {}).get('url', '') or ar.get('url', '') or ''
        # Some JSONL formats store url at top level
        if not url:
            url = d.get('url', '')

        mirror_dir = url_to_mirror_dir(url) if url else None

        out = {
            'row_id':      row_id,
            'url':         url,
            'mirror_dir':  mirror_dir,
            'suggestions': ar.get('suggestions', []),
            'cms_platform': ar.get('cms_platform', 'unknown'),
            'device_type': ar.get('device_type', ''),
        }
        with open(os.path.join(out_dir, f'{row_id}.json'), 'w', encoding='utf-8') as fo:
            json.dump(out, fo)
        count += 1

print(f'[live-sugg] Indexed {count} rows.')
PY

# ── Per-job function ──────────────────────────────────────────────────────────
run_job() {
  local ROW_ID="$1" SLOT="$2"
  local JOB_TMP="$TMP_ROOT/$ROW_ID"
  local PORT=$(( BASE_PORT + SLOT ))

  local SUGG_FILE="$SUGG_INDEX_DIR/${ROW_ID}.json"
  if [[ ! -f "$SUGG_FILE" ]]; then
    echo "[live-sugg] WARN: no index entry for row_id=$ROW_ID — skipping"
    return 0
  fi

  local URL MIRROR_DIR CMS SUGG_COUNT
  URL="$(python3 -c "import json; d=json.load(open('$SUGG_FILE')); print(d.get('url',''))" 2>/dev/null || echo "")"
  MIRROR_DIR="$(python3 -c "import json; d=json.load(open('$SUGG_FILE')); print(d.get('mirror_dir') or '')" 2>/dev/null || echo "")"
  CMS="$(python3 -c "import json; d=json.load(open('$SUGG_FILE')); print(d.get('cms_platform','unknown'))" 2>/dev/null || echo "unknown")"
  SUGG_COUNT="$(python3 -c "
import json
d = json.load(open('$SUGG_FILE'))
print(len(d.get('suggestions', [])))
" 2>/dev/null || echo 0)"

  if [[ "$SUGG_COUNT" -eq 0 ]]; then
    echo "[live-sugg] WARN: 0 suggestions for row_id=$ROW_ID — skipping"
    return 0
  fi

  if [[ -z "$MIRROR_DIR" ]]; then
    echo "[live-sugg] WARN: no mirror dir found for URL=$URL (row_id=$ROW_ID) — skipping"
    return 0
  fi

  local MIRROR_ABS="$MIRRORS_ROOT/$MIRROR_DIR"
  if [[ ! -d "$MIRROR_ABS" ]]; then
    echo "[live-sugg] WARN: mirror dir not found: $MIRROR_ABS (row_id=$ROW_ID) — skipping"
    return 0
  fi

  echo "====== Job row_id=$ROW_ID | $URL | sugg_count=$SUGG_COUNT | slot=$SLOT ======"
  mkdir -p "$JOB_TMP"

  # ── Clone baseline: copy mirror + git init ──────────────────────────────────
  local BASELINE_DIR="$JOB_TMP/baseline"
  rm -rf "$BASELINE_DIR"
  cp -r "$MIRROR_ABS" "$BASELINE_DIR"
  git -C "$BASELINE_DIR" init -q
  git -C "$BASELINE_DIR" add -A
  git -C "$BASELINE_DIR" commit -qm "baseline" 2>/dev/null || true

  local FW
  FW="$(echo "${CMS:-unknown}" | tr '[:upper:]' '[:lower:]' | tr ' ' '_')"

  # ── Process each suggestion ──────────────────────────────────────────────────
  local SUGG_IDX
  for SUGG_IDX in $(seq 0 $(( SUGG_COUNT - 1 ))); do
    local JOB_LABEL="${ROW_ID}_s${SUGG_IDX}_${AGENT_NAME}"
    local OUT_DIR="$OUT_ROOT/results/$JOB_LABEL"
    local PATCH_FILE="$OUT_DIR/${JOB_LABEL}.patch"

    mkdir -p "$OUT_DIR"

    # Write source metadata
    python3 -c "
import json
d = json.load(open('$SUGG_FILE'))
meta = {'row_id': '$ROW_ID', 'url': d.get('url',''), 'mirror_dir': '$MIRROR_DIR', 'cms_platform': d.get('cms_platform','')}
with open('$OUT_DIR/baseline_meta.json', 'w') as f: json.dump(meta, f, indent=2)
" 2>/dev/null || true

    # Resume check
    if [[ "$RESUME" == "1" ]]; then
      if [[ "$SKIP_MEASURE" == "1" ]]; then
        if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
          echo "[live-sugg] SKIP (resume): patch exists $ROW_ID s$SUGG_IDX"
          continue
        fi
      elif [[ "$MODE" == "cwv_only" ]]; then
        if [[ -f "$OUT_DIR/baseline_mobile.json" && -f "$OUT_DIR/baseline_desktop.json" && \
              -f "$OUT_DIR/mobile.json" && -f "$OUT_DIR/desktop.json" ]]; then
          echo "[live-sugg] SKIP (resume): CWV done $ROW_ID s$SUGG_IDX"
          continue
        fi
      elif [[ "$MODE" == "measure_only" ]]; then
        if [[ -f "$OUT_DIR/visual.json" ]]; then
          echo "[live-sugg] SKIP (resume): visual done $ROW_ID s$SUGG_IDX"
          continue
        fi
      else
        if [[ -f "$OUT_DIR/visual.json" ]]; then
          echo "[live-sugg] SKIP (resume): already evaluated $ROW_ID s$SUGG_IDX"
          continue
        fi
      fi
    fi

    # cwv_only: only if visual already done
    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ ! -f "$OUT_DIR/visual.json" ]]; then
        echo "[live-sugg] SKIP cwv_only: no visual.json yet ($ROW_ID s$SUGG_IDX)"
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
    raise SystemExit(f"index {idx} out of range ({len(suggs)} suggestions)")
with open(sys.argv[3], 'w') as f:
    json.dump(suggs[idx], f, indent=2)
PY
    cp "$SUGG_ITEM_FILE" "$OUT_DIR/input_suggestion.json"

    # Fresh working copy
    local WORK_DIR="$JOB_TMP/s${SUGG_IDX}"
    rm -rf "$WORK_DIR"
    cp -r --no-preserve=mode "$BASELINE_DIR" "$WORK_DIR"

    # ── Run agent (skip for cwv_only and measure_only) ──────────────────────
    if [[ "$MODE" != "cwv_only" && "$MODE" != "measure_only" ]]; then
      export EVAL_SUGGESTION_FILE="$SUGG_ITEM_FILE"
      export EVAL_SUGGESTION_INDEX="$SUGG_IDX"
      export EVAL_JOB_LABEL="$JOB_LABEL"
      export EVAL_JOB_ID="$ROW_ID"
      export EVAL_AGENT_NAME="$AGENT_NAME"
      export EVAL_CWV_DATA_FILE=""
      export FRAMEWORK="$FW"
      # Export live-bench context vars for agent templates that need them
      export PAGE_URL="$URL"
      export DOMAIN="$(python3 -c "from urllib.parse import urlparse; u=urlparse('$URL'); h=u.netloc or u.path; print(h[4:] if h.startswith('www.') else h)" 2>/dev/null || echo "$URL")"
      export CWV_FIELD_MOBILE="${CWV_FIELD_MOBILE:-null}"
      export CWV_FIELD_DESKTOP="${CWV_FIELD_DESKTOP:-null}"
      export CWV_SYNTHETIC_MOBILE="${CWV_SYNTHETIC_MOBILE:-null}"
      export CWV_SYNTHETIC_DESKTOP="${CWV_SYNTHETIC_DESKTOP:-null}"
      export LCP_ENTRIES_MOBILE="${LCP_ENTRIES_MOBILE:-null}"
      export LCP_ENTRIES_DESKTOP="${LCP_ENTRIES_DESKTOP:-null}"

      local _AGENT_T0=$SECONDS
      bash "$AGENT_SCRIPT" \
        "$WORK_DIR" "" "$OUT_DIR/agent.log" "$PATCH_FILE" \
        </dev/null \
        || echo "[live-sugg] WARN: agent non-zero ($ROW_ID s$SUGG_IDX)"
      local _AGENT_WALL=$(( SECONDS - _AGENT_T0 ))

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
    fi

    # ── Patch-only mode ─────────────────────────────────────────────────────
    if [[ "$SKIP_MEASURE" == "1" ]]; then
      echo "[live-sugg] --skip-measure: patch saved ($ROW_ID s$SUGG_IDX)"
      rm -rf "$WORK_DIR"
      echo "[live-sugg] ✓ $ROW_ID s$SUGG_IDX (patch only)"
      continue
    fi

    # ── Baseline CWV (cwv_only: measure unpatched mirror before applying patch) ─
    if [[ "$MODE" == "cwv_only" ]]; then
      if [[ ! -f "$OUT_DIR/baseline_mobile.json" || ! -f "$OUT_DIR/baseline_desktop.json" ]]; then
        fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
        for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done
        PORT="$PORT" setsid bash "$HOST_SCRIPT" "$WORK_DIR" "$OUT_DIR/baseline_host.log" &
        local BASELINE_HOST_PID=$!
        if wait_for_server "$PORT" 90; then
          python3 "$CWV_SCRIPT" \
            --device mobile  --num-runs "$NUM_RUNS" \
            --url "http://localhost:$PORT" \
            > "$OUT_DIR/baseline_mobile.json"  2>>"$OUT_DIR/cwv_stderr.txt" || true
          python3 "$CWV_SCRIPT" \
            --device desktop --num-runs "$NUM_RUNS" \
            --url "http://localhost:$PORT" \
            > "$OUT_DIR/baseline_desktop.json" 2>>"$OUT_DIR/cwv_stderr.txt" || true
        else
          echo "[live-sugg] WARN: baseline server never ready ($ROW_ID s$SUGG_IDX)"
        fi
        kill -- -"$BASELINE_HOST_PID" 2>/dev/null || kill "$BASELINE_HOST_PID" 2>/dev/null || true
        wait "$BASELINE_HOST_PID" 2>/dev/null || true
        fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
        for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done
      fi
    fi

    # ── Apply patch ──────────────────────────────────────────────────────────
    if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
      git -C "$WORK_DIR" apply --whitespace=nowarn "$PATCH_FILE" >/dev/null 2>&1 \
        || echo "[live-sugg] WARN: patch apply failed ($ROW_ID s$SUGG_IDX)"
    else
      echo "[live-sugg] WARN: empty/missing patch ($ROW_ID s$SUGG_IDX) — measuring baseline"
      touch "$PATCH_FILE"
    fi

    # ── Start HTTP server ────────────────────────────────────────────────────
    fuser -k -KILL "$PORT/tcp" 2>/dev/null || true
    for _w in $(seq 1 20); do fuser "$PORT/tcp" >/dev/null 2>&1 || break; sleep 0.5; done
    PORT="$PORT" setsid bash "$HOST_SCRIPT" "$WORK_DIR" "$OUT_DIR/host.log" &
    local HOST_PID=$!

    if ! wait_for_server "$PORT" 90; then
      echo "[live-sugg] ERROR: server never ready ($ROW_ID s$SUGG_IDX)"
      kill -- -"$HOST_PID" 2>/dev/null || kill "$HOST_PID" 2>/dev/null || true
      rm -rf "$WORK_DIR"
      continue
    fi

    # ── Visual validation ────────────────────────────────────────────────────
    local VISUAL_REGRESSED=0
    if [[ "$MODE" == "visual_only" || "$MODE" == "both" || "$MODE" == "measure_only" ]]; then
      timeout 480 python3 "$VISUAL_SCRIPT" \
        --url             "http://localhost:$PORT" \
        --screenshot-path "$OUT_DIR/screenshot.png" \
        --repo-id         "$URL" \
        --commit-id       "" \
        --framework       "${FW:-static html}" \
        --patch-file      "$PATCH_FILE" \
        --output-json     "$OUT_DIR/visual.json" \
        2>>"$OUT_DIR/visual.stderr" \
        || echo "[live-sugg] WARN: visual failed ($ROW_ID s$SUGG_IDX)"

      if [[ -f "$OUT_DIR/visual.json" ]]; then
        VISUAL_REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
      fi
    fi

    # ── CWV measurement ──────────────────────────────────────────────────────
    if [[ "$MODE" == "cwv_only" || "$MODE" == "both" || "$MODE" == "measure_only" ]]; then
      if [[ "$VISUAL_REGRESSED" == "1" ]]; then
        echo "[live-sugg] Skipping CWV — visual regression ($ROW_ID s$SUGG_IDX)"
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
    echo "[live-sugg] ✓ $ROW_ID s$SUGG_IDX"
  done

  rm -rf "$JOB_TMP"
  echo "✓ Done: $ROW_ID ($SUGG_COUNT suggestions)"
}

# ── Wait helper for server ────────────────────────────────────────────────────
wait_for_server() {
  local port="$1" timeout="${2:-90}" i
  for i in $(seq 1 "$timeout"); do
    curl -fs "http://localhost:${port}/" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# ── Job pool ──────────────────────────────────────────────────────────────────
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

# Kill any zombie servers in our port range
for _p in $(seq "$BASE_PORT" $(( BASE_PORT + PARALLEL - 1 ))); do
  fuser -k -KILL "$_p/tcp" 2>/dev/null || true
done

echo "[live-sugg] JSONL:          $JSONL"
echo "[live-sugg] MIRRORS_ROOT:   $MIRRORS_ROOT"
echo "[live-sugg] Output root:    $OUT_ROOT"
echo "[live-sugg] Agent:          $AGENT_NAME"
echo "[live-sugg] MODE=$MODE  PARALLEL=$PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
[[ -n "$LIMIT" ]]             && echo "[live-sugg] LIMIT=$LIMIT"
[[ "$RESUME" == "1" ]]        && echo "[live-sugg] --resume: skipping already-evaluated jobs"
[[ "$SKIP_MEASURE" == "1" ]]  && echo "[live-sugg] --skip-measure: patch only (no server/CWV)"
[[ "$MODE" == "measure_only" ]] && echo "[live-sugg] measure_only: using existing patches, skipping agent"

# ── Dispatch loop ─────────────────────────────────────────────────────────────
while IFS=$'\t' read -r ROW_ID; do
  acquire_slot
  slot=$_SLOT
  ( run_job "$ROW_ID" "$slot" ) &
  JOB_SLOT[$!]=$slot
done < <(python3 - "$JSONL" "${LIMIT:-}" << 'PY'
import json, sys

jsonl_path = sys.argv[1]
limit_s    = sys.argv[2] if len(sys.argv) > 2 else ""
limit      = int(limit_s) if limit_s else None

n = 0
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
        print(row_id)
        n += 1
        if limit and n >= limit:
            break
PY
)

wait
echo ""
echo "[live-sugg] All jobs complete."
echo "[live-sugg] Results: $OUT_ROOT/results/"
