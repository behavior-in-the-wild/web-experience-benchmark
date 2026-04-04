#!/usr/bin/env bash
# =============================================================================
# run_agents.sh — Stage 1: Run coding agents on benchmark repos
# =============================================================================
#
# Iterates the input CSV, unzips each repo snapshot, runs the selected agent(s),
# and saves the resulting patches. Does NOT host, measure CWV, or visual-validate.
#
# Usage:
#   ./harness/scripts/run_agents.sh [OPTIONS]
#
# Options:
#   --agents AGENTS         Comma-separated agent templates (default: agents/template_cwvoptimizer.sh)
#   --csv PATH              Input CSV (default: SAMPLE/input.csv)
#   --auto-snapshot         Clone+zip repos if snapshots are missing
#   --limit N               Only process the first N repos
#   --run-ts TIMESTAMP      Shared run timestamp (auto-generated if omitted)
#   --help                  Show this message
#
# Output:
#   harness/out/<run_ts>/patches/{ID}_{AGENT}.patch
#   harness/out/<run_ts>/logs/{ID}_{AGENT}_agent.log
# =============================================================================
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SAMPLE_DIR="$HARNESS_DIR/SAMPLE"

AUTO_SNAPSHOT=0
LIMIT=""
AGENTS_CSV=""
CSV_PATH=""
RUN_TS=""

usage() {
  sed -n '/^# Usage:/,/^# ====/p' "$0" | grep '^#' | sed 's/^# \?//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agents)        shift; AGENTS_CSV="$1" ;;
    --csv)           shift; CSV_PATH="$1" ;;
    --auto-snapshot) AUTO_SNAPSHOT=1 ;;
    --limit)         shift; LIMIT="$1" ;;
    --run-ts)        shift; RUN_TS="$1" ;;
    --help|-h)       usage ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# ── Defaults ──────────────────────────────────────────────────────────────────
[[ -z "$RUN_TS" ]]  && RUN_TS="$(date +%Y%m%d_%H%M%S)"
[[ -z "$CSV_PATH" ]] && CSV_PATH="$SAMPLE_DIR/input.csv"

TASK_SPEC="$HARNESS_DIR/tasks/optimize_cwv_debug.txt"
TMP_ROOT="$HARNESS_DIR/out/${RUN_TS}/run"
PATCHES_DIR="$HARNESS_DIR/out/${RUN_TS}/patches"
LOGS_DIR="$HARNESS_DIR/out/${RUN_TS}/logs"

# Build agents array
IFS=',' read -ra AGENTS <<< "${AGENTS_CSV:-agents/template_cwvoptimizer.sh}"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [[ -f "$HARNESS_DIR/.env" ]]; then
  set -a; source "$HARNESS_DIR/.env"; set +a
fi

# ── Sanity checks ─────────────────────────────────────────────────────────────
[[ -f "$CSV_PATH" ]]  || { echo "Missing CSV: $CSV_PATH"; exit 1; }
[[ -f "$TASK_SPEC" ]] || { echo "Missing task spec: $TASK_SPEC"; exit 1; }

mkdir -p "$TMP_ROOT" "$PATCHES_DIR" "$LOGS_DIR"

echo "═══════════════════════════════════════════"
echo " Stage 1: Run Agents — $RUN_TS"
echo "═══════════════════════════════════════════"
echo " CSV:    $CSV_PATH"
echo " Agents: ${AGENTS[*]}"
echo " Output: $HARNESS_DIR/out/$RUN_TS/"
[[ -n "$LIMIT" ]] && echo " Limit:  $LIMIT"
echo "═══════════════════════════════════════════"

