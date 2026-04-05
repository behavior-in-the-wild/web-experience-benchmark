#!/usr/bin/env bash
set -euo pipefail

AUTO_SNAPSHOT=0
LIMIT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto-snapshot) AUTO_SNAPSHOT=1; shift ;;
    --limit)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --limit N"; exit 1; }
      LIMIT="$1"
      shift
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# =========================
# Resolve paths
# =========================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAMPLE_DIR="$(cd "$SCRIPT_DIR/SAMPLE" && pwd)"

# input.csv
RUN_TS="$(date +%Y%m%d_%H%M%S)"
CSV="$SAMPLE_DIR/input.csv"
TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv_debug.txt"

TMP_ROOT="$SCRIPT_DIR/out/${RUN_TS}/run"
RESULTS_DIR="$SCRIPT_DIR/out/${RUN_TS}/results"

CWV_SCRIPT="$SCRIPT_DIR/../scripts/helper_scripts/cwv_benchmark.py"
VISUAL_SCRIPT="$SCRIPT_DIR/visual_validate.py"

# Save command-line overrides before .env (so DEVICE=desktop ./evaluate.sh wins over .env)
_OVERRIDE_DEVICE="${DEVICE:-}"
_OVERRIDE_PORT="${PORT:-}"
_OVERRIDE_NUM_RUNS="${NUM_RUNS:-}"

# =========================
# Load environment
# =========================
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Restore command-line overrides; then apply defaults for any still unset
[[ -n "$_OVERRIDE_DEVICE" ]] && DEVICE="$_OVERRIDE_DEVICE"
[[ -n "$_OVERRIDE_PORT" ]] && PORT="$_OVERRIDE_PORT"
[[ -n "$_OVERRIDE_NUM_RUNS" ]] && NUM_RUNS="$_OVERRIDE_NUM_RUNS"
PORT="${PORT:-4000}"
export AZURE_DEPLOYMENT="gpt-4.1"
DEVICE="${DEVICE:-desktop}"     # mobile|desktop
NUM_RUNS="${NUM_RUNS:-5}"

# =========================
# Agents to benchmark
# =========================
AGENTS=(
  # "agents/template_null.sh"
  # "agents/template_codex.sh"   # requires: npm install -g @openai/codex
  # "agents/template_aider.sh"
  # "agents/template_opencode.sh"
  # "agents/template_opencodegpt51codex.sh"
  # "agents/template_gemini.sh"
  # "agents/template_claudecode.sh"
  "agents/template_cwvoptimizer.sh"
)

# =========================
# Sanity checks
# =========================
[[ -f "$CSV" ]] || { echo "Missing CSV: $CSV"; exit 1; }
[[ -f "$TASK_SPEC" ]] || { echo "Missing task spec: $TASK_SPEC"; exit 1; }
[[ -f "$CWV_SCRIPT" ]] || { echo "Missing cwv_benchmark.py: $CWV_SCRIPT"; exit 1; }
[[ -f "$VISUAL_SCRIPT" ]] || { echo "Missing visual_validate.py: $VISUAL_SCRIPT"; exit 1; }

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"
echo "[run] Input: $CSV"
echo "[run] Output: $RESULTS_DIR"
echo "[run] Devices: mobile+desktop PORT=$PORT NUM_RUNS=$NUM_RUNS"
[[ -n "$LIMIT" ]] && echo "[run] LIMIT=$LIMIT"

# =========================
# Snapshot helper
# =========================
clone_and_zip_repo() {
  local github_repo="$1"
  local commit_id="$2"
  local zip_path="$3"
  local work_dir="$4"

  rm -rf "$work_dir"
  git clone "https://github.com/$github_repo.git" "$work_dir" >/dev/null 2>&1 \
    || { echo "[snapshot] ERROR: failed to clone $github_repo"; return 1; }

  if [[ -n "$commit_id" && "$commit_id" != "null" ]]; then
    git -C "$work_dir" checkout "$commit_id" >/dev/null 2>&1 \
      || { echo "[snapshot] ERROR: failed to checkout $commit_id"; return 1; }
  fi

  mkdir -p "$(dirname "$zip_path")"
  (cd "$work_dir" && zip -qr "$zip_path" .) \
    || { echo "[snapshot] ERROR: failed to zip repo"; return 1; }

  echo "[snapshot] Created $zip_path"
}

