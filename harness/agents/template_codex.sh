#!/usr/bin/env bash
set -euo pipefail

# Route all temp files to /dev/shm (overlay /tmp is small; /dev/shm has ~1 TB free)
export TMPDIR="${BENCH_TMPDIR:-/dev/shm}"

# ============================================================
# Common agent template (Codex variant)
# ============================================================

REPO_DIR="$1"
TASK_SPEC="$2"
LOG_FILE="$3"
PATCH_FILE="${4:-/dev/null}"

FRAMEWORK="${FRAMEWORK:-unknown}"
DEVICE="${DEVICE:-unknown}"

cd "$REPO_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

echo "[agent] Two-phase CWV agent (codex)" > "$LOG_FILE"

PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"
PHASE1_NDJSON="$(mktemp)"
PHASE2_NDJSON="$(mktemp)"
PHASE1_LAST_MESSAGE="$(mktemp)"

# ============================================================
# Phase 1 workspace: repo read-only, plan.md writable only
# ============================================================
PHASE1_DIR="$(mktemp -d)"

_write_usage() {
  local log_dir usage_file
  log_dir="$(dirname "$LOG_FILE")"
  usage_file="$log_dir/$(basename "$LOG_FILE" _agent.log)_usage.json"
  # Only write if not already written (avoid overwriting final write)
  [[ -f "$usage_file" ]] && return
  python3 - "$PHASE1_NDJSON" "$PHASE2_NDJSON" "$usage_file" 2>>"$LOG_FILE" << 'PYEOF'
import json, sys
TOOL_ITEM_TYPES = {'command_execution', 'file_change', 'mcp_tool_call', 'web_search'}
def parse_ndjson(path):
    tok = {'input': 0, 'cached_input': 0, 'output': 0}
    tool_calls = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    ev = json.loads(line)
                    t = ev.get('type', '')
                    if t == 'turn.completed':
                        u = ev.get('usage', {}) or {}
                        tok['input']        += u.get('input_tokens', 0) or 0
                        tok['cached_input'] += u.get('cached_input_tokens', 0) or 0
                        tok['output']       += u.get('output_tokens', 0) or 0
                    elif t == 'item.completed':
                        if ev.get('item', {}).get('type', '') in TOOL_ITEM_TYPES:
                            tool_calls += 1
                except Exception: pass
    except Exception: pass
    return {'tokens': tok, 'tool_calls': tool_calls}
p1 = parse_ndjson(sys.argv[1])
p2 = parse_ndjson(sys.argv[2])
total = {k: p1['tokens'][k] + p2['tokens'][k] for k in p1['tokens']}
total['total'] = total['input'] + total['output']
with open(sys.argv[3], 'w') as f:
    json.dump({'cost_usd': None, 'tokens': total,
               'tool_calls': p1['tool_calls'] + p2['tool_calls'],
               'phases': {'phase1': p1, 'phase2': p2}}, f, indent=2)
PYEOF
}

trap '_write_usage; chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR"; rm -f "$PHASE1_NDJSON" "$PHASE2_NDJSON" "$PHASE1_LAST_MESSAGE"' EXIT

# Copy repo to phase1 workspace (repo will be made read-only)
cp -r "$REPO_DIR" "$PHASE1_DIR/repo"

# Write init CWV data for the model to read (from evaluate.sh exports)
# evaluate.sh exports CWV_ENV_FILE with base64-encoded values to avoid ARG_MAX limits.
if [[ -n "${CWV_ENV_FILE:-}" && -f "$CWV_ENV_FILE" ]]; then
  while IFS='=' read -r _cwv_key _cwv_b64; do
    [[ -n "$_cwv_key" ]] || continue
    printf -v "$_cwv_key" '%s' "$(printf '%s' "$_cwv_b64" | base64 -d 2>/dev/null || true)"
  done < "$CWV_ENV_FILE"
