#!/usr/bin/env bash
set -euo pipefail

# Route all temp files to /dev/shm (overlay /tmp is small; /dev/shm has ~1 TB free)
export TMPDIR="${BENCH_TMPDIR:-/dev/shm}"

# ============================================================
# Claude Opus 4.6 agent for harness
# Uses OpenCode with anthropic/ provider + Azure Foundry key
# (same auth pattern as template_opencodeopus.sh)
# Two-phase: Plan (read-only, write plan.md) → Execute
#
# Receives from evaluate.sh:
#   CWV_FIELD_MOBILE, CWV_FIELD_DESKTOP       — real-user CrUX data
#   CWV_SYNTHETIC_MOBILE, CWV_SYNTHETIC_DESKTOP — pre-agent Lighthouse on local mirror
#   LCP_ENTRIES_MOBILE, LCP_ENTRIES_DESKTOP
#   PAGE_URL, DOMAIN
# ============================================================

REPO_DIR="$1"
TASK_SPEC="$2"
LOG_FILE="$3"
PATCH_FILE="${4:-/dev/null}"

PAGE_URL="${PAGE_URL:-unknown}"
DOMAIN="${DOMAIN:-unknown}"

# Dual CWV context (set by evaluate.sh)
CWV_FIELD_MOBILE="${CWV_FIELD_MOBILE:-null}"
CWV_FIELD_DESKTOP="${CWV_FIELD_DESKTOP:-null}"
CWV_SYNTHETIC_MOBILE="${CWV_SYNTHETIC_MOBILE:-null}"
CWV_SYNTHETIC_DESKTOP="${CWV_SYNTHETIC_DESKTOP:-null}"
LCP_ENTRIES_MOBILE="${LCP_ENTRIES_MOBILE:-null}"
LCP_ENTRIES_DESKTOP="${LCP_ENTRIES_DESKTOP:-null}"

CLAUDE_MODEL="${CLAUDE_MODEL:-claude-opus-4-6}"

cd "$REPO_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
LOG_DIR="$(dirname "$LOG_FILE")"

echo "[agent] Two-phase CWV agent (claude-code live-bench, model=$CLAUDE_MODEL)" > "$LOG_FILE"
echo "[agent] PAGE_URL=$PAGE_URL DOMAIN=$DOMAIN" >> "$LOG_FILE"

PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"
PHASE1_NDJSON="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1.ndjson"
PHASE2_NDJSON="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase2.ndjson"

# ============================================================
# Phase 1 workspace: repo read-only, plan.md writable only
# ============================================================
PHASE1_DIR="$(mktemp -d)"

