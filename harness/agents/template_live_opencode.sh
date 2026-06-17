#!/usr/bin/env bash
set -euo pipefail

# Route all temp files to /dev/shm (overlay /tmp is small; /dev/shm has ~1 TB free)
export TMPDIR="${BENCH_TMPDIR:-/dev/shm}"

# ============================================================
# OpenCode agent for harness
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

cd "$REPO_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
LOG_DIR="$(dirname "$LOG_FILE")"

echo "[agent] Two-phase CWV agent (opencode live-bench)" > "$LOG_FILE"
echo "[agent] PAGE_URL=$PAGE_URL DOMAIN=$DOMAIN" >> "$LOG_FILE"

PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"
PHASE1_NDJSON="$(mktemp)"
PHASE2_NDJSON="$(mktemp)"
OPENCODE_DATA_DIR="$(mktemp -d)"

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
    tok = {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0, 'cache_write': 0}
    tool_calls = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
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
                except Exception: pass
    except Exception: pass
    return {'cost_usd': round(cost, 6), 'tokens': tok, 'tool_calls': tool_calls}

p1 = parse_ndjson(sys.argv[1])
p2 = parse_ndjson(sys.argv[2])
total = {k: p1['tokens'][k] + p2['tokens'][k] for k in p1['tokens']}
total['total'] = total['input'] + total['output'] + total['reasoning']
with open(sys.argv[3], 'w') as f:
    json.dump({'cost_usd': round(p1['cost_usd'] + p2['cost_usd'], 6),
               'tokens': total,
               'tool_calls': p1['tool_calls'] + p2['tool_calls'],
               'phases': {'phase1': p1, 'phase2': p2}}, f, indent=2)
PYEOF
}

trap '_write_usage; chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR" "$OPENCODE_DATA_DIR"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT" "$PHASE1_NDJSON" "$PHASE2_NDJSON"' EXIT

# Write cwv_context.json into REPO_DIR first (Phase 2 will also use it from here)
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

# Copy repo (with cwv_context.json already in it) to phase1 workspace
cp -r "$REPO_DIR" "$PHASE1_DIR/repo"

# Hide .git so OpenCode treats PHASE1_DIR as cwd=project root
if [[ -d "$PHASE1_DIR/repo/.git" ]]; then
  mv "$PHASE1_DIR/repo/.git" "$PHASE1_DIR/repo/.git.bak"
fi

# Make repo read-only so model can only write plan.md
chmod -R a-w "$PHASE1_DIR/repo"

# plan.md is the only writable file in the workspace
touch "$PHASE1_DIR/plan.md"

# -------------------------
# Model config
# -------------------------
if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  AZURE_DEPLOY="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5}"
  OPENCODE_MODEL="${OPENCODE_MODEL:-azure/$AZURE_DEPLOY}"
  if [[ -z "${AZURE_RESOURCE_NAME:-}" && -n "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
    AZURE_RESOURCE_NAME="${AZURE_OPENAI_ENDPOINT#*://}"
    AZURE_RESOURCE_NAME="${AZURE_RESOURCE_NAME%%.*}"
    export AZURE_RESOURCE_NAME
  fi
else
  OPENCODE_MODEL="${OPENCODE_MODEL:-openai/gpt-5}"
fi

if ! command -v opencode &>/dev/null; then
  echo "[agent] ERROR: opencode CLI not found. Install: curl -fsSL https://opencode.ai/install | bash" >> "$LOG_FILE"
  exit 1
fi

OPENCODE_REASONING="${OPENCODE_REASONING_EFFORT:-medium}"
OPENCODE_MAX="${OPENCODE_MAX_TOKENS:-50000}"
OPENCODE_CFG="{\"permission\":{\"question\":\"deny\"},\"small_model\":\"$OPENCODE_MODEL\",\"provider\":{\"azure\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}},\"openai\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}}}}"

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
- Do NOT ask the user questions; proceed autonomously with your best judgment
EOF

cp "$PLAN_PROMPT" "$LOG_DIR/phase1_prompt.txt"
echo "[agent] Phase 1: Planning..." >> "$LOG_FILE"

# Retry up to 3 times with backoff
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
    "$(<"$PLAN_PROMPT")") > "$PHASE1_NDJSON" 2>> "$LOG_FILE"
  PHASE1_EXIT=$?
  _p1_ndjson_sz=$(wc -c < "$PHASE1_NDJSON"); _p1_plan_sz=$(wc -c < "$PHASE1_DIR/plan.md")
  echo "[agent] Phase 1 attempt $_p1_attempt: NDJSON=${_p1_ndjson_sz}bytes plan=${_p1_plan_sz}bytes exit=$PHASE1_EXIT" >> "$LOG_FILE"
  # If plan.md is empty, try to extract from NDJSON text events
  if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
    python3 - "$PHASE1_NDJSON" "$PHASE1_DIR/plan.md" << 'PYEOF'
import json, sys, re
text = ''
try:
    with open(sys.argv[1]) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                ev = json.loads(line)
                if ev.get('type') == 'text':
                    text += ev.get('part', {}).get('text', '')
            except Exception: pass
except Exception: pass
marker = '## Baseline Analysis'
idx = text.find(marker)
if idx == -1:
    marker = '## Performance Issues'
    idx = text.find(marker)
if idx != -1:
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text[idx:])
    with open(sys.argv[2], 'w') as f:
        f.write(clean)