fi
CWV_MOBILE="${CWV_BASELINE_MOBILE:-}"
CWV_DESKTOP="${CWV_BASELINE_DESKTOP:-}"
LCP_MOBILE="${LCP_ENTRIES_MOBILE:-}"
LCP_DESKTOP="${LCP_ENTRIES_DESKTOP:-}"
CLS_SHIFTS_M="${CLS_SHIFTS_MOBILE:-}"
CLS_SHIFTS_D="${CLS_SHIFTS_DESKTOP:-}"
INP_INTERACTIONS_M="${INP_INTERACTIONS_MOBILE:-}"
INP_INTERACTIONS_D="${INP_INTERACTIONS_DESKTOP:-}"
# Use null for empty (evaluate.sh uses " " as placeholder for empty CSV cells)
[[ "$CWV_MOBILE" == " " || -z "$CWV_MOBILE" ]] && CWV_MOBILE="null"
[[ "$CWV_DESKTOP" == " " || -z "$CWV_DESKTOP" ]] && CWV_DESKTOP="null"
[[ "$LCP_MOBILE" == " " || -z "$LCP_MOBILE" ]] && LCP_MOBILE="null"
[[ "$LCP_DESKTOP" == " " || -z "$LCP_DESKTOP" ]] && LCP_DESKTOP="null"
[[ "$CLS_SHIFTS_M" == " " || -z "$CLS_SHIFTS_M" ]] && CLS_SHIFTS_M="null"
[[ "$CLS_SHIFTS_D" == " " || -z "$CLS_SHIFTS_D" ]] && CLS_SHIFTS_D="null"
[[ "$INP_INTERACTIONS_M" == " " || -z "$INP_INTERACTIONS_M" ]] && INP_INTERACTIONS_M="null"
[[ "$INP_INTERACTIONS_D" == " " || -z "$INP_INTERACTIONS_D" ]] && INP_INTERACTIONS_D="null"
printf '{"mobile":%s,"desktop":%s,"lcp_entries_mobile":%s,"lcp_entries_desktop":%s,"cls_shifts_mobile":%s,"cls_shifts_desktop":%s,"inp_interactions_mobile":%s,"inp_interactions_desktop":%s}\n' \
  "$CWV_MOBILE" "$CWV_DESKTOP" "$LCP_MOBILE" "$LCP_DESKTOP" \
  "$CLS_SHIFTS_M" "$CLS_SHIFTS_D" "$INP_INTERACTIONS_M" "$INP_INTERACTIONS_D" > "$PHASE1_DIR/repo/init_cwv.json"

# Make repo read-only so model can only write plan.md
chmod -R a-w "$PHASE1_DIR/repo"

# plan.md is the only writable file in the workspace
touch "$PHASE1_DIR/plan.md"

# -------------------------
# Model config
# -------------------------
if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  AZURE_BASE="${AZURE_OPENAI_ENDPOINT%/}/openai/v1"
  AZURE_MODEL="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5}"
  CODEX_EXTRA=(
    -c "model_provider=azure"
    -c "model=$AZURE_MODEL"
    -c "model_providers.azure.name=Azure"
    -c "model_providers.azure.base_url=$AZURE_BASE"
    -c "model_providers.azure.env_key=AZURE_OPENAI_API_KEY"
    -c "model_providers.azure.wire_api=responses"
    -c "reasoning.effort=medium"
    -c "max_output_tokens=50000"
  )
else
  echo "ERROR: Missing AZURE_OPENAI_API_KEY" >> "$LOG_FILE"
  exit 1
fi

# ============================================================
# Phase 1 — Planning
# ============================================================
cat <<EOF > "$PLAN_PROMPT"
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

IMPORTANT: You MUST always write plan.md regardless of whether metrics look good or bad.
Even if current scores are "Good", there are always further optimizations possible.

Write plan.md with EXACTLY these sections (required even if scores appear healthy):

