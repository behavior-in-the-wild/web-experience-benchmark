#!/usr/bin/env bash
set -euo pipefail

# Route all temp files to /dev/shm (overlay /tmp is small; /dev/shm has ~1 TB free)
export TMPDIR="${BENCH_TMPDIR:-/dev/shm}"

# ============================================================
# Common agent template (OpenCode + GPT5 variant)
# ============================================================

REPO_DIR="$1"
TASK_SPEC="$2"
LOG_FILE="$3"
PATCH_FILE="${4:-/dev/null}"

FRAMEWORK="${FRAMEWORK:-unknown}"
DEVICE="${DEVICE:-unknown}"

cd "$REPO_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
LOG_DIR="$(dirname "$LOG_FILE")"

echo "[agent] Two-phase CWV agent (opencode + GPT5)" > "$LOG_FILE"

PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"

# ============================================================
# Phase 1 workspace: repo read-only, plan.md writable only
# ============================================================
PHASE1_DIR="$(mktemp -d)"
trap 'chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR"' EXIT

# Copy repo to phase1 workspace (repo will be made read-only)
cp -r "$REPO_DIR" "$PHASE1_DIR/repo"

# Write init CWV data for the model to read (from evaluate.sh exports)
# evaluate.sh exports CWV_ENV_FILE with base64-encoded values to avoid ARG_MAX limits.
# Optional: EVAL_SUGGESTION_FILE (absolute path to one suggestion object JSON) when running with --suggestions-file
if [[ -n "${CWV_ENV_FILE:-}" && -f "$CWV_ENV_FILE" ]]; then
  while IFS='=' read -r _cwv_key _cwv_b64; do
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

# Ensure PHASE1_DIR is the project root (not repo/): move repo/.git aside so OpenCode
# uses PHASE1_DIR as cwd=project, matching Codex -C and Claude cd behavior.
if [[ -d "$PHASE1_DIR/repo/.git" ]]; then
  mv "$PHASE1_DIR/repo/.git" "$PHASE1_DIR/repo/.git.bak"
fi

# Make repo read-only so model can only write plan.md
chmod -R a-w "$PHASE1_DIR/repo"

# plan.md is the only writable file in the workspace
touch "$PHASE1_DIR/plan.md"

