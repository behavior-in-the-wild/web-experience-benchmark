#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Direct-implementation CWV agent (single phase, no planning).
# Reads EVAL_SUGGESTION_FILE and implements the suggestion
# without a prior planning / analysis phase.
# ============================================================

REPO_DIR="$1"
# $2 (TASK_SPEC) is kept for interface compatibility but unused
LOG_FILE="$3"
PATCH_FILE="${4:-/dev/null}"

mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee "$LOG_FILE") 2>&1

FRAMEWORK="${FRAMEWORK:-unknown}"

cd "$REPO_DIR"
LOG_DIR="$(dirname "$LOG_FILE")"

# Route all temp I/O off /tmp (small overlay FS) onto /dev/shm (~1 TB tmpfs)
SCRATCH_BASE="$(dirname "$LOG_DIR")/scratch"
mkdir -p "$SCRATCH_BASE"
export TMPDIR="$SCRATCH_BASE"

# Unique XDG dir per parallel job to prevent SQLite session conflicts in OpenCode
_JOB_SCRATCH_ID="${EVAL_JOB_LABEL:-$$}"
export XDG_DATA_HOME="$SCRATCH_BASE/xdg_${_JOB_SCRATCH_ID}"
mkdir -p "$XDG_DATA_HOME"

echo "[agent] Direct-implementation CWV agent (opencode + opencode model, single phase)"

EXEC_PROMPT="$(mktemp)"
PHASE_NDJSON="$(mktemp)"
PHASE_WALL=0

# ---- Usage tracking (single-phase variant) ----
_write_usage() {
  local usage_file="$LOG_DIR/usage.json"
  [[ -f "$usage_file" ]] && return
  python3 - "$PHASE_NDJSON" "$usage_file" "$PHASE_WALL" << 'PYEOF'
import json, sys

def parse_ndjson(path):
    cost, tool_calls = 0.0, 0
    tok = {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0, 'cache_write': 0}
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

p = parse_ndjson(sys.argv[1])
p['wall_seconds'] = int(sys.argv[3]) if len(sys.argv) > 3 else 0
total = dict(p['tokens'])
total['total'] = total['input'] + total['output'] + total['reasoning']
with open(sys.argv[2], 'w') as f:
    json.dump({
        'cost_usd':   p['cost_usd'],
        'tokens':     total,
        'tool_calls': p['tool_calls'],
        'phases':     {'phase1': p},
    }, f, indent=2)
PYEOF
}

trap '_write_usage; rm -rf "$XDG_DATA_HOME"; rm -f "$EXEC_PROMPT" "$PHASE_NDJSON"' EXIT

# ---- Write CWV baseline data for context (agent may read init_cwv.json) ----
if [[ -n "${EVAL_CWV_DATA_FILE:-}" && -f "$EVAL_CWV_DATA_FILE" ]]; then
  python3 - "$EVAL_CWV_DATA_FILE" "$REPO_DIR/init_cwv.json" << 'PYEOF'
import json, sys

def parse_val(v):
    if v is None or v in ('', ' '):
        return None
    try:
        return json.loads(v)
    except Exception:
        return v

with open(sys.argv[1]) as f:
    d = json.load(f)

out = {
    'mobile':                   parse_val(d.get('CWV_BASELINE_MOBILE')),
    'desktop':                  parse_val(d.get('CWV_BASELINE_DESKTOP')),
    'lcp_entries_mobile':       parse_val(d.get('LCP_ENTRIES_MOBILE')),
    'lcp_entries_desktop':      parse_val(d.get('LCP_ENTRIES_DESKTOP')),
    'cls_shifts_mobile':        parse_val(d.get('CLS_SHIFTS_MOBILE')),
    'cls_shifts_desktop':       parse_val(d.get('CLS_SHIFTS_DESKTOP')),
    'inp_interactions_mobile':  parse_val(d.get('INP_INTERACTIONS_MOBILE')),
    'inp_interactions_desktop': parse_val(d.get('INP_INTERACTIONS_DESKTOP')),
}
with open(sys.argv[2], 'w') as f:
    json.dump(out, f)
