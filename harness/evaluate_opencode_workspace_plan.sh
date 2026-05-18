#!/usr/bin/env bash
set -euo pipefail

# Plan-only harness: for each CSV row, clone a public OpenCode workspace template (see
# OPENCODE_WORKSPACE_GIT_URL), clone the benchmark site into workspace/repo/, copy
# harness/opencode_workspace/opencode.workspace.json (read/write tools; bash disabled), run
#   opencode run --agent plan
# capture plan.md, then rm -rf the whole run directory.
#
# This is a variation of evaluate.sh without HTTP hosting, bore, PSI, CWV, or visual measurement.
#
# Environment:
#   CSV                         Input CSV (default: SAMPLE/input.csv)
#   OPENCODE_WORKSPACE_GIT_URL  Git URL to clone as workspace shell (see default below)
#   OPENCODE_PLAN_AGENT         Agent name for planning (default: plan)
#   OPENCODE_EXPERIMENTAL_PLAN_MODE  Set to 1 to export OPENCODE_EXPERIMENTAL_PLAN_MODE for OpenCode
#
# Optional: same auth/model env vars as harness/opencode.json and template_opencodegpt51codex.sh

usage() {
  cat <<'EOF'
Usage: evaluate_opencode_workspace_plan.sh [options]

Options:
  --limit N       Process only the first N CSV rows
  --parallel N    Max concurrent jobs (default: 1)
  --help, -h      Show this message

Environment:
  CSV                           Input CSV path (default: SAMPLE/input.csv)
  OPENCODE_WORKSPACE_GIT_URL    Workspace template git URL
  OPENCODE_PLAN_AGENT           Passed through to OpenCode (default: plan)
EOF
}

LIMIT=""
PARALLEL=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --limit N"; exit 1; }
      LIMIT="$1"
      shift
      ;;
    --parallel)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N"; exit 1; }
      PARALLEL="$1"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TS="$(date +%Y%m%d_%H%M%S)"
CSV="${CSV:-$SCRIPT_DIR/SAMPLE/input.csv}"
TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv_debug.txt"

TMP_ROOT="$SCRIPT_DIR/out/${RUN_TS}_workspace_plan/run"
RESULTS_DIR="$SCRIPT_DIR/out/${RUN_TS}_workspace_plan/results"

# Default: public GitHub workspace with .opencode layout (override with OPENCODE_WORKSPACE_GIT_URL).
_default_ws_repo="$(printf '%s' 'RXhwZXJpR2VuV29ya3NwYWNl' | base64 -d)"
WORKSPACE_GIT_URL="${OPENCODE_WORKSPACE_GIT_URL:-https://github.com/someshsingh22/${_default_ws_repo}.git}"
WORKSPACE_JSON="$SCRIPT_DIR/opencode_workspace/opencode.workspace.json"
AGENT_RUNNER="$SCRIPT_DIR/agents/run_opencode_workspace_plan.sh"

[[ -f "$CSV" ]]       || { echo "Missing CSV: $CSV"; exit 1; }
[[ -f "$TASK_SPEC" ]] || { echo "Missing task spec: $TASK_SPEC"; exit 1; }
[[ -f "$WORKSPACE_JSON" ]] || { echo "Missing $WORKSPACE_JSON"; exit 1; }
[[ -f "$AGENT_RUNNER" ]]   || { echo "Missing $AGENT_RUNNER"; exit 1; }

if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/.env"
  set +a
fi

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"

echo "[run] CSV:              $CSV"
echo "[run] Results:        $RESULTS_DIR"
echo "[run] Workspace clone: $WORKSPACE_GIT_URL"
echo "[run] Parallel:         $PARALLEL"
[[ -n "$LIMIT" ]] && echo "[run] LIMIT=$LIMIT"

# Agent chmods repo/ read-only; restore write bits so rm -rf can remove the tree.
teardown_run_dir() {
  local d="${1:-}"
  [[ -n "$d" && -d "$d" ]] || return 0
  chmod -R u+w "$d" 2>/dev/null || true
  rm -rf "$d"
}

