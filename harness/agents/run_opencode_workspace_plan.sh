#!/usr/bin/env bash
set -euo pipefail

# OpenCode plan-only run inside a pre-built "workspace" directory (see evaluate_opencode_workspace_plan.sh).
# Args:
#   $1  WORKSPACE_ROOT   root that contains repo/ (site) and opencode.json
#   $2  TASK_SPEC        path to task text file (prompt seed)
#   $3  LOG_FILE         agent log
#   $4  PLAN_DEST        path to copy final plan.md (optional; empty skips)

WORKSPACE_ROOT="$1"
TASK_SPEC="$2"
LOG_FILE="$3"
PLAN_DEST="${4:-}"

REPO_DIR="$WORKSPACE_ROOT/repo"
FRAMEWORK="${FRAMEWORK:-unknown}"

mkdir -p "$(dirname "$LOG_FILE")"
LOG_DIR="$(dirname "$LOG_FILE")"

{
  echo "[agent] OpenCode workspace plan (read/write tools only; bash disabled in config)"
  echo "[agent] WORKSPACE_ROOT=$WORKSPACE_ROOT"
} > "$LOG_FILE"

if [[ ! -d "$REPO_DIR" ]]; then
  echo "[agent] ERROR: missing site checkout at $REPO_DIR" >> "$LOG_FILE"
  exit 1
fi

if ! command -v opencode &>/dev/null; then
  echo "[agent] ERROR: opencode CLI not found. Install: curl -fsSL https://opencode.ai/install | bash" >> "$LOG_FILE"
  exit 1
fi