PYEOF
  CWV_MOBILE="$(python3 -c "
import json
d = json.load(open('$REPO_DIR/init_cwv.json'))
v = d.get('mobile')
print(json.dumps(v) if v is not None else 'null')
")"
  CWV_DESKTOP="$(python3 -c "
import json
d = json.load(open('$REPO_DIR/init_cwv.json'))
v = d.get('desktop')
print(json.dumps(v) if v is not None else 'null')
")"
else
  CWV_MOBILE="${CWV_BASELINE_MOBILE:-null}"
  CWV_DESKTOP="${CWV_BASELINE_DESKTOP:-null}"
  [[ "$CWV_MOBILE" == " " || -z "$CWV_MOBILE" ]] && CWV_MOBILE="null"
  [[ "$CWV_DESKTOP" == " " || -z "$CWV_DESKTOP" ]] && CWV_DESKTOP="null"
fi

# ---- Validate suggestion input ----
if [[ -z "${EVAL_SUGGESTION_FILE:-}" || ! -f "$EVAL_SUGGESTION_FILE" ]]; then
  echo "[agent] ERROR: EVAL_SUGGESTION_FILE not set or missing: '${EVAL_SUGGESTION_FILE:-}'"
  exit 1
fi

# ---- Build direct-implementation prompt via Python ----
# Python reads EVAL_SUGGESTION_FILE directly — avoids printf format-string
# issues with content starting with '-', and handles any special chars safely.
python3 - "$EVAL_SUGGESTION_FILE" "$EXEC_PROMPT" "$FRAMEWORK" "$CWV_MOBILE" "$CWV_DESKTOP" << 'PYEOF'
import json, sys

sugg_path, out_path, framework, cwv_mobile, cwv_desktop = sys.argv[1:]

with open(sugg_path, encoding='utf-8') as f:
    sugg = json.load(f)

title  = sugg.get('title', '')
metric = sugg.get('metric', '')
effort = sugg.get('effort', '')
desc   = sugg.get('description', '')
impl   = sugg.get('implementation', '')

prompt = (
    f"You are implementing a specific Core Web Vitals optimization"
    f" for a {framework} website.\n\n"
    f"## Optimization to implement\n\n"
    f"**Title:**  {title}\n"
    f"**Metric:** {metric}\n"
    f"**Effort:** {effort}\n\n"
    f"**Description:**\n{desc}\n\n"
    f"**Implementation instructions:**\n{impl}\n\n"
    f"## Baseline CWV scores\n\n"
    f"  Mobile:  {cwv_mobile}\n"
    f"  Desktop: {cwv_desktop}\n\n"
    "The file `init_cwv.json` in this directory contains the full CWV data\n"
    "(lcp_entries, cls_shifts, inp_interactions) for reference.\n\n"
    f"## {framework}-Specific Considerations\n\n"
    f"  - Work within the existing {framework} architecture and patterns\n"
    "  - Preserve all existing functionality and visible content\n\n"
    "## Implementation Constraints\n\n"
    "  - Implement ONLY this specific optimization\n"
    "  - Do NOT change visible content, layout, or remove pages\n"
    "  - Do NOT introduce new build systems or package manager dependencies\n"
    "  - Do NOT edit `init_cwv.json` or other data/config files\n"
    "  - Apply optimizations that work for both mobile and desktop viewports\n"
    "  - Proceed autonomously; do not ask questions\n"
)

with open(out_path, 'w', encoding='utf-8') as f:
    f.write(prompt)
PYEOF

EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"
cp "$EXEC_PROMPT" "$LOG_DIR/phase1_prompt.txt"