## Performance Issues Identified
- List current metric values and ratings for LCP, CLS, INP (mobile and desktop)
- Note any metrics that are borderline or could regress under load

## Optimization Plan
- Provide specific, concrete code changes to further improve or protect each CWV metric
- Be specific about file paths and exact code changes

Output Instructions:
- You can read files to get better understanding of the codebase
- You MUST WRITE the plan to 'plan.md' in the current directory — this is required
- Use valid Markdown formatting
- Be specific about file paths and code changes
- DO NOT modify any repository files (init_cwv.json or source code)
- DO NOT create additional files or output to chat
- DO NOT ask the user questions; proceed autonomously with your best judgment
EOF
if [[ -n "${EVAL_SUGGESTION_FILE:-}" && -f "$EVAL_SUGGESTION_FILE" ]]; then
  {
    echo ""
    echo "### Benchmark harness: external suggestion for this run"
    echo "The JSON below is one suggestion from an automated CWV audit (index ${EVAL_SUGGESTION_INDEX:-?})."
    echo "Treat it as primary guidance: align your plan with title, description, solution, codeChanges,"
    echo "and validationCriteria. Adapt if the repository differs from the described paths."
    echo ""
    echo '```json'
    cat "$EVAL_SUGGESTION_FILE"
    echo '```'
  } >> "$PLAN_PROMPT"
fi

# -------- CODEX CALL (PHASE 1) — two workspaces: repo read-only, plan.md writable --------
echo "[agent] DEBUG: Starting Phase 1 (planning)..." >> "$LOG_FILE"
PHASE1_START=$(date +%s)
set +e
codex exec \
  -C "$PHASE1_DIR" \
  "${CODEX_EXTRA[@]}" \
  --disable image_generation \
  --skip-git-repo-check \
  --sandbox workspace-write \
  --json \
  -o "$PHASE1_LAST_MESSAGE" \
  "$(<"$PLAN_PROMPT")" \
  </dev/null \
  2>> "$LOG_FILE" > "$PHASE1_NDJSON"
PHASE1_EXIT=$?
set -e
PHASE1_END=$(date +%s)
echo "[agent] DEBUG: Phase 1 exit code=$PHASE1_EXIT, duration=$((PHASE1_END - PHASE1_START))s" >> "$LOG_FILE"
if [[ "$PHASE1_EXIT" -ne 0 ]]; then
  echo "[agent] ERROR: Phase 1 codex returned non-zero ($PHASE1_EXIT)" >> "$LOG_FILE"
  exit 0
fi
# -------------------------------------

if [[ ! -s "$PHASE1_DIR/plan.md" && -s "$PHASE1_LAST_MESSAGE" ]]; then
  cp "$PHASE1_LAST_MESSAGE" "$PHASE1_DIR/plan.md"
  echo "[agent] DEBUG: Recovered plan.md from codex last message output" >> "$LOG_FILE"
fi

# plan.md is the only writable file; repo/ was chmod read-only
if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  exit 0
fi

PLAN_SIZE=$(wc -c < "$PHASE1_DIR/plan.md")
echo "[agent] DEBUG: Phase 1 complete, plan.md size=$PLAN_SIZE bytes" >> "$LOG_FILE"

# Copy plan to REPO_DIR so Phase 2 can read it (avoids passing huge prompt as CLI arg)
cp "$PHASE1_DIR/plan.md" "$REPO_DIR/plan.md"
echo "[agent] DEBUG: Copied plan.md to REPO_DIR=$REPO_DIR" >> "$LOG_FILE"
[[ -s "$REPO_DIR/plan.md" ]] || { echo "[agent] ERROR: plan.md copy failed or empty" >> "$LOG_FILE"; exit 1; }