# Model (aligned with template_opencodegpt51codex.sh)
if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  AZURE_DEPLOY="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5.1-codex}"
  OPENCODE_MODEL="${OPENCODE_MODEL:-azure/$AZURE_DEPLOY}"
  if [[ -z "${AZURE_RESOURCE_NAME:-}" && -n "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
    AZURE_RESOURCE_NAME="${AZURE_OPENAI_ENDPOINT#*://}"
    AZURE_RESOURCE_NAME="${AZURE_RESOURCE_NAME%%.*}"
    export AZURE_RESOURCE_NAME
  fi
else
  OPENCODE_MODEL="${OPENCODE_MODEL:-openai/gpt-5.1-codex}"
fi

OPENCODE_REASONING="${OPENCODE_REASONING_EFFORT:-medium}"
export OPENCODE_REASONING_EFFORT="${OPENCODE_REASONING_EFFORT:-$OPENCODE_REASONING}"

# Optional: experimental plan UI behavior (see OpenCode env docs)
if [[ "${OPENCODE_EXPERIMENTAL_PLAN_MODE:-0}" == "1" ]]; then
  export OPENCODE_EXPERIMENTAL_PLAN_MODE=true
fi

PLAN_AGENT="${OPENCODE_PLAN_AGENT:-plan}"
# "opencode -p" in older notes often maps to non-interactive plan; current CLI: `opencode run --agent plan`.
echo "[agent] OPENCODE_MODEL=$OPENCODE_MODEL  PLAN_AGENT=$PLAN_AGENT" >> "$LOG_FILE"

CWV_MOBILE="${CWV_BASELINE_MOBILE:-}"
CWV_DESKTOP="${CWV_BASELINE_DESKTOP:-}"
LCP_MOBILE="${LCP_ENTRIES_MOBILE:-}"
LCP_DESKTOP="${LCP_ENTRIES_DESKTOP:-}"
CLS_SHIFTS_M="${CLS_SHIFTS_MOBILE:-}"
CLS_SHIFTS_D="${CLS_SHIFTS_DESKTOP:-}"
INP_INTERACTIONS_M="${INP_INTERACTIONS_MOBILE:-}"
INP_INTERACTIONS_D="${INP_INTERACTIONS_DESKTOP:-}"
[[ "$CWV_MOBILE" == " " || -z "$CWV_MOBILE" ]] && CWV_MOBILE="null"
[[ "$CWV_DESKTOP" == " " || -z "$CWV_DESKTOP" ]] && CWV_DESKTOP="null"
[[ "$LCP_MOBILE" == " " || -z "$LCP_MOBILE" ]] && LCP_MOBILE="null"
[[ "$LCP_DESKTOP" == " " || -z "$LCP_DESKTOP" ]] && LCP_DESKTOP="null"
[[ "$CLS_SHIFTS_M" == " " || -z "$CLS_SHIFTS_M" ]] && CLS_SHIFTS_M="null"
[[ "$CLS_SHIFTS_D" == " " || -z "$CLS_SHIFTS_D" ]] && CLS_SHIFTS_D="null"
[[ "$INP_INTERACTIONS_M" == " " || -z "$INP_INTERACTIONS_M" ]] && INP_INTERACTIONS_M="null"
[[ "$INP_INTERACTIONS_D" == " " || -z "$INP_INTERACTIONS_D" ]] && INP_INTERACTIONS_D="null"

PLAN_PROMPT="$(mktemp)"
NDJSON="$(mktemp)"
trap 'rm -f "$PLAN_PROMPT" "$NDJSON"' EXIT

{
  cat <<EOF
You are a Core Web Vitals optimization expert analyzing a $FRAMEWORK web application.

### Prompt: LCP, CLS, and INP for mobile and desktop

Your Task:
Analyze the codebase and baseline metrics to create a detailed optimization plan that improves:
- Largest Contentful Paint (LCP): time until main content loads
- Cumulative Layout Shift (CLS): visual stability during page load
- Interaction to Next Paint (INP): responsiveness to user interactions

Initial CWV Scores (baseline):
- Mobile: $CWV_MOBILE
- Desktop: $CWV_DESKTOP

Data Available:
- repo/init_cwv.json: Contains full CWV data (scores + lcp_entries + cls_shifts + inp_interactions for mobile and desktop)
- repo/: Complete source code for the application

Write plan.md with these sections:

   ## Performance Issues Identified
   - List specific CWV metrics that need improvement (with current values)
   - List specific CWV metrics that need improvement and provide exact suggestions

Output Instructions:
- You can read files to get better understanding of the codebase
- WRITE the plan to 'plan.md' in the workspace root (next to repo/), or to '.opencode/plans/' if your agent routes there
- List specific CWV metrics that need improvement and provide exact suggestions
- Use valid Markdown formatting
- Be specific about file paths and code changes
- DO NOT modify any repository files under repo/ (init_cwv.json or source code)
- DO NOT create additional files or output to chat
- DO NOT ask the user questions; proceed autonomously with your best judgment
EOF
  if [[ -f "$TASK_SPEC" ]]; then
    echo ""
    echo "### Task spec (harness)"
    cat "$TASK_SPEC"
  fi
  if [[ -n "${EVAL_SUGGESTION_FILE:-}" && -f "$EVAL_SUGGESTION_FILE" ]]; then
    echo ""
    echo "### Benchmark harness: external suggestion for this run"
    echo "The JSON below is one suggestion from an automated CWV audit (index ${EVAL_SUGGESTION_INDEX:-?})."
    echo '```json'
    cat "$EVAL_SUGGESTION_FILE"
    echo '```'
  fi
} > "$PLAN_PROMPT"

cp "$PLAN_PROMPT" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan_prompt.txt"

chmod -R u+w "$WORKSPACE_ROOT/repo" 2>/dev/null || true
if [[ -d "$WORKSPACE_ROOT/repo/.git.bak" ]]; then
  : # already prepared
elif [[ -d "$WORKSPACE_ROOT/repo/.git" ]]; then
  mv "$WORKSPACE_ROOT/repo/.git" "$WORKSPACE_ROOT/repo/.git.bak"
fi
chmod -R a-w "$WORKSPACE_ROOT/repo"
mkdir -p "$WORKSPACE_ROOT/.opencode/plans"
chmod -R u+w "$WORKSPACE_ROOT/.opencode" 2>/dev/null || true
touch "$WORKSPACE_ROOT/plan.md"
chmod u+w "$WORKSPACE_ROOT/plan.md"

set +e
(
  cd "$WORKSPACE_ROOT" && opencode run \
    --format json \
    --model "$OPENCODE_MODEL" \
    --agent "$PLAN_AGENT" \
    "$(<"$PLAN_PROMPT")"
) 2>>"$LOG_FILE" >"$NDJSON"
RUN_EXIT=$?
set -e

echo "[agent] opencode run exit code: $RUN_EXIT" >> "$LOG_FILE"
if [[ "$RUN_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: opencode run exited with $RUN_EXIT" >> "$LOG_FILE"
fi

# OpenCode can exit 0 while emitting type=error in JSONL (e.g. Azure 401); surface that in the log.
if python3 - "$NDJSON" >>"$LOG_FILE" 2>&1 <<'PYERR'
import json, sys
path = sys.argv[1]
found = False
try:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "error":
                continue
            found = True
            err = ev.get("error") or {}
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            msg = data.get("message") or err.get("message") or str(err)
            meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
            url = meta.get("url", "")
            print(f"[agent] ERROR (OpenCode stream): {msg}")
            if url:
                print(f"[agent] Request URL: {url}")
except OSError as e:
    print(f"[agent] WARN: could not read NDJSON for errors: {e}")
sys.exit(2 if found else 0)
PYERR
then
  :
else
  _stream_err=$?
  if [[ "$_stream_err" -eq 2 ]]; then
    echo "[agent] FAIL: model/API error in stream — fix Azure or OpenAI credentials and endpoint (see harness/.env and opencode auth)." >> "$LOG_FILE"
  fi
fi

# Primary agent output for `plan` is often under .opencode/plans/*.md (see `opencode agent list`), not ./plan.md.
if [[ ! -s "$WORKSPACE_ROOT/plan.md" ]]; then
  _plan_pick=""
  if compgen -G "$WORKSPACE_ROOT/.opencode/plans/"*.md >/dev/null 2>&1; then
    _plan_pick="$(ls -t "$WORKSPACE_ROOT/.opencode/plans/"*.md 2>/dev/null | head -1)"
  fi
  if [[ -n "$_plan_pick" && -s "$_plan_pick" ]]; then
    cp "$_plan_pick" "$WORKSPACE_ROOT/plan.md"
    echo "[agent] Normalized plan from $_plan_pick -> plan.md" >> "$LOG_FILE"
  fi
fi

if [[ ! -s "$WORKSPACE_ROOT/plan.md" ]]; then
  python3 - "$NDJSON" "$WORKSPACE_ROOT/plan.md" <<'PYEOF'
import json, sys, re
ndjson_path, out_path = sys.argv[1], sys.argv[2]
text = ""
try:
    with open(ndjson_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if ev.get("type") == "text":
                    text += ev.get("part", {}).get("text", "")
            except Exception:
                pass
except Exception:
    pass
clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
marker = "## Performance Issues Identified"
idx = clean.find(marker)
body = clean[idx:] if idx != -1 else clean.strip()
if len(body) > 80:
    with open(out_path, "w") as f:
        f.write(body)
PYEOF
fi

if [[ ! -s "$WORKSPACE_ROOT/plan.md" ]]; then
  _base="$(basename "$LOG_FILE" _agent.log)"
  cp "$NDJSON" "$LOG_DIR/${_base}_opencode.ndjson" 2>/dev/null || true
  echo "[agent] WARN: plan still empty; saved NDJSON to $LOG_DIR/${_base}_opencode.ndjson for debugging" >> "$LOG_FILE"
fi

if [[ -n "$PLAN_DEST" ]]; then
  mkdir -p "$(dirname "$PLAN_DEST")"
  if [[ -s "$WORKSPACE_ROOT/plan.md" ]]; then
    cp "$WORKSPACE_ROOT/plan.md" "$PLAN_DEST"
  else
    touch "$PLAN_DEST"
    echo "[agent] WARN: plan.md missing or empty after run" >> "$LOG_FILE"
  fi
fi

echo "[agent] === plan.md (workspace) ===" >> "$LOG_FILE"
cat "$WORKSPACE_ROOT/plan.md" >> "$LOG_FILE" 2>/dev/null || true
echo "[agent] === end plan.md ===" >> "$LOG_FILE"

exit 0
