#!/usr/bin/env bash
# =============================================================================
# run_evaluation.sh — Stage 2: Evaluate agent patches
# =============================================================================
#
# Takes patches produced by run_agents.sh (Stage 1), re-extracts each repo
# snapshot, applies the patch, hosts the site, measures CWV (mobile + desktop),
# and runs visual validation.
#
# Usage:
#   ./harness/scripts/run_evaluation.sh --run-dir harness/out/TIMESTAMP [OPTIONS]
#
# Required:
#   --run-dir DIR           Run directory from Stage 1 (contains patches/ and logs/)
#
# Options:
#   --csv PATH              Input CSV (default: SAMPLE/input.csv)
#   --port PORT             Localhost port for hosting (default: 4000)
#   --num-runs N            CWV measurement runs per device (default: 5)
#   --skip-visual           Skip visual validation
#   --agents AGENTS         Comma-separated agent filter (default: evaluate all patches)
#   --limit N               Only process the first N repos
#   --help                  Show this message
#
# Output:
#   <run-dir>/results/{ID}_{AGENT}_mobile.json
#   <run-dir>/results/{ID}_{AGENT}_desktop.json
#   <run-dir>/results/{ID}_{AGENT}_screenshot.png
#   <run-dir>/results/{ID}_{AGENT}_visual.json
# =============================================================================
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAMPLE_DIR="$HARNESS_DIR/SAMPLE"

RUN_DIR=""
CSV_PATH=""
PORT=""
NUM_RUNS=""
SKIP_VISUAL=0
AGENTS_FILTER=""
LIMIT=""

usage() {
  sed -n '/^# Usage:/,/^# ====/p' "$0" | grep '^#' | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)     shift; RUN_DIR="$1" ;;
    --csv)         shift; CSV_PATH="$1" ;;
    --port)        shift; PORT="$1" ;;
    --num-runs)    shift; NUM_RUNS="$1" ;;
    --skip-visual) SKIP_VISUAL=1 ;;
    --agents)      shift; AGENTS_FILTER="$1" ;;
    --limit)       shift; LIMIT="$1" ;;
    --help|-h)     usage ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ -z "$RUN_DIR" ]]; then
  echo "ERROR: --run-dir is required"
  echo "Example: $0 --run-dir harness/out/20260403_120000"
  exit 1
fi

PATCHES_DIR="$RUN_DIR/patches"
if [[ ! -d "$PATCHES_DIR" ]]; then
  echo "ERROR: No patches/ directory in $RUN_DIR"
  exit 1
fi

# ── Defaults ──────────────────────────────────────────────────────────────────
[[ -z "$CSV_PATH" ]] && CSV_PATH="$SAMPLE_DIR/input.csv"

# Save overrides, load .env, restore
_OVERRIDE_PORT="$PORT"
_OVERRIDE_NUM_RUNS="$NUM_RUNS"

if [[ -f "$HARNESS_DIR/.env" ]]; then
  set -a; source "$HARNESS_DIR/.env"; set +a
fi

[[ -n "$_OVERRIDE_PORT" ]]     && PORT="$_OVERRIDE_PORT"
[[ -n "$_OVERRIDE_NUM_RUNS" ]] && NUM_RUNS="$_OVERRIDE_NUM_RUNS"
PORT="${PORT:-4000}"
NUM_RUNS="${NUM_RUNS:-5}"

CWV_SCRIPT="$HARNESS_DIR/../scripts/helper_scripts/cwv_benchmark.py"
VISUAL_SCRIPT="$HARNESS_DIR/visual_validate.py"

RESULTS_DIR="$RUN_DIR/results"
TMP_ROOT="$RUN_DIR/eval_tmp"

# ── Sanity checks ─────────────────────────────────────────────────────────────
[[ -f "$CSV_PATH" ]]      || { echo "Missing CSV: $CSV_PATH"; exit 1; }
[[ -f "$CWV_SCRIPT" ]]    || { echo "Missing cwv_benchmark.py: $CWV_SCRIPT"; exit 1; }
[[ -f "$VISUAL_SCRIPT" ]] || { echo "Missing visual_validate.py: $VISUAL_SCRIPT"; exit 1; }

mkdir -p "$RESULTS_DIR" "$TMP_ROOT"

# Build agent filter set (empty means accept all)
declare -A AGENT_FILTER_SET
if [[ -n "$AGENTS_FILTER" ]]; then
  IFS=',' read -ra _agents <<< "$AGENTS_FILTER"
  for a in "${_agents[@]}"; do
    AGENT_FILTER_SET["$(basename "$a" .sh)"]=1
  done
