#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Common agent template (OpenCode + open-source/vLLM model)
# ============================================================

REPO_DIR="$1"
TASK_SPEC="$2"
LOG_FILE="$3"
PATCH_FILE="${4:-/dev/null}"

mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee "$LOG_FILE") 2>&1

FRAMEWORK="${FRAMEWORK:-unknown}"
DEVICE="${DEVICE:-unknown}"

cd "$REPO_DIR"
LOG_DIR="$(dirname "$LOG_FILE")"

# Redirect all temp I/O off /tmp (which lives on the small overlay FS) onto
# /dev/shm which has ~1TB of tmpfs headroom.  A per-run scratch dir is created
# under LOG_DIR/../scratch so everything is co-located with results and is
# cleaned up by the same trap below.
SCRATCH_BASE="$(dirname "$LOG_DIR")/scratch"
mkdir -p "$SCRATCH_BASE"
export TMPDIR="$SCRATCH_BASE"
# OpenCode (Bun) stores its SQLite session DB under XDG_DATA_HOME; redirect it
# to the same scratch area so it never touches /tmp or ~/.local.
# Use EVAL_JOB_LABEL (set by evaluate.sh) to make this path unique per parallel
# job — sharing a single XDG dir across concurrent jobs causes SQLite locking
# errors and corrupted sessions.
_JOB_SCRATCH_ID="${EVAL_JOB_LABEL:-$$}"
export XDG_DATA_HOME="$SCRATCH_BASE/xdg_${_JOB_SCRATCH_ID}"
mkdir -p "$XDG_DATA_HOME"

echo "[agent] Two-phase CWV agent (opencode + opensource model)"

PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"
PHASE1_NDJSON="$(mktemp)"
PHASE2_NDJSON="$(mktemp)"

# ============================================================
# Phase 1 workspace: repo read-only, plan.md writable only
# ============================================================
PHASE1_DIR="$(mktemp -d)"

PHASE1_WALL=0
PHASE2_WALL=0

_write_usage() {
  local usage_file="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_usage.json"
  [[ -f "$usage_file" ]] && return
  python3 - "$PHASE1_NDJSON" "$PHASE2_NDJSON" "$usage_file" "$PHASE1_WALL" "$PHASE2_WALL" << 'PYEOF'
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
p1['wall_seconds'] = int(sys.argv[4]) if len(sys.argv) > 4 else 0
p2['wall_seconds'] = int(sys.argv[5]) if len(sys.argv) > 5 else 0
total = {k: p1['tokens'][k] + p2['tokens'][k] for k in p1['tokens']}
total['total'] = total['input'] + total['output'] + total['reasoning']
with open(sys.argv[3], 'w') as f:
    json.dump({'cost_usd': round(p1['cost_usd'] + p2['cost_usd'], 6),
               'tokens': total,
               'tool_calls': p1['tool_calls'] + p2['tool_calls'],
               'phases': {'phase1': p1, 'phase2': p2}}, f, indent=2)
PYEOF
}

trap '_write_usage; chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR" "$XDG_DATA_HOME"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT" "$PHASE1_NDJSON" "$PHASE2_NDJSON"' EXIT

# Copy repo to phase1 workspace (repo will be made read-only)
cp -r "$REPO_DIR" "$PHASE1_DIR/repo"

# Write init CWV data for the model to read.
# evaluate.sh serializes all large CWV vars to EVAL_CWV_DATA_FILE to avoid E2BIG.
# That file includes CLS_SHIFTS and INP_INTERACTIONS which are important agent inputs.
if [[ -n "${EVAL_CWV_DATA_FILE:-}" && -f "$EVAL_CWV_DATA_FILE" ]]; then
  python3 - "$EVAL_CWV_DATA_FILE" "$PHASE1_DIR/repo/init_cwv.json" << 'PYEOF'
import json, sys

def parse_val(v):
    if v is None or v == '' or v == ' ':
        return None
    try:
        return json.loads(v)
    except Exception:
        return v

with open(sys.argv[1]) as f:
    d = json.load(f)