# ── Snapshot helper ───────────────────────────────────────────────────────────
clone_and_zip_repo() {
  local github_repo="$1" commit_id="$2" zip_path="$3" work_dir="$4"
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

# ── Unzip + normalize helper ─────────────────────────────────────────────────
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
while IFS=$'\t' read -r \
  ID REPO_ID FRAMEWORK COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH \
  CWV_MOBILE CWV_DESKTOP LCP_ENTRIES_DESKTOP LCP_ENTRIES_MOBILE \
  CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP
do
  for AGENT in "${AGENTS[@]}"; do
    AGENT_NAME="$(basename "$AGENT" .sh)"

    echo ""
    echo "======================================"
    echo "ID=$ID  Repo=$REPO_ID  Agent=$AGENT_NAME"
    echo "======================================"

    RUN_DIR="$TMP_ROOT/${ID}_${AGENT_NAME}"
    REPO_DIR="$RUN_DIR/repo"

    pkill -f "jekyll serve" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    mkdir -p "$RUN_DIR" "$REPO_DIR"

    # ── 1) Ensure snapshot ──
    if [[ -z "$ZIP_REPO_PATH" || "$ZIP_REPO_PATH" == " " ]]; then
      ZIP_REPO_PATH="REPO_SNAPSHOTS/$(echo "$REPO_ID" | tr '/' '_').zip"
    fi
    ZIP_ABS_PATH="$SAMPLE_DIR/$ZIP_REPO_PATH"

    if [[ ! -f "$ZIP_ABS_PATH" ]]; then
      if [[ "$AUTO_SNAPSHOT" -eq 1 ]]; then
        echo "[snapshot] Missing ZIP, auto-snapshot enabled"
        clone_and_zip_repo "$REPO_ID" "$COMMIT_ID" "$ZIP_ABS_PATH" "$RUN_DIR/_snapshot_tmp" || {
          echo "[snapshot] Failed; skipping repo"
          continue
        }
      else
        echo "ERROR: Missing snapshot $ZIP_REPO_PATH (use --auto-snapshot)"
        continue
      fi
    fi

    # ── 2) Unzip + normalize ──
    unzip_and_normalize "$ZIP_ABS_PATH" "$RUN_DIR" "$REPO_DIR"

    # ── 3) Git baseline ──
    if [[ ! -d "$REPO_DIR/.git" ]]; then
      git -C "$REPO_DIR" init -q
      git -C "$REPO_DIR" add -A
      git -C "$REPO_DIR" commit -qm "baseline" || true
    fi

    # ── 4) Export context ──
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

    # ── 5) Run agent ──
    AGENT_LOG="$LOGS_DIR/${ID}_${AGENT_NAME}_agent.log"
    PATCH_FILE="$PATCHES_DIR/${ID}_${AGENT_NAME}.patch"

    bash "$HARNESS_DIR/$AGENT" \
      "$REPO_DIR" \
      "$TASK_SPEC" \
      "$AGENT_LOG" \
      "$PATCH_FILE" \
      </dev/null \
      || echo "[agent] Agent failed (continuing)"

    # ── 6) Normalize patch ──
    if [[ -d "$REPO_DIR/.git" ]]; then
      (
        set +e
        cd "$REPO_DIR" || exit 0
        if [[ ! -s "$PATCH_FILE" ]]; then
          git add -A >/dev/null 2>&1
          git diff --cached > "$PATCH_FILE" 2>/dev/null || true
        fi
        git reset --hard HEAD >/dev/null 2>&1 || true
        git clean -fd >/dev/null 2>&1 || true
        [[ -s "$PATCH_FILE" ]] && git apply "$PATCH_FILE" >/dev/null 2>&1 || true
      )
    fi

    rm -rf "$RUN_DIR"
    TOTAL=$((TOTAL + 1))
    echo "✓ Patch saved: ${ID}_${AGENT_NAME}.patch"
  done
done < <(python3 - <<'PY' "$CSV_PATH" "$LIMIT"
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

echo ""
echo "═══════════════════════════════════════════"
echo " Stage 1 complete: $TOTAL patch(es) saved"
echo " Patches: $PATCHES_DIR"
echo " Logs:    $LOGS_DIR"
echo "═══════════════════════════════════════════"