# ---- Model / provider config (mirrors template_opencode_os.sh) ----
OPENCODE_OPENAI_BASE_URL="${OPENCODE_OPENAI_BASE_URL:-${OPENAI_BASE_URL:-}}"
if [[ -n "$OPENCODE_OPENAI_BASE_URL" ]]; then
  OPENCODE_MODEL="${OPENCODE_MODEL:-vllm/${VLLM_SERVED_MODEL_NAME:-local-model}}"
  OPENAI_API_KEY="${OPENAI_API_KEY:-${VLLM_API_KEY:-EMPTY}}"
  export OPENAI_API_KEY
elif [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  OPENCODE_MODEL="${OPENCODE_MODEL:-}"
  if [[ -z "${AZURE_RESOURCE_NAME:-}" && -n "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
    AZURE_RESOURCE_NAME="${AZURE_OPENAI_ENDPOINT#*://}"
    AZURE_RESOURCE_NAME="${AZURE_RESOURCE_NAME%%.*}"
    export AZURE_RESOURCE_NAME
  fi
else
  OPENCODE_MODEL="${OPENCODE_MODEL:-}"
fi

if ! command -v opencode &>/dev/null; then
  echo "[agent] ERROR: opencode CLI not found. Install: curl -fsSL https://opencode.ai/install | bash"
  exit 1
fi

echo "OPENCODE_MODEL: $OPENCODE_MODEL"

OPENCODE_MAX="${OPENCODE_MAX_TOKENS:-8000}"

_urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

_build_opencode_cfg() {
  if [[ -n "$OPENCODE_OPENAI_BASE_URL" ]]; then
    local base model_name
    if [[ "${OPENCODE_USAGE_PROXY:-0}" == "1" ]]; then
      local root="${OPENCODE_OPENAI_BASE_URL%/}"; root="${root%/v1}"
      local job_enc phase_enc
      job_enc="$(_urlencode "${EVAL_JOB_LABEL:-unknown}")"
      phase_enc="$(_urlencode "phase1")"
      base="$root/__usage/$job_enc/$phase_enc/v1"
    else
      base="$OPENCODE_OPENAI_BASE_URL"
    fi
    model_name="${OPENCODE_MODEL#*/}"
    printf '{"permission":{"question":"deny","external_directory":"allow"},"small_model":"vllm/%s","provider":{"vllm":{"npm":"@ai-sdk/openai-compatible","name":"vLLM","options":{"apiKey":"%s","baseURL":"%s","maxTokens":%s},"models":{"%s":{"name":"%s"}}}}}' \
      "$model_name" "$OPENAI_API_KEY" "$base" "$OPENCODE_MAX" "$model_name" "$model_name"
  else
    printf '{"permission":{"question":"deny","external_directory":"allow"},"small_model":"%s","provider":{"azure":{"options":{"maxTokens":%s}},"openai":{"options":{"maxTokens":%s}}}}' \
      "$OPENCODE_MODEL" "$OPENCODE_MAX" "$OPENCODE_MAX"
  fi
}

# ============================================================
# Single-phase direct implementation run
# ============================================================
PHASE_STDERR="$LOG_DIR/phase1_stderr.txt"
OPENCODE_CFG="$(_build_opencode_cfg)"

set +e
_PHASE_T0=$SECONDS
(cd "$REPO_DIR" && OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" \
  timeout "${OPENCODE_PHASE_TIMEOUT:-3600}" opencode run \
  --format json \
  --model "$OPENCODE_MODEL" \
  "$EXEC_PROMPT_CONTENT") 2>"$PHASE_STDERR" > "$PHASE_NDJSON"
PHASE_EXIT=$?
PHASE_WALL=$(( SECONDS - _PHASE_T0 ))
echo "[agent] Phase wall time: ${PHASE_WALL}s"
set -e

if [[ "$PHASE_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: opencode returned non-zero ($PHASE_EXIT), continuing to capture diff"
fi

# Remove context files written to repo before capturing diff
rm -f "$REPO_DIR/init_cwv.json"

git diff > "$PATCH_FILE"
git reset --hard HEAD
git clean -fd
rm -f "$EXEC_PROMPT"

echo "[agent] Done"