out = {
    "mobile":                  parse_val(d.get("CWV_BASELINE_MOBILE")),
    "desktop":                 parse_val(d.get("CWV_BASELINE_DESKTOP")),
    "lcp_entries_mobile":      parse_val(d.get("LCP_ENTRIES_MOBILE")),
    "lcp_entries_desktop":     parse_val(d.get("LCP_ENTRIES_DESKTOP")),
    "cls_shifts_mobile":       parse_val(d.get("CLS_SHIFTS_MOBILE")),
    "cls_shifts_desktop":      parse_val(d.get("CLS_SHIFTS_DESKTOP")),
    "inp_interactions_mobile": parse_val(d.get("INP_INTERACTIONS_MOBILE")),
    "inp_interactions_desktop":parse_val(d.get("INP_INTERACTIONS_DESKTOP")),
}
with open(sys.argv[2], 'w') as f:
    json.dump(out, f)
PYEOF
  # Read back the summarised scores for use in the prompt
  CWV_MOBILE="$(python3 -c "import json; d=json.load(open('$PHASE1_DIR/repo/init_cwv.json')); v=d.get('mobile'); print(json.dumps(v) if v is not None else 'null')")"
  CWV_DESKTOP="$(python3 -c "import json; d=json.load(open('$PHASE1_DIR/repo/init_cwv.json')); v=d.get('desktop'); print(json.dumps(v) if v is not None else 'null')")"
else
  # Fallback: build from env vars directly (no EVAL_CWV_DATA_FILE)
  CWV_MOBILE="${CWV_BASELINE_MOBILE:-}"
  CWV_DESKTOP="${CWV_BASELINE_DESKTOP:-}"
  LCP_MOBILE="${LCP_ENTRIES_MOBILE:-}"
  LCP_DESKTOP="${LCP_ENTRIES_DESKTOP:-}"
  [[ "$CWV_MOBILE" == " " || -z "$CWV_MOBILE" ]] && CWV_MOBILE="null"
  [[ "$CWV_DESKTOP" == " " || -z "$CWV_DESKTOP" ]] && CWV_DESKTOP="null"
  [[ "$LCP_MOBILE" == " " || -z "$LCP_MOBILE" ]] && LCP_MOBILE="null"
  [[ "$LCP_DESKTOP" == " " || -z "$LCP_DESKTOP" ]] && LCP_DESKTOP="null"
  printf '{"mobile":%s,"desktop":%s,"lcp_entries_mobile":%s,"lcp_entries_desktop":%s}\n' \
    "$CWV_MOBILE" "$CWV_DESKTOP" "$LCP_MOBILE" "$LCP_DESKTOP" > "$PHASE1_DIR/repo/init_cwv.json"
fi

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
OPENCODE_OPENAI_BASE_URL="${OPENCODE_OPENAI_BASE_URL:-${OPENAI_BASE_URL:-}}"
if [[ -n "$OPENCODE_OPENAI_BASE_URL" ]]; then
  # vLLM exposes an OpenAI-compatible API at /v1. The served model name must
  # match the name passed to vLLM via --served-model-name.
  OPENCODE_MODEL="${OPENCODE_MODEL:-vllm/${VLLM_SERVED_MODEL_NAME:-local-model}}"
  OPENAI_API_KEY="${OPENAI_API_KEY:-${VLLM_API_KEY:-EMPTY}}"
  export OPENAI_API_KEY