# =========================
# CSV iteration (robust)
# =========================
# We use Python csv.DictReader so quoted JSON with commas does not break parsing.
while IFS=$'\t' read -r \
  ID REPO_ID FRAMEWORK COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH \
  CWV_MOBILE CWV_DESKTOP LCP_ENTRIES_DESKTOP LCP_ENTRIES_MOBILE \
  CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP
do
  for AGENT in "${AGENTS[@]}"; do
    AGENT_NAME="$(basename "$AGENT" .sh)"

    echo "======================================"
    echo "ID=$ID Repo=$REPO_ID Agent=$AGENT_NAME"
    echo "======================================"

    RUN_DIR="$TMP_ROOT/${ID}_${AGENT_NAME}"
    REPO_DIR="$RUN_DIR/repo"

    # Hard cleanup
    pkill -f "jekyll serve" 2>/dev/null || true
    pkill -f "http.server $PORT" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    mkdir -p "$RUN_DIR" "$REPO_DIR"

    # -------------------------
    # 1) Ensure snapshot exists
    # -------------------------
    if [[ -z "$ZIP_REPO_PATH" || "$ZIP_REPO_PATH" == " " ]]; then
      ZIP_REPO_PATH="REPO_SNAPSHOTS/$(echo "$REPO_ID" | tr '/' '_').zip"
    fi
    ZIP_ABS_PATH="$SAMPLE_DIR/$ZIP_REPO_PATH"

    if [[ ! -f "$ZIP_ABS_PATH" ]]; then
      if [[ "$AUTO_SNAPSHOT" -eq 1 ]]; then
        echo "[snapshot] Missing ZIP, auto-snapshot enabled"
        SNAPSHOT_TMP="$RUN_DIR/_snapshot_tmp"
        clone_and_zip_repo "$REPO_ID" "$COMMIT_ID" "$ZIP_ABS_PATH" "$SNAPSHOT_TMP" || {
          echo "[snapshot] Failed; skipping repo"
          continue
        }
      else
        echo "ERROR: Missing snapshot $ZIP_REPO_PATH"
        echo "       Re-run with --auto-snapshot to clone+zip automatically"
        continue
      fi
    fi

    # -------------------------
    # 2) Unzip snapshot + normalize
    # -------------------------
    unzip -q "$ZIP_ABS_PATH" -d "$RUN_DIR"

    ITEMS=("$RUN_DIR"/*)
    TOP_ITEMS=()
    for item in "${ITEMS[@]}"; do
      [[ "$item" == "$REPO_DIR" ]] && continue
      TOP_ITEMS+=("$item")
    done

    if [[ -d "$REPO_DIR" && "$(ls -A "$REPO_DIR" 2>/dev/null)" ]]; then
      :
    elif [[ ${#TOP_ITEMS[@]} -eq 1 && -d "${TOP_ITEMS[0]}" ]]; then
      shopt -s dotglob nullglob
      mv "${TOP_ITEMS[0]}"/* "$REPO_DIR"
      shopt -u dotglob nullglob
      rmdir "${TOP_ITEMS[0]}" 2>/dev/null || true
    else
      for item in "${TOP_ITEMS[@]}"; do
        mv "$item" "$REPO_DIR"
      done
    fi

    # -------------------------
    # 3) Initialize git baseline
    # -------------------------
    if [[ ! -d "$REPO_DIR/.git" ]]; then
      git -C "$REPO_DIR" init -q
      git -C "$REPO_DIR" add -A
      git -C "$REPO_DIR" commit -qm "baseline" || true
    fi

    # -------------------------
    # 4) Export context (CSV baselines — both mobile and desktop)
    # -------------------------
    export FRAMEWORK="$(echo "${FRAMEWORK:-unknown}" | tr '[:upper:]' '[:lower:]')"
    export REPO_ID
    export CWV_BASELINE_MOBILE="${CWV_MOBILE:-}"
    export LCP_ENTRIES_MOBILE="${LCP_ENTRIES_MOBILE:-}"
    export CWV_BASELINE_DESKTOP="${CWV_DESKTOP:-}"
    export LCP_ENTRIES_DESKTOP="${LCP_ENTRIES_DESKTOP:-}"
    export CLS_SHIFTS_MOBILE="${CLS_SHIFTS_MOBILE:-}"
    export CLS_SHIFTS_DESKTOP="${CLS_SHIFTS_DESKTOP:-}"
    export INP_INTERACTIONS_MOBILE="${INP_INTERACTIONS_MOBILE:-}"
    export INP_INTERACTIONS_DESKTOP="${INP_INTERACTIONS_DESKTOP:-}"

    # -------------------------
    # 5) Run agent
    # -------------------------
    AGENT_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_agent.log"
    PATCH_FILE="$RESULTS_DIR/${ID}_${AGENT_NAME}.patch"

    bash "$SCRIPT_DIR/$AGENT" \
      "$REPO_DIR" \
      "$TASK_SPEC" \
      "$AGENT_LOG" \
      "$PATCH_FILE" \
      </dev/null \
      || echo "[agent] Agent failed (continuing)"

    # -------------------------
    # 6) Normalize patch (baseline + patch only)
    # -------------------------
    if [[ -d "$REPO_DIR/.git" ]]; then
      (
        set +e
        cd "$REPO_DIR" || exit 0

        # If agent didn't write patch, capture diff
        if [[ ! -s "$PATCH_FILE" ]]; then
          git add -A >/dev/null 2>&1
          git diff --cached > "$PATCH_FILE" 2>/dev/null || true
        fi

        git reset --hard HEAD >/dev/null 2>&1 || true
        git clean -fd >/dev/null 2>&1 || true

        [[ -s "$PATCH_FILE" ]] && git apply "$PATCH_FILE" >/dev/null 2>&1 || true
      )
    fi

    # If requested, stop after producing the patch and skip
    # host launch, CWV measurement, and visual validation.
    if [[ "${SKIP_CWV_MEASURE:-0}" == "1" ]]; then
      echo "[run] SKIP_CWV_MEASURE=1; skipping host launch and CWV measurement for ID=$ID Agent=$AGENT_NAME"
      rm -rf "$RUN_DIR"
      echo "✓ Done: ID=$ID Agent=$AGENT_NAME"
      continue
    fi

    # -------------------------
    # 7) Launch host
    # -------------------------
    HOST_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_host.log"
    PORT="$PORT" bash "$SCRIPT_DIR/$HOST_FILE_PATH" "$REPO_DIR" "$HOST_LOG" &
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
      continue
    fi

    # -------------------------
    # 8) Measure CWV (post-patch) — mobile and desktop (separate JSON files)
    # -------------------------
    RESULT_MOBILE="$RESULTS_DIR/${ID}_${AGENT_NAME}_mobile.json"
    RESULT_DESKTOP="$RESULTS_DIR/${ID}_${AGENT_NAME}_desktop.json"
    CWV_STDERR="$RESULTS_DIR/${ID}_${AGENT_NAME}_cwv_stderr.txt"

    python3 "$CWV_SCRIPT" --device mobile --num-runs "$NUM_RUNS" --url "http://localhost:$PORT/" \
      > "$RESULT_MOBILE" 2>> "$CWV_STDERR" || true
    python3 "$CWV_SCRIPT" --device desktop --num-runs "$NUM_RUNS" --url "http://localhost:$PORT/" \
      > "$RESULT_DESKTOP" 2>> "$CWV_STDERR" || true

    echo "RESULT_MOBILE=$RESULT_MOBILE"
    echo "RESULT_DESKTOP=$RESULT_DESKTOP"

    # -------------------------
    # 8b) Visual validation (screenshot + AI eval)
    # -------------------------
    SCREENSHOT_PATH="$RESULTS_DIR/${ID}_${AGENT_NAME}_screenshot.png"
    VISUAL_JSON="$RESULTS_DIR/${ID}_${AGENT_NAME}_visual.json"
    python3 "$VISUAL_SCRIPT" \
      --url "http://localhost:$PORT/" \
      --screenshot-path "$SCREENSHOT_PATH" \
      --repo-id "$REPO_ID" \
      --output-json "$VISUAL_JSON" \
      || echo "[visual] Validation failed (continuing)"

    # -------------------------
    # 9) Teardown
    # -------------------------
    kill "$HOST_PID" 2>/dev/null || true
    wait "$HOST_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"

    echo "✓ Done: ID=$ID Agent=$AGENT_NAME"
  done
done < <(python3 - <<'PY' "$CSV" "$LIMIT"
import csv, sys
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
      if v is None:
        v = ""
      v = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
      # Use placeholder for empty to avoid consecutive tabs (bash coalesces "\t\t")
      if v == "":
        v = " "
      out.append(v)
    print("\t".join(out))
    n += 1
    if limit is not None and n >= limit:
      break
PY
)