PYEOF
  fi
  [[ -s "$PHASE1_DIR/plan.md" ]] && break
  echo "[agent] Phase 1 attempt $_p1_attempt: plan.md still empty after extraction" >> "$LOG_FILE"
done

PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  # Save NDJSON for diagnosis
  cp "$PHASE1_NDJSON" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_ndjson.txt" 2>/dev/null || true
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
  printf 'You created this optimization plan. Now execute it precisely.\n\n'
  printf 'Read plan.md in this directory for the full plan.\n\n'
  printf 'Rules:\n'
  printf '  - Edit ONLY existing files in this repository\n'
  printf '  - Do NOT add a build system, bundler, or new external dependencies\n'
  printf '  - Do NOT change visible content or page layout\n'
  printf '  - Do NOT edit cwv_context.json or plan.md\n'
  printf '  - Asset paths must remain valid (already rewritten to relative paths)\n'
  printf '  - Do NOT ask the user questions; proceed autonomously.\n\n'
  printf 'Implement all changes from the plan now.\n'
} > "$EXEC_PROMPT"

EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"
printf "%s" "$EXEC_PROMPT_CONTENT" > "$LOG_DIR/phase2_prompt.txt"

echo "[agent] Phase 2: Executing plan..." >> "$LOG_FILE"

set +e
PHASE2_EXIT=1
for _p2_attempt in 1 2 3; do
  if [[ $_p2_attempt -gt 1 ]]; then
    _p2_wait=$(( (_p2_attempt - 1) * 30 ))
    echo "[agent] Phase 2 retry $_p2_attempt after ${_p2_wait}s" >> "$LOG_FILE"
    sleep "$_p2_wait"
  fi
  : > "$PHASE2_NDJSON"
  (cd "$REPO_DIR" && XDG_DATA_HOME="$OPENCODE_DATA_DIR" OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
    --format json \
    --model "$OPENCODE_MODEL" \
    "$EXEC_PROMPT_CONTENT") > "$PHASE2_NDJSON" 2>> "$LOG_FILE"
  PHASE2_EXIT=$?
  [[ -s "$PHASE2_NDJSON" ]] && break
  echo "[agent] Phase 2 attempt $_p2_attempt: OpenCode produced no output (exit=$PHASE2_EXIT)" >> "$LOG_FILE"
done
set -e

if [[ "$PHASE2_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: Phase 2 opencode returned non-zero ($PHASE2_EXIT), continuing to capture diff" >> "$LOG_FILE"
fi

# Remove planning/context artefacts before patch capture
rm -f "$REPO_DIR/plan.md" "$REPO_DIR/cwv_context.json"
echo "[agent] Removed plan.md + cwv_context.json before patch capture" >> "$LOG_FILE"

git diff > "$PATCH_FILE"
echo "[agent] Patch: $(wc -l < "$PATCH_FILE") lines" >> "$LOG_FILE"
git reset --hard HEAD
git clean -fd

echo "[agent] Done" >> "$LOG_FILE"