fi

echo "═══════════════════════════════════════════"
echo " Stage 2: Evaluate Patches"
echo "═══════════════════════════════════════════"
echo " Run dir:  $RUN_DIR"
echo " CSV:      $CSV_PATH"
echo " Port:     $PORT"
echo " Num runs: $NUM_RUNS"
echo "═══════════════════════════════════════════"

# ── Unzip + normalize helper (same as run_agents.sh) ─────────────────────────
unzip_and_normalize() {
  local zip_path="$1" run_dir="$2" repo_dir="$3"
  unzip -q "$zip_path" -d "$run_dir"

  local items=("$run_dir"/*) top_items=()
  for item in "${items[@]}"; do
    [[ "$item" == "$repo_dir" ]] && continue
    top_items+=("$item")
  done

  if [[ -d "$repo_dir" && "$(ls -A "$repo_dir" 2>/dev/null)" ]]; then
    :
  elif [[ ${#top_items[@]} -eq 1 && -d "${top_items[0]}" ]]; then
    shopt -s dotglob nullglob
    mv "${top_items[0]}"/* "$repo_dir"
    shopt -u dotglob nullglob
    rmdir "${top_items[0]}" 2>/dev/null || true
  else
    for item in "${top_items[@]}"; do
      mv "$item" "$repo_dir"
    done
  fi
}

# ── CSV iteration ─────────────────────────────────────────────────────────────
TOTAL=0
EVALUATED=0
SKIPPED=0

while IFS=$'\t' read -r \
  ID REPO_ID FRAMEWORK COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH \
  CWV_MOBILE CWV_DESKTOP LCP_ENTRIES_DESKTOP LCP_ENTRIES_MOBILE \
  CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP
do
  # Find all patches for this ID
  shopt -s nullglob
  PATCH_FILES=("$PATCHES_DIR/${ID}_"*.patch)
  shopt -u nullglob

  if [[ ${#PATCH_FILES[@]} -eq 0 ]]; then
    continue
  fi

  for PATCH_FILE in "${PATCH_FILES[@]}"; do
    PATCH_BASENAME="$(basename "$PATCH_FILE" .patch)"
    # Extract agent name: {ID}_{AGENT_NAME} → AGENT_NAME
    AGENT_NAME="${PATCH_BASENAME#${ID}_}"

    # Apply agent filter
    if [[ ${#AGENT_FILTER_SET[@]} -gt 0 && -z "${AGENT_FILTER_SET[$AGENT_NAME]:-}" ]]; then
      continue
    fi

    # Skip empty patches
    if [[ ! -s "$PATCH_FILE" ]]; then
      echo "[eval] Skipping $PATCH_BASENAME (empty patch)"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi

    echo ""
    echo "======================================"
    echo "ID=$ID  Repo=$REPO_ID  Agent=$AGENT_NAME"
    echo "======================================"

    EVAL_DIR="$TMP_ROOT/${ID}_${AGENT_NAME}"
    REPO_DIR="$EVAL_DIR/repo"

    # Hard cleanup
    pkill -f "jekyll serve" 2>/dev/null || true
    pkill -f "http.server $PORT" 2>/dev/null || true
    rm -rf "$EVAL_DIR"
    mkdir -p "$EVAL_DIR" "$REPO_DIR"

    # ── 1) Resolve snapshot ──
    if [[ -z "$ZIP_REPO_PATH" || "$ZIP_REPO_PATH" == " " ]]; then
      ZIP_REPO_PATH="REPO_SNAPSHOTS/$(echo "$REPO_ID" | tr '/' '_').zip"
    fi
    ZIP_ABS_PATH="$SAMPLE_DIR/$ZIP_REPO_PATH"

    if [[ ! -f "$ZIP_ABS_PATH" ]]; then
      echo "ERROR: Missing snapshot $ZIP_REPO_PATH — skipping"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi

    # ── 2) Unzip + normalize ──
    unzip_and_normalize "$ZIP_ABS_PATH" "$EVAL_DIR" "$REPO_DIR"

    # ── 3) Git baseline ──
    if [[ ! -d "$REPO_DIR/.git" ]]; then
      git -C "$REPO_DIR" init -q
      git -C "$REPO_DIR" add -A
      git -C "$REPO_DIR" commit -qm "baseline" || true
    fi

    # ── 4) Apply patch ──
    (
      set +e
      cd "$REPO_DIR" || exit 0
      git apply "$PATCH_FILE" >/dev/null 2>&1 || {
        git apply --3way "$PATCH_FILE" >/dev/null 2>&1 || {
          echo "[eval] WARN: Could not apply patch for $PATCH_BASENAME"
        }
      }
    )

    # ── 5) Launch host ──
    HOST_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_host.log"
    PORT="$PORT" bash "$HARNESS_DIR/$HOST_FILE_PATH" "$REPO_DIR" "$HOST_LOG" &
    HOST_PID=$!

    READY=0
    for _ in {1..90}; do
      if curl -fs "http://localhost:$PORT/" >/dev/null; then
        READY=1
        break
      fi
      sleep 1
    done

    if [[ "$READY" -ne 1 ]]; then
      echo "ERROR: Site never became ready (ID=$ID Agent=$AGENT_NAME)"
      tail -n 50 "$HOST_LOG" 2>/dev/null || true
      kill "$HOST_PID" 2>/dev/null || true
      rm -rf "$EVAL_DIR"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi

    # ── 6) Measure CWV (mobile + desktop) ──
    RESULT_MOBILE="$RESULTS_DIR/${ID}_${AGENT_NAME}_mobile.json"
    RESULT_DESKTOP="$RESULTS_DIR/${ID}_${AGENT_NAME}_desktop.json"
    CWV_STDERR="$RESULTS_DIR/${ID}_${AGENT_NAME}_cwv_stderr.txt"

    python3 "$CWV_SCRIPT" --device mobile --num-runs "$NUM_RUNS" \
      --url "http://localhost:$PORT/" \
      > "$RESULT_MOBILE" 2>> "$CWV_STDERR" || true

    python3 "$CWV_SCRIPT" --device desktop --num-runs "$NUM_RUNS" \
      --url "http://localhost:$PORT/" \
      > "$RESULT_DESKTOP" 2>> "$CWV_STDERR" || true

    # ── 7) Visual validation ──
    if [[ "$SKIP_VISUAL" -eq 0 ]]; then
      SCREENSHOT_PATH="$RESULTS_DIR/${ID}_${AGENT_NAME}_screenshot.png"
      VISUAL_JSON="$RESULTS_DIR/${ID}_${AGENT_NAME}_visual.json"
      python3 "$VISUAL_SCRIPT" \
        --url "http://localhost:$PORT/" \
        --screenshot-path "$SCREENSHOT_PATH" \
        --repo-id "$REPO_ID" \
        --output-json "$VISUAL_JSON" \
        || echo "[visual] Validation failed (continuing)"
    fi

    # ── 8) Teardown ──
    kill "$HOST_PID" 2>/dev/null || true
    wait "$HOST_PID" 2>/dev/null || true
    rm -rf "$EVAL_DIR"

    EVALUATED=$((EVALUATED + 1))
    echo "✓ Evaluated: ID=$ID Agent=$AGENT_NAME"
  done

  TOTAL=$((TOTAL + 1))
done < <(python3 - <<'PY' "$CSV_PATH" "$LIMIT"
import csv, sys
csv.field_size_limit(sys.maxsize)
csv_path = sys.argv[1]
limit_s = sys.argv[2] if len(sys.argv) > 2 else ""
limit = int(limit_s) if limit_s else None
cols = [
  "ID","REPO_ID","FRAMEWORK","COMMIT_ID","ZIP_REPO_PATH","HOST_FILE_PATH",
  "CWV_MOBILE","CWV_DESKTOP","LCP_ENTRIES_DESKTOP","LCP_ENTRIES_MOBILE",
  "CLS_SHIFTS_MOBILE","CLS_SHIFTS_DESKTOP","INP_INTERACTIONS_MOBILE","INP_INTERACTIONS_DESKTOP"
]
n = 0
with open(csv_path, newline="", encoding="utf-8") as f:
  r = csv.DictReader(f)
  for row in r:
    out = []
    for c in cols:
      v = row.get(c, "")
      if v is None: v = ""
      v = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
      if v == "": v = " "
      out.append(v)
    print("\t".join(out))
    n += 1
    if limit is not None and n >= limit:
      break
PY
)

rmdir "$TMP_ROOT" 2>/dev/null || true

echo ""
echo "═══════════════════════════════════════════"
echo " Stage 2 complete: $EVALUATED evaluated, $SKIPPED skipped"
echo " Results: $RESULTS_DIR"
echo "═══════════════════════════════════════════"