_write_usage() {
  local usage_file="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_usage.json"
  [[ -f "$usage_file" ]] && return
  python3 - "$PHASE1_NDJSON" "$PHASE2_NDJSON" "$usage_file" 2>>"$LOG_FILE" << 'PYEOF'
import json, sys

def parse_ndjson(path):
    cost = 0.0
    tok = {'input': 0, 'output': 0, 'cache_creation': 0, 'cache_read': 0, 'thinking': 0}
    tool_calls = 0
    model_usage = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    ev = json.loads(line)
                    t = ev.get('type', '')
                    if t == 'result':
                        cost += ev.get('total_cost_usd', 0) or ev.get('cost_usd', 0) or 0
                        u = ev.get('usage', {}) or {}
                        tok['input']          += u.get('input_tokens', 0) or 0
                        tok['output']         += u.get('output_tokens', 0) or 0
                        tok['cache_creation'] += u.get('cache_creation_input_tokens', 0) or 0
                        tok['cache_read']     += u.get('cache_read_input_tokens', 0) or 0
                        for it in (u.get('iterations') or []):
                            iu = it.get('usage', {}) or {}
                            tok['thinking'] += iu.get('thinking_tokens', 0) or iu.get('thinking_input_tokens', 0) or 0
                        for model, mu in (ev.get('modelUsage') or {}).items():
                            if model not in model_usage:
                                model_usage[model] = {'input_tokens': 0, 'output_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0, 'cost_usd': 0.0, 'tool_calls': 0}
                            model_usage[model]['input_tokens']               += mu.get('inputTokens', 0) or 0
                            model_usage[model]['output_tokens']              += mu.get('outputTokens', 0) or 0
                            model_usage[model]['cache_creation_input_tokens'] += mu.get('cacheCreationInputTokens', 0) or 0
                            model_usage[model]['cache_read_input_tokens']    += mu.get('cacheReadInputTokens', 0) or 0
                            model_usage[model]['cost_usd']                   += mu.get('costUSD', 0) or 0
                    elif t == 'assistant':
                        for item in (ev.get('message', {}) or {}).get('content', []) or []:
                            if not isinstance(item, dict): continue
                            if item.get('type') == 'tool_use':
                                tool_calls += 1
                            elif item.get('type') == 'thinking':
                                tok['thinking'] += len(item.get('thinking', ''))
                except Exception: pass
    except Exception: pass
    for m in model_usage:
        model_usage[m]['cost_usd'] = round(model_usage[m]['cost_usd'], 6)
    return {'cost_usd': round(cost, 6), 'tokens': tok, 'tool_calls': tool_calls, 'model_usage': model_usage}

def merge_model_usage(a, b):
    merged = dict(a)
    for model, mu in b.items():
        if model not in merged:
            merged[model] = dict(mu)
        else:
            merged[model] = {k: merged[model][k] + mu[k] for k in mu}
            merged[model]['cost_usd'] = round(merged[model]['cost_usd'], 6)
    return merged

p1 = parse_ndjson(sys.argv[1])
p2 = parse_ndjson(sys.argv[2])
total = {k: p1['tokens'][k] + p2['tokens'][k] for k in p1['tokens']}
total['total'] = total['input'] + total['output']
with open(sys.argv[3], 'w') as f:
    json.dump({'cost_usd': round(p1['cost_usd'] + p2['cost_usd'], 6),
               'tokens': total,
               'tool_calls': p1['tool_calls'] + p2['tool_calls'],
               'model_usage': merge_model_usage(p1['model_usage'], p2['model_usage']),
               'phases': {'phase1': p1, 'phase2': p2}}, f, indent=2)
PYEOF
}

trap '_write_usage; chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"' EXIT

# Write cwv_context.json into REPO_DIR first (Phase 2 also reads it from here)
CWV_CONTEXT_FILE="$REPO_DIR/cwv_context.json"
python3 - <<PYSCRIPT > "$CWV_CONTEXT_FILE"
import json
def load_json_var(s):
    try:
        return json.loads(s)
    except Exception:
        return None

ctx = {
    "page_url":   "$PAGE_URL",
    "domain":     "$DOMAIN",
    "field_cwv": {
        "note":    "Real-user data from Google CrUX (live page traffic)",
        "mobile":  load_json_var("""$CWV_FIELD_MOBILE"""),
        "desktop": load_json_var("""$CWV_FIELD_DESKTOP"""),
    },
    "synthetic_cwv": {
        "note":    "Lighthouse measured on this local mirror just before the agent ran",
        "mobile":  load_json_var("""$CWV_SYNTHETIC_MOBILE"""),
        "desktop": load_json_var("""$CWV_SYNTHETIC_DESKTOP"""),
        "lcp_entries_mobile":  load_json_var("""$LCP_ENTRIES_MOBILE"""),
        "lcp_entries_desktop": load_json_var("""$LCP_ENTRIES_DESKTOP"""),
    },
}
print(json.dumps(ctx, indent=2))
PYSCRIPT
echo "[agent] cwv_context.json written" >> "$LOG_FILE"

# Copy repo (with cwv_context.json) to phase1 workspace
cp -r "$REPO_DIR" "$PHASE1_DIR/repo"

# Make repo read-only so model can only write plan.md
chmod -R a-w "$PHASE1_DIR/repo"

# plan.md is the only writable file in the workspace
touch "$PHASE1_DIR/plan.md"

# -------------------------
# Model config
# -------------------------
export CLAUDE_CODE_EFFORT_LEVEL="${CLAUDE_CODE_EFFORT_LEVEL:-medium}"
export CLAUDE_MAX_TOKENS="${CLAUDE_MAX_TOKENS:-50000}"
# Force claude CLI to use ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL from env only.
# CLAUDE_CODE_SIMPLE=1 bypasses OAuth/keychain and format validation (Azure keys are not sk-ant-*).
export CLAUDE_CODE_SIMPLE=1
unset CLAUDE_CODE_USE_FOUNDRY 2>/dev/null || true

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "[agent] WARNING: ANTHROPIC_API_KEY not set; claude CLI may fail" >> "$LOG_FILE"
fi

if ! command -v claude &>/dev/null; then
  echo "[agent] ERROR: claude CLI not found. Install Claude Code: https://claude.ai/code" >> "$LOG_FILE"
  exit 1
fi

# ============================================================
# Phase 1 — Planning
# ============================================================

# Load specific suggestion if running inside suggestions eval pipeline
SUGGESTION_CONTENT=""
if [[ -n "${EVAL_SUGGESTION_FILE:-}" && -f "${EVAL_SUGGESTION_FILE}" ]]; then
  SUGGESTION_CONTENT="$(python3 -c "
import json, sys
d = json.load(open('$EVAL_SUGGESTION_FILE'))
print('Title:       ' + d.get('title',''))
print('Metric:      ' + d.get('metric',''))
print('Priority:    ' + d.get('priority',''))
print('Description: ' + d.get('description',''))
print('Implementation: ' + d.get('implementation',''))
" 2>/dev/null || cat "$EVAL_SUGGESTION_FILE")"
  echo "[agent] Loaded suggestion from EVAL_SUGGESTION_FILE" >> "$LOG_FILE"
fi

cat <<EOF > "$PLAN_PROMPT"
You are a web performance analyst optimizing a mirrored live web page.

Page URL: $PAGE_URL
Domain:   $DOMAIN

$(if [[ -n "$SUGGESTION_CONTENT" ]]; then
  printf '## Target Suggestion to Implement\n\n%s\n\nYour PRIMARY goal is to implement the suggestion above.\n' "$SUGGESTION_CONTENT"
fi)

You have access to repo/cwv_context.json (read-only) which contains CWV baselines:

1. **Field CWV** (Google CrUX — real user data from the live site):
   Mobile:  $CWV_FIELD_MOBILE
   Desktop: $CWV_FIELD_DESKTOP

2. **Synthetic CWV** (Lighthouse on this LOCAL mirror, measured moments ago):
   Mobile:  $CWV_SYNTHETIC_MOBILE
   Desktop: $CWV_SYNTHETIC_DESKTOP

Your changes will be benchmarked using Lighthouse — prioritize improvements
that Lighthouse can detect locally (LCP element loading, CLS from unsized
images/fonts, render-blocking JS/CSS).

Read repo/cwv_context.json for LCP element detail and lcp_entries.
Read the repository files under repo/ to understand the structure.

IMPORTANT: You MUST always write plan.md regardless of whether metrics look good or bad.
Even if scores appear healthy, there are always further optimizations possible.

Write to plan.md (in the current directory, NOT inside repo/):
1. **Target Change**: Exactly what needs to change to implement the suggestion (cite file paths under repo/)
2. **Files to Modify**: Full list of files that need changes (paths relative to repo/)
3. **Proposed Changes**: For each file — the specific code change and why it improves CWV
4. **Expected Impact**: Estimated LCP / CLS / INP improvement per device

Rules for this phase:
- Write ONLY to plan.md (at the top level, next to repo/)
- Do NOT modify any file inside repo/
- Do NOT write actual code — describe changes in plain English
- Do NOT create additional files
- Do NOT ask the user questions; proceed autonomously with your best judgment
EOF

cp "$PLAN_PROMPT" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_prompt.txt"
echo "[agent] Phase 1: Planning..." >> "$LOG_FILE"

PHASE1_START=$(date +%s)
(cd "$PHASE1_DIR" && claude -p \
  --model "$CLAUDE_MODEL" \
  --effort medium \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --verbose \
  --no-session-persistence \
  "$(<"$PLAN_PROMPT")") \
  2>> "$LOG_FILE" > "$PHASE1_NDJSON"
PHASE1_EXIT=$?
PHASE1_END=$(date +%s)
echo "[agent] Phase 1 complete: exit=$PHASE1_EXIT duration=$((PHASE1_END - PHASE1_START))s plan=$(wc -c < "$PHASE1_DIR/plan.md")bytes" >> "$LOG_FILE"

PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  touch "$PLAN_COPY"
  exit 0
fi

# Copy plan to REPO_DIR so Phase 2 can read it
cp "$PHASE1_DIR/plan.md" "$REPO_DIR/plan.md"
[[ -s "$REPO_DIR/plan.md" ]] || { echo "[agent] ERROR: plan.md copy failed or empty" >> "$LOG_FILE"; exit 1; }

cp "$REPO_DIR/plan.md" "$PLAN_COPY"

echo "[agent] === plan.md content ===" >> "$LOG_FILE"
cat "$REPO_DIR/plan.md" >> "$LOG_FILE"
echo "[agent] === end plan.md ===" >> "$LOG_FILE"

# ============================================================
# Phase 2 — Execution
# ============================================================
{
  printf 'You are an expert web performance engineer optimizing a mirrored live web page.\n\n'
  printf 'Page: %s\nDomain: %s\n\n' "$PAGE_URL" "$DOMAIN"
  printf 'Dual CWV baseline for reference:\n'
  printf '  Field CWV mobile (real users):       %s\n' "$CWV_FIELD_MOBILE"
  printf '  Field CWV desktop (real users):      %s\n' "$CWV_FIELD_DESKTOP"
  printf '  Synthetic CWV mobile (Lighthouse):   %s\n' "$CWV_SYNTHETIC_MOBILE"
  printf '  Synthetic CWV desktop (Lighthouse):  %s\n\n' "$CWV_SYNTHETIC_DESKTOP"
  printf 'Your Task:\nExecute the code modifications specified in plan.md (in this directory) to optimize CWV metrics (LCP, CLS, INP) for both mobile and desktop.\n\n'
  printf 'Implementation Constraints:\n'
  printf '  - Edit ONLY existing files in this repository\n'
  printf '  - Do NOT add a build system, bundler, or new external dependencies\n'
  printf '  - Do NOT change visible content or page layout\n'
  printf '  - Do NOT edit cwv_context.json or plan.md\n'
  printf '  - Asset paths must remain valid (already rewritten to relative paths)\n\n'
  printf 'Focus on executing the concrete file modifications from plan.md.\n'
  printf 'Do not ask the user questions; proceed autonomously.\n'
} > "$EXEC_PROMPT"

EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"
printf "%s" "$EXEC_PROMPT_CONTENT" > "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase2_prompt.txt"

echo "[agent] Phase 2: Executing plan..." >> "$LOG_FILE"

set +e
PHASE2_START=$(date +%s)
claude -p \
  --model "$CLAUDE_MODEL" \
  --effort medium \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --verbose \
  --no-session-persistence \
  "$EXEC_PROMPT_CONTENT" \
  2>> "$LOG_FILE" > "$PHASE2_NDJSON"
PHASE2_EXIT=$?
set -e

PHASE2_END=$(date +%s)
echo "[agent] Phase 2 complete: exit=$PHASE2_EXIT duration=$((PHASE2_END - PHASE2_START))s" >> "$LOG_FILE"

if [[ "$PHASE2_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: Phase 2 claude returned non-zero ($PHASE2_EXIT), continuing to capture diff" >> "$LOG_FILE"
fi

# Remove planning/context artefacts before patch capture
rm -f "$REPO_DIR/plan.md" "$REPO_DIR/cwv_context.json"
echo "[agent] Removed plan.md + cwv_context.json before patch capture" >> "$LOG_FILE"

git diff > "$PATCH_FILE"
PATCH_LINES=$(wc -l < "$PATCH_FILE")
echo "[agent] Patch: $PATCH_LINES lines" >> "$LOG_FILE"
git reset --hard HEAD
git clean -fd

echo "[agent] Done" >> "$LOG_FILE"