# -------------------------
# Model config (OpenCode - supports 75+ providers via models.dev)
# -------------------------
# Model format: provider/model (run `opencode models` to list available models)
# Examples:
#   openai/gpt-5           - GPT-5 (OpenAI)
#   openai/gpt-5.2         - GPT-5.2
#   302ai/glm-4.5          - GLM-4.5 (Zhipu)
#   302ai/glm-4.6          - GLM-4.6
#   302ai/glm-4.7          - GLM-4.7
#   302ai/kimi-k2-0905-preview  - Kimi K2 (Moonshot)
#   302ai/kimi-k2-thinking      - Kimi K2 Thinking
#   openrouter/moonshotai/kimi-k2       - Kimi K2 via OpenRouter
#   openrouter/moonshotai/kimi-k2-0905  - Kimi K2 0905 via OpenRouter
#   openrouter/z-ai/glm-4.5     - GLM-4.5 via OpenRouter
#   aihubmix/glm-4.7           - GLM-4.7 via AIHubMix
#   aihubmix/Kimi-K2-0905      - Kimi K2 via AIHubMix
#   azure/gpt-5                - Azure OpenAI (deployment name)
if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  AZURE_DEPLOY="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5.1-codex}"
  OPENCODE_MODEL="${OPENCODE_MODEL:-azure/$AZURE_DEPLOY}"
  # OpenCode uses AZURE_API_KEY; always derive from the standard AZURE_OPENAI_API_KEY
  export AZURE_API_KEY="$AZURE_OPENAI_API_KEY"
  # OpenCode requires AZURE_RESOURCE_NAME; derive from AZURE_OPENAI_ENDPOINT if unset
  if [[ -z "${AZURE_RESOURCE_NAME:-}" && -n "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
    # e.g. https://myresource.openai.azure.com -> myresource
    AZURE_RESOURCE_NAME="${AZURE_OPENAI_ENDPOINT#*://}"
    AZURE_RESOURCE_NAME="${AZURE_RESOURCE_NAME%%.*}"
    export AZURE_RESOURCE_NAME
  fi
else
  OPENCODE_MODEL="${OPENCODE_MODEL:-openai/gpt-5.1-codex}"
fi

if ! command -v opencode &>/dev/null; then
  echo "[agent] ERROR: opencode CLI not found. Install: curl -fsSL https://opencode.ai/install | bash" >> "$LOG_FILE"
  exit 1
fi

echo "OPENCODE_MODEL: $OPENCODE_MODEL" >> "$LOG_FILE"

# Auth: run `opencode auth login` to configure providers. Common env vars:
#   openai/*: OPENAI_API_KEY
#   azure/*: AZURE_OPENAI_API_KEY, AZURE_RESOURCE_NAME (or AZURE_OPENAI_ENDPOINT)
#   openrouter/*: OPENROUTER_API_KEY
#   302ai/*, aihubmix/*, etc.: see provider docs at opencode.ai/docs/providers

# OpenCode config: permission + reasoning effort + max output tokens (matches Codex)
# Override via OPENCODE_REASONING_EFFORT (default: medium) or OPENCODE_MAX_TOKENS (default: 50000)
OPENCODE_REASONING="${OPENCODE_REASONING_EFFORT:-medium}"
OPENCODE_MAX="${OPENCODE_MAX_TOKENS:-50000}"
# Deny question tool: in non-interactive mode it blocks indefinitely (opencode run has no TUI to answer)

# OPENCODE_CFG="{\"permission\":{\"question\":\"deny\"},\"small_model\":\"$OPENCODE_MODEL\",\"provider\":{\"azure\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}},\"openai\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}}}}"
OPENCODE_CFG="{\"permission\":{\"question\":\"deny\"},\"small_model\":\"azure/gpt-5\",\"provider\":{\"azure\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}},\"openai\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}}}}"

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

cp "$PLAN_PROMPT" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_prompt.txt"

# -------- OPENCODE RUN (PHASE 1) — matches Codex/Claude: workspace=PHASE1_DIR, repo read-only, plan.md writable --------
# Note: OpenCode may output the plan to stdout instead of editing plan.md; we extract it as fallback.
# Stderr is captured to LOG_FILE so rate-limit / auth errors are visible in agent.log.
PHASE1_NDJSON="$(mktemp)"
PHASE2_NDJSON="$(mktemp)"
# Isolate OpenCode's SQLite database per task to prevent SQLITE_BUSY contention
# when many parallel agents share the default ~/.local/share/opencode/opencode.db
OPENCODE_DATA_DIR="$(mktemp -d)"
trap 'chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR" "$OPENCODE_DATA_DIR"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT" "$PHASE1_NDJSON" "$PHASE2_NDJSON"' EXIT
# Retry up to 3 times; retry condition is based on plan.md content (not just NDJSON size)
# because OpenCode may return a non-empty but unhelpful NDJSON response (e.g. an error
# event or "I cannot access files") that fails plan extraction.
PHASE1_EXIT=1
for _p1_attempt in 1 2 3; do
  if [[ $_p1_attempt -gt 1 ]]; then
    _p1_wait=$(( (_p1_attempt - 1) * 30 ))
    echo "[agent] Phase 1 retry $_p1_attempt after ${_p1_wait}s" >> "$LOG_FILE"
    sleep "$_p1_wait"
  fi
  : > "$PHASE1_NDJSON"
  : > "$PHASE1_DIR/plan.md"
  (cd "$PHASE1_DIR" && XDG_DATA_HOME="$OPENCODE_DATA_DIR" OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
    --format json \
    --model "$OPENCODE_MODEL" \
    --dangerously-skip-permissions \
    "$(<"$PLAN_PROMPT")") > "$PHASE1_NDJSON" 2>> "$LOG_FILE"
  PHASE1_EXIT=$?
  _p1_ndjson_sz=$(wc -c < "$PHASE1_NDJSON"); _p1_plan_sz=$(wc -c < "$PHASE1_DIR/plan.md")
  echo "[agent] Phase 1 attempt $_p1_attempt: NDJSON=${_p1_ndjson_sz}bytes plan=${_p1_plan_sz}bytes exit=$PHASE1_EXIT" >> "$LOG_FILE"
  # Try extracting plan from NDJSON if opencode didn't write plan.md directly
  if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
    python3 - "$PHASE1_NDJSON" "$PHASE1_DIR/plan.md" << 'PYEOF'
import json, sys, re
ndjson_path, out_path = sys.argv[1], sys.argv[2]
text = ''
try:
    with open(ndjson_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if ev.get('type') == 'text':
                    text += ev.get('part', {}).get('text', '')
            except Exception:
                pass
except Exception:
    pass
marker = '## Performance Issues Identified'
idx = text.find(marker)
if idx != -1:
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text[idx:])
    with open(out_path, 'w') as f:
        f.write(clean)
PYEOF
  fi
  # Success: plan.md has content
  [[ -s "$PHASE1_DIR/plan.md" ]] && break
  echo "[agent] Phase 1 attempt $_p1_attempt: plan.md still empty after extraction" >> "$LOG_FILE"
done
# -------------------------------------

# plan.md is the only writable file; repo/ was chmod read-only
PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  touch "$PLAN_COPY"
  exit 0
fi

# Copy plan to REPO_DIR so Phase 2 can read it (avoids passing huge prompt as CLI arg)
cp "$PHASE1_DIR/plan.md" "$REPO_DIR/plan.md"
[[ -s "$REPO_DIR/plan.md" ]] || { echo "[agent] ERROR: plan.md copy failed or empty" >> "$LOG_FILE"; exit 1; }

# Copy plan.md to results folder for post-analysis
cp "$REPO_DIR/plan.md" "$PLAN_COPY"

# Append plan content to log for visibility
echo "[agent] === plan.md content ===" >> "$LOG_FILE"
cat "$REPO_DIR/plan.md" >> "$LOG_FILE"
echo "[agent] === end plan.md ===" >> "$LOG_FILE"

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

EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"

printf "%s" "$EXEC_PROMPT_CONTENT" > "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase2_prompt.txt"

set +e
PHASE2_EXIT=1
for _p2_attempt in 1 2 3; do
  if [[ $_p2_attempt -gt 1 ]]; then
    _p2_wait=$(( (_p2_attempt - 1) * 30 ))
    echo "[agent] Phase 2 retry $_p2_attempt after ${_p2_wait}s (previous attempt produced no output)" >> "$LOG_FILE"
    sleep "$_p2_wait"
  fi
  : > "$PHASE2_NDJSON"
  (cd "$REPO_DIR" && XDG_DATA_HOME="$OPENCODE_DATA_DIR" OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
    --format json \
    --model "$OPENCODE_MODEL" \
    --dangerously-skip-permissions \
    "$EXEC_PROMPT_CONTENT") > "$PHASE2_NDJSON" 2>> "$LOG_FILE"
  PHASE2_EXIT=$?
  [[ -s "$PHASE2_NDJSON" ]] && break
  echo "[agent] Phase 2 attempt $_p2_attempt: OpenCode produced no NDJSON output (exit=$PHASE2_EXIT)" >> "$LOG_FILE"
done
set -e

if [[ "$PHASE2_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: Phase 2 opencode returned non-zero ($PHASE2_EXIT), continuing to capture diff" >> "$LOG_FILE"
fi

# -------------------------------------
# Write usage metrics JSON (cost, tokens, tool_calls) from both phases' NDJSON output
# -------------------------------------
USAGE_FILE="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_usage.json"
python3 - "$PHASE1_NDJSON" "$PHASE2_NDJSON" "$USAGE_FILE" << 'PYEOF'
import json, sys

def parse_ndjson(path):
    cost = 0.0
    tok = {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0, 'cache_write': 0}
    tool_calls = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    t = ev.get('type', '')
                    part = ev.get('part', {})
                    if t == 'step_finish':
                        cost += part.get('cost', 0) or 0
                        t2 = part.get('tokens', {}) or {}
                        tok['input']      += t2.get('input', 0) or 0
                        tok['output']     += t2.get('output', 0) or 0
                        tok['reasoning']  += t2.get('reasoning', 0) or 0
                        cache = t2.get('cache', {}) or {}
                        tok['cache_read']  += cache.get('read', 0) or 0
                        tok['cache_write'] += cache.get('write', 0) or 0
                    elif t in ('tool_use', 'tool-use', 'tool_call', 'tool-call'):
                        tool_calls += 1
                except Exception:
                    pass
    except Exception:
        pass
    return {'cost_usd': round(cost, 6), 'tokens': tok, 'tool_calls': tool_calls}

p1_path, p2_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
p1 = parse_ndjson(p1_path)
p2 = parse_ndjson(p2_path)

def merge_tokens(a, b):
    return {k: a[k] + b[k] for k in a}

total_tok = merge_tokens(p1['tokens'], p2['tokens'])
total_tok['total'] = total_tok['input'] + total_tok['output'] + total_tok['reasoning']

usage = {
    'cost_usd': round(p1['cost_usd'] + p2['cost_usd'], 6),
    'tokens': total_tok,
    'tool_calls': p1['tool_calls'] + p2['tool_calls'],
    'phases': {
        'phase1': p1,
        'phase2': p2,
    },
}

with open(out_path, 'w') as f:
    json.dump(usage, f, indent=2)
print(f"[agent] usage written to {out_path}", file=sys.stderr)
PYEOF

# Remove plan.md from repo before capturing diff (planning artifact, not a code change)
rm -f "$REPO_DIR/plan.md"

cp "$PHASE1_NDJSON" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1.ndjson" 2>/dev/null || true
cp "$PHASE2_NDJSON" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase2.ndjson" 2>/dev/null || true
git ls-files --others --exclude-standard > "$LOG_DIR/untracked_files.txt" 2>/dev/null || true
git add -A
git diff --cached > "$PATCH_FILE"
PATCH_LINES=$(wc -l < "$PATCH_FILE" 2>/dev/null || echo 0)
echo "[agent] Patch: $PATCH_LINES lines" >> "$LOG_FILE"
git reset --hard HEAD
git clean -fd
rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"

echo "[agent] Done" >> "$LOG_FILE"