run_job() {
  local ID="$1"
  local REPO_ID="$2"
  local FRAMEWORK="$3"
  local COMMIT_ID="$4"
  local CWV_MOBILE="$5"
  local CWV_DESKTOP="$6"
  local LCP_ENTRIES_DESKTOP="$7"
  local LCP_ENTRIES_MOBILE="$8"
  local CLS_SHIFTS_MOBILE="$9"
  local CLS_SHIFTS_DESKTOP="${10}"
  local INP_INTERACTIONS_MOBILE="${11}"
  local INP_INTERACTIONS_DESKTOP="${12}"
  local SLOT="${13}"

  local JOB_LABEL RUN_DIR WS_ROOT SITE_DIR AGENT_LOG PLAN_OUT
  JOB_LABEL="${ID}_workspace_plan"
  RUN_DIR="$TMP_ROOT/${JOB_LABEL}"
  WS_ROOT="$RUN_DIR/ws"
  SITE_DIR="$WS_ROOT/repo"

  echo "======================================"
  echo "ID=$ID Repo=$REPO_ID Slot=$SLOT"
  echo "======================================"

  mkdir -p "$RUN_DIR"

  echo "[run] Cloning workspace template ..."
  if ! git clone --depth 1 "$WORKSPACE_GIT_URL" "$WS_ROOT" >/dev/null 2>&1; then
    echo "ERROR: workspace git clone failed (URL=$WORKSPACE_GIT_URL)"
    teardown_run_dir "$RUN_DIR"
    return 1
  fi

  rm -rf "$WS_ROOT/.opencode/node_modules" 2>/dev/null || true

  cp "$WORKSPACE_JSON" "$WS_ROOT/opencode.json"

  echo "[run] Cloning benchmark site $REPO_ID ..."
  if ! git clone "https://github.com/${REPO_ID}.git" "$SITE_DIR" >/dev/null 2>&1; then
    echo "ERROR: site git clone failed (ID=$ID Repo=$REPO_ID)"
    teardown_run_dir "$RUN_DIR"
    return 1
  fi

  local COMMIT_ID_CLEAN="${COMMIT_ID:-}"
  [[ "$COMMIT_ID_CLEAN" == " " ]] && COMMIT_ID_CLEAN=""
  if [[ -n "$COMMIT_ID_CLEAN" && "$COMMIT_ID_CLEAN" != "null" ]]; then
    if ! git -C "$SITE_DIR" checkout "$COMMIT_ID_CLEAN" >/dev/null 2>&1; then
      echo "ERROR: git checkout $COMMIT_ID_CLEAN failed (ID=$ID)"
      teardown_run_dir "$RUN_DIR"
      return 1
    fi
  fi

  git -C "$SITE_DIR" add -A >/dev/null 2>&1 || true
  git -C "$SITE_DIR" commit -qm "baseline" >/dev/null 2>&1 || true

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

  printf '{"mobile":%s,"desktop":%s,"lcp_entries_mobile":%s,"lcp_entries_desktop":%s,"cls_shifts_mobile":%s,"cls_shifts_desktop":%s,"inp_interactions_mobile":%s,"inp_interactions_desktop":%s}\n' \
    "$(op_ws_json_cell "$CWV_MOBILE")" \
    "$(op_ws_json_cell "$CWV_DESKTOP")" \
    "$(op_ws_json_cell "$LCP_ENTRIES_MOBILE")" \
    "$(op_ws_json_cell "$LCP_ENTRIES_DESKTOP")" \
    "$(op_ws_json_cell "$CLS_SHIFTS_MOBILE")" \
    "$(op_ws_json_cell "$CLS_SHIFTS_DESKTOP")" \
    "$(op_ws_json_cell "$INP_INTERACTIONS_MOBILE")" \
    "$(op_ws_json_cell "$INP_INTERACTIONS_DESKTOP")" \
    > "$SITE_DIR/init_cwv.json"

  AGENT_LOG="$RESULTS_DIR/${JOB_LABEL}_agent.log"
  PLAN_OUT="$RESULTS_DIR/${JOB_LABEL}_plan.md"

  bash "$AGENT_RUNNER" "$WS_ROOT" "$TASK_SPEC" "$AGENT_LOG" "$PLAN_OUT" </dev/null \
    || echo "[agent] runner returned non-zero (continuing)"

  teardown_run_dir "$RUN_DIR"
  echo "✓ Done: ID=$ID (workspace teardown)"
}

# CSV placeholder " " → JSON null for init_cwv.json
op_ws_json_cell() {
  local v="${1:-}"
  [[ "$v" == " " || -z "$v" ]] && echo "null" || printf '%s' "$v"
}

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
      for s in $(seq 0 $((PARALLEL - 1))); do
        used=0
        if [[ $count -gt 0 ]]; then
          for p in "${!JOB_SLOT[@]}"; do
            [[ "${JOB_SLOT[$p]}" == "$s" ]] && used=1 && break
          done
        fi
        if [[ $used -eq 0 ]]; then
          _SLOT="$s"
          return 0
        fi
      done
    fi
    sleep 0.5
  done
}

export OPENCODE_REASONING_EFFORT="${OPENCODE_REASONING_EFFORT:-medium}"

while IFS=$'\t' read -r \
  ID REPO_ID FRAMEWORK COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH \
  CWV_MOBILE CWV_DESKTOP LCP_ENTRIES_DESKTOP LCP_ENTRIES_MOBILE \
  CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP
do
  acquire_slot
  slot=$_SLOT
  (
    run_job \
      "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" \
      "$CWV_MOBILE" "$CWV_DESKTOP" "$LCP_ENTRIES_DESKTOP" "$LCP_ENTRIES_MOBILE" \
      "$CLS_SHIFTS_MOBILE" "$CLS_SHIFTS_DESKTOP" "$INP_INTERACTIONS_MOBILE" "$INP_INTERACTIONS_DESKTOP" \
      "$slot"
  ) &
  JOB_SLOT[$!]=$slot
done < <(python3 - <<'PY' "$CSV" "$LIMIT"
import csv, sys

csv.field_size_limit(sys.maxsize)
csv_path, limit_s = sys.argv[1], sys.argv[2]
limit = int(limit_s) if limit_s else None

cols = [
  "ID","REPO_ID","FRAMEWORK","COMMIT_ID","ZIP_REPO_PATH","HOST_FILE_PATH",
  "CWV_MOBILE","CWV_DESKTOP","LCP_ENTRIES_DESKTOP","LCP_ENTRIES_MOBILE",
  "CLS_SHIFTS_MOBILE","CLS_SHIFTS_DESKTOP","INP_INTERACTIONS_MOBILE","INP_INTERACTIONS_DESKTOP",
]

def row_tuple(row):
  out = []
  for c in cols:
    v = row.get(c, "")
    if v is None:
      v = ""
    v = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    if v == "":
      v = " "
    out.append(v)
  return out

n = 0
with open(csv_path, newline="", encoding="utf-8") as f:
  r = csv.DictReader(f)
  for row in r:
    print("\t".join(row_tuple(row)))
    n += 1
    if limit is not None and n >= limit:
      break
PY
)

wait
echo "[run] All jobs complete."