elif [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  # Set OPENCODE_MODEL to your Azure deployment, e.g. azure/gpt-4.1
  OPENCODE_MODEL="${OPENCODE_MODEL:-}"
  # OpenCode requires AZURE_RESOURCE_NAME; derive from AZURE_OPENAI_ENDPOINT if unset
  if [[ -z "${AZURE_RESOURCE_NAME:-}" && -n "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
    # e.g. https://myresource.cognitiveservices.azure.com -> myresource
    AZURE_RESOURCE_NAME="${AZURE_OPENAI_ENDPOINT#*://}"
    AZURE_RESOURCE_NAME="${AZURE_RESOURCE_NAME%%.*}"
    export AZURE_RESOURCE_NAME
  fi
else
  # Set OPENCODE_MODEL to your desired provider/model, e.g. openrouter/qwen/qwen-2.5-72b-instruct
  OPENCODE_MODEL="${OPENCODE_MODEL:-}"
fi

if ! command -v opencode &>/dev/null; then
  echo "[agent] ERROR: opencode CLI not found. Install: curl -fsSL https://opencode.ai/install | bash"
  exit 1
fi

echo "OPENCODE_MODEL: $OPENCODE_MODEL"

# Auth: run `opencode auth login` to configure providers. Common env vars:
#   openai/*: OPENAI_API_KEY
#   azure/*: AZURE_OPENAI_API_KEY, AZURE_RESOURCE_NAME (or AZURE_OPENAI_ENDPOINT)
#   openrouter/*: OPENROUTER_API_KEY
#   302ai/*, aihubmix/*, etc.: see provider docs at opencode.ai/docs/providers

# OpenCode config: permission + reasoning effort + max output tokens (matches Codex)
# Override via OPENCODE_REASONING_EFFORT (default: medium) or OPENCODE_MAX_TOKENS (default: 50000)
OPENCODE_REASONING="${OPENCODE_REASONING_EFFORT:-medium}"
OPENCODE_MAX="${OPENCODE_MAX_TOKENS:-8000}"
# Deny question tool: in non-interactive mode it blocks indefinitely (opencode run has no TUI to answer)

_urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

_opencode_base_for_phase() {
  local phase="$1"
  if [[ -n "$OPENCODE_OPENAI_BASE_URL" && "${OPENCODE_USAGE_PROXY:-0}" == "1" ]]; then
    local base="${OPENCODE_OPENAI_BASE_URL%/}"
    local root="${base%/v1}"
    local job_encoded phase_encoded
    job_encoded="$(_urlencode "${EVAL_JOB_LABEL:-unknown}")"
    phase_encoded="$(_urlencode "$phase")"
    echo "$root/__usage/$job_encoded/$phase_encoded/v1"
  else
    echo "$OPENCODE_OPENAI_BASE_URL"
  fi
}

_build_opencode_cfg() {
  local phase="$1"
  if [[ -n "$OPENCODE_OPENAI_BASE_URL" ]]; then
    local phase_base model_name
    phase_base="$(_opencode_base_for_phase "$phase")"
    # Strip provider prefix (e.g. "vllm/qwen3-coder-next" → "qwen3-coder-next")
    model_name="${OPENCODE_MODEL#*/}"
    # Use @ai-sdk/openai-compatible so OpenCode accepts arbitrary model IDs
    # without requiring them to be in its built-in registry.
    printf '{"permission":{"question":"deny","external_directory":"allow"},"small_model":"vllm/%s","provider":{"vllm":{"npm":"@ai-sdk/openai-compatible","name":"vLLM","options":{"apiKey":"%s","baseURL":"%s","maxTokens":%s},"models":{"%s":{"name":"%s"}}}}}' \
      "$model_name" "$OPENAI_API_KEY" "$phase_base" "$OPENCODE_MAX" "$model_name" "$model_name"
  else
    printf '{"permission":{"question":"deny","external_directory":"allow"},"small_model":"%s","provider":{"azure":{"options":{"maxTokens":%s}},"openai":{"options":{"maxTokens":%s}}}}' \
      "$OPENCODE_MODEL" "$OPENCODE_MAX" "$OPENCODE_MAX"
  fi
}

# ============================================================
# Phase 1 — Planning
# ============================================================
# ── Prompt override hook (harness/prompt_optimisation/bridge/runner.py) ──────
# PHASE1_INSTRUCTION: full instruction text with ${FRAMEWORK}, ${CWV_MOBILE},
# ${CWV_DESKTOP} placeholders expanded via envsubst. When unset, falls through
# to the hardcoded default below — fully backward-compatible.
if [[ -n "${PHASE1_INSTRUCTION:-}" ]]; then
  printf '%s' "$PHASE1_INSTRUCTION" \
    | FRAMEWORK="$FRAMEWORK" CWV_MOBILE="$CWV_MOBILE" CWV_DESKTOP="$CWV_DESKTOP" \
      envsubst '${FRAMEWORK}${CWV_MOBILE}${CWV_DESKTOP}' > "$PLAN_PROMPT"
else
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
- repo/init_cwv.json: Contains full CWV data (scores, lcp_entries, cls_shifts, inp_interactions for mobile and desktop)
- repo/: Complete source code for the application

Write plan.md with these sections:

   ## Performance Issues Identified
   - List specific CWV metrics that need improvement (with current values)
   - List specific CWV metrics that need improvement and provide exact suggestions

Output Instructions:
- You can read files to get better understanding of the codebase
- WRITE the plan to 'plan.md' in the current directory
- List specific CWV metrics that need improvement and provide exact suggestions
- Use valid Markdown formatting
- Be specific about file paths and code changes
- DO NOT modify any repository files (init_cwv.json or source code)
- DO NOT create additional files or output to chat
- DO NOT ask the user questions; proceed autonomously with your best judgment
EOF
fi

cp "$PLAN_PROMPT" "$LOG_DIR/phase1_prompt.txt"

# -------- OPENCODE RUN (PHASE 1) — workspace=PHASE1_DIR, repo read-only, plan.md writable --------
PHASE1_STDERR="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_stderr.txt"
OPENCODE_CFG="$(_build_opencode_cfg phase1)"
set +e
_PHASE1_T0=$SECONDS
(cd "$PHASE1_DIR" && OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" \
  timeout "${OPENCODE_PHASE_TIMEOUT:-3600}" opencode run \
  --format json \
  --model "$OPENCODE_MODEL" \
  "$(<"$PLAN_PROMPT")") 2>"$PHASE1_STDERR" > "$PHASE1_NDJSON"
PHASE1_EXIT=$?
PHASE1_WALL=$(( SECONDS - _PHASE1_T0 ))
echo "[agent] Phase 1 wall time: ${PHASE1_WALL}s"
set -e
# -------------------------------------

# plan.md is the only writable file; repo/ was chmod read-only
PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  # OpenCode may output plan to stdout (NDJSON text events) instead of writing plan.md
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
# Match various heading styles: ## Heading, **Heading**, # Heading, or bare "Performance Issues"
m = re.search(r'(?:^|\n)(#{1,3} |[*]{1,2})?Performance Issues', text)
if m:
    idx = m.start() if text[m.start()] == '\n' else m.start()
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text[idx:])
    with open(sys.argv[2], 'w') as f:
        f.write(clean)
elif len(text) > 200:
    # Fallback: if the model produced substantial text but no matching header, save it all
    clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    with open(sys.argv[2], 'w') as f:
        f.write(clean)
PYEOF
fi

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty"
  echo "[agent] Phase 1 exit code: $PHASE1_EXIT"
  cp "$PHASE1_NDJSON" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_stdout.txt"
  echo "[agent] Debug: Phase 1 NDJSON saved to *_phase1_stdout.txt, stderr to *_phase1_stderr.txt"
  if [[ -s "$PHASE1_STDERR" ]]; then
    echo "[agent] === Phase 1 stderr (last 50 lines) ==="
    tail -50 "$PHASE1_STDERR"
    echo "[agent] === end stderr ==="
  fi
  touch "$PLAN_COPY"
  exit 0
fi

# Copy plan to REPO_DIR so Phase 2 can read it (avoids passing huge prompt as CLI arg)
cp "$PHASE1_DIR/plan.md" "$REPO_DIR/plan.md"
[[ -s "$REPO_DIR/plan.md" ]] || { echo "[agent] ERROR: plan.md copy failed or empty"; exit 1; }

# Copy plan.md to results folder for post-analysis
cp "$REPO_DIR/plan.md" "$PLAN_COPY"

# Append plan content to log for visibility
echo "[agent] === plan.md content ==="
cat "$REPO_DIR/plan.md"
echo "[agent] === end plan.md ==="

# ============================================================
# Phase 2 — Execution (plan in repo, prompt as arg)
# ============================================================
# ── Prompt override hook (harness/prompt_optimisation/bridge/runner.py) ──────
if [[ -n "${PHASE2_INSTRUCTION:-}" ]]; then
  printf '%s' "$PHASE2_INSTRUCTION" \
    | FRAMEWORK="$FRAMEWORK" envsubst '${FRAMEWORK}' > "$EXEC_PROMPT"
else
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
fi

EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"

printf "%s" "$EXEC_PROMPT_CONTENT" > "$LOG_DIR/phase2_prompt.txt"

set +e
OPENCODE_CFG="$(_build_opencode_cfg phase2)"
PHASE2_STDERR="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase2_stderr.txt"
_PHASE2_T0=$SECONDS
(cd "$REPO_DIR" && OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" \
  timeout "${OPENCODE_PHASE_TIMEOUT:-3600}" opencode run \
  --format json \
  --model "$OPENCODE_MODEL" \
  "$EXEC_PROMPT_CONTENT") 2>"$PHASE2_STDERR" > "$PHASE2_NDJSON"
PHASE2_EXIT=$?
PHASE2_WALL=$(( SECONDS - _PHASE2_T0 ))
echo "[agent] Phase 2 wall time: ${PHASE2_WALL}s"
set -e

if [[ "$PHASE2_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: Phase 2 opencode returned non-zero ($PHASE2_EXIT), continuing to capture diff"
fi

# -------------------------------------

# Remove plan.md from repo before capturing diff (planning artifact, not a code change)
rm -f "$REPO_DIR/plan.md"

git diff > "$PATCH_FILE"
git reset --hard HEAD
git clean -fd
rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"

echo "[agent] Done"