# ============================================================
# Phase 2 — Execution (plan in repo, prompt as arg)
# ============================================================
{
  printf 'You are implementing Core Web Vitals optimizations for a %s website.\n\n' "$FRAMEWORK"
  printf 'Your Task:\nExecute the code modifications specified in plan.md (in this directory) to optimize CWV metrics (LCP, CLS, INP) for both mobile and desktop.\n\n'
  printf '%s-Specific Considerations:\n' "$FRAMEWORK"
  printf '  - Work within the existing %s architecture and patterns\n' "$FRAMEWORK"
  printf '  - Preserve all existing functionality and visible content\n\n'
  printf 'Implementation Constraints:\n'
  printf '  - Follow the plan and implement the changes\n'
  printf '  - Do NOT edit init_cwv.json or configuration files\n'
  printf '  - Do NOT remove pages or alter visible content/layout\n'
  printf '  - Apply optimizations that work for both mobile and desktop viewports\n\n'
  printf 'Focus on executing the concrete file modifications from plan.md. Skip any analysis or documentation steps.\n'
  printf 'Do not ask the user questions; proceed autonomously.\n'
} > "$EXEC_PROMPT"
if [[ -n "${EVAL_SUGGESTION_FILE:-}" && -f "$EVAL_SUGGESTION_FILE" ]]; then
  {
    echo ""
    echo "### Benchmark harness: external suggestion (same as planning phase)"
    echo '```json'
    cat "$EVAL_SUGGESTION_FILE"
    echo '```'
    echo ""
    echo "Implement changes that satisfy this suggestion together with plan.md; prefer the suggestion when they overlap."
  } >> "$EXEC_PROMPT"
fi

EXEC_PROMPT_SIZE=$(wc -c < "$EXEC_PROMPT")
EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"
echo "[agent] DEBUG: EXEC_PROMPT size=$EXEC_PROMPT_SIZE bytes" >> "$LOG_FILE"
echo "[agent] DEBUG: REPO_DIR=$REPO_DIR plan.md exists=$( [[ -f "$REPO_DIR/plan.md" ]] && echo yes || echo no )" >> "$LOG_FILE"
echo "[agent] DEBUG: Starting Phase 2 (execution)..." >> "$LOG_FILE"

# -------- CODEX CALL (PHASE 2) — pass prompt as arg (avoids stdin issues) --------
# EXEC_PROMPT is small (~500B), so we pass it directly; more reliable than stdin
set +e
PHASE2_START=$(date +%s)
codex exec \
  -C "$REPO_DIR" \
  "${CODEX_EXTRA[@]}" \
  --disable image_generation \
  --skip-git-repo-check \
  --sandbox workspace-write \
  --json \
  "$EXEC_PROMPT_CONTENT" \
  </dev/null \
  2>> "$LOG_FILE" > "$PHASE2_NDJSON"
PHASE2_EXIT=$?
set -e

PHASE2_END=$(date +%s)
echo "[agent] DEBUG: Phase 2 exit code=$PHASE2_EXIT, duration=$((PHASE2_END - PHASE2_START))s" >> "$LOG_FILE"

if [[ "$PHASE2_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: Phase 2 codex returned non-zero ($PHASE2_EXIT), continuing to capture diff" >> "$LOG_FILE"
fi

# Remove plan.md before capturing diff (planning artifact, not a code change)
rm -f "$REPO_DIR/plan.md"

cp "$PHASE1_NDJSON" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1.ndjson" 2>/dev/null || true
cp "$PHASE2_NDJSON" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase2.ndjson" 2>/dev/null || true
git ls-files --others --exclude-standard > "$LOG_DIR/untracked_files.txt" 2>/dev/null || true
git add -A
git diff --cached > "$PATCH_FILE"
PATCH_LINES=$(wc -l < "$PATCH_FILE" 2>/dev/null || echo 0)
echo "[agent] DEBUG: git diff --cached captured, patch lines=$PATCH_LINES" >> "$LOG_FILE"
git reset --hard HEAD
git clean -fd
rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"

echo "[agent] Done" >> "$LOG_FILE"
