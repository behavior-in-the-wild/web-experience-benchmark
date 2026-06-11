#!/usr/bin/env bash
set -euo pipefail

# Route all temp files to /dev/shm (overlay /tmp is small; /dev/shm has ~1 TB free)
export TMPDIR="${BENCH_TMPDIR:-/dev/shm}"

REPO_DIR="$1"
# $2 TASK_SPEC unused — evaluate.sh passes it for consistency with other templates
LOG="$3"
PATCH_FILE="${4:-/dev/null}"

# evaluate.sh exports FRAMEWORK; do not read from positional args (only 4 are passed)
FRAMEWORK="${FRAMEWORK:-static_html}"
DEVICE="${DEVICE:-desktop}"
PORT="${PORT:-4000}"
NUM_RUNS="${NUM_RUNS:-3}"

AIDER_MODEL="${AIDER_MODEL:-azure/gpt-5}"
AIDER_MAP_TOKENS="${AIDER_MAP_TOKENS:-512}"

# Write a model settings file so litellm drops unsupported params (e.g. temperature)
# for o-series / Responses API models like gpt-5.1-codex
AIDER_MODEL_SETTINGS_FILE="$(mktemp)"
cat > "$AIDER_MODEL_SETTINGS_FILE" << YAML
- name: "${AIDER_MODEL}"
  use_temperature: false
YAML

# Resolve script directory for finding host_files and cwv_benchmark.py
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AIDER_BIN="${AIDER_BIN:-aider}"
if [[ -x "$SCRIPT_DIR/../../.venv/bin/aider" ]]; then
  AIDER_BIN="$SCRIPT_DIR/../../.venv/bin/aider"
fi

mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"

USAGE_FILE="${LOG%_agent.log}_usage.json"
PLAN_FILE="$REPO_DIR/plan.md"
CWV_JSON="$(mktemp)"
PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"

# Always write usage JSON on exit, regardless of which exit path is taken
_write_usage() {
  python3 - "$LOG" "$USAGE_FILE" << 'PYEOF'
import json, re, sys

log_path, out_path = sys.argv[1], sys.argv[2]

total_sent = 0
total_received = 0
total_cost = 0.0
file_edits = 0

def parse_num(s):
    s = s.strip().replace(',', '')
    if s.lower().endswith('k'):
        return int(float(s[:-1]) * 1000)
    return int(float(s))

try:
    with open(log_path) as f:
        for line in f:
            m = re.search(r'Tokens:\s*([\d,.k]+)\s*sent,\s*([\d,.k]+)\s*received', line, re.I)
            if m:
                total_sent     += parse_num(m.group(1))
                total_received += parse_num(m.group(2))
            m = re.search(r'Cost:\s*\$([\d.]+)\s*message', line, re.I)
            if m:
                total_cost += float(m.group(1))
            if re.search(r'Applied edit to |Wrote |Editing |edited ', line, re.I):
                file_edits += 1
except Exception as e:
    print(f"[aider usage] parse error: {e}", file=sys.stderr)

usage = {
    'cost_usd': round(total_cost, 6),
    'tokens': {
        'input':  total_sent,
        'output': total_received,
        'total':  total_sent + total_received,
    },
    'tool_calls': file_edits,
}
with open(out_path, 'w') as f:
    json.dump(usage, f, indent=2)
print(f"[agent_aider] usage written to {out_path}", file=sys.stderr)
PYEOF
}

_cleanup() {
  _write_usage
  rm -f "$CWV_JSON" "$PLAN_PROMPT" "$EXEC_PROMPT"
}
trap _cleanup EXIT

AIDER_BASE_ARGS=(
  --yes-always
  --no-auto-commits
  --no-pretty
  --no-stream
  --no-show-model-warnings
  --no-suggest-shell-commands
  --no-detect-urls
  --no-gitignore
  --no-gui
  --no-browser
  --map-tokens "$AIDER_MAP_TOKENS"
  --map-refresh manual
  --model "$AIDER_MODEL"
  --editor-model "$AIDER_MODEL"
)

_write_cwv_json() {
  # evaluate.sh writes CWV_ENV_FILE with base64-encoded values to avoid ARG_MAX limits.
  if [[ -n "${CWV_ENV_FILE:-}" && -f "$CWV_ENV_FILE" ]]; then
    while IFS='=' read -r _cwv_key _cwv_b64; do
      [[ -n "$_cwv_key" ]] || continue
      printf -v "$_cwv_key" '%s' "$(printf '%s' "$_cwv_b64" | base64 -d 2>/dev/null || true)"
    done < "$CWV_ENV_FILE"
  fi

  local cwv_mobile="${CWV_BASELINE_MOBILE:-}"
  local cwv_desktop="${CWV_BASELINE_DESKTOP:-}"
  local lcp_mobile="${LCP_ENTRIES_MOBILE:-}"
  local lcp_desktop="${LCP_ENTRIES_DESKTOP:-}"
  local cls_shifts_m="${CLS_SHIFTS_MOBILE:-}"
  local cls_shifts_d="${CLS_SHIFTS_DESKTOP:-}"
  local inp_m="${INP_INTERACTIONS_MOBILE:-}"
  local inp_d="${INP_INTERACTIONS_DESKTOP:-}"

  [[ "$cwv_mobile" == " " || -z "$cwv_mobile" ]] && cwv_mobile="null"
  [[ "$cwv_desktop" == " " || -z "$cwv_desktop" ]] && cwv_desktop="null"
  [[ "$lcp_mobile" == " " || -z "$lcp_mobile" ]] && lcp_mobile="null"
  [[ "$lcp_desktop" == " " || -z "$lcp_desktop" ]] && lcp_desktop="null"
  [[ "$cls_shifts_m" == " " || -z "$cls_shifts_m" ]] && cls_shifts_m="null"
  [[ "$cls_shifts_d" == " " || -z "$cls_shifts_d" ]] && cls_shifts_d="null"
  [[ "$inp_m" == " " || -z "$inp_m" ]] && inp_m="null"
  [[ "$inp_d" == " " || -z "$inp_d" ]] && inp_d="null"

  printf '{"mobile":%s,"desktop":%s,"lcp_entries_mobile":%s,"lcp_entries_desktop":%s,"cls_shifts_mobile":%s,"cls_shifts_desktop":%s,"inp_interactions_mobile":%s,"inp_interactions_desktop":%s}\n' \
    "$cwv_mobile" "$cwv_desktop" "$lcp_mobile" "$lcp_desktop" \
    "$cls_shifts_m" "$cls_shifts_d" "$inp_m" "$inp_d" > "$CWV_JSON"
}

_reset_repo() {
  git reset --hard HEAD 2>/dev/null || true
  git clean -fd
  rm -f .aider* 2>/dev/null || true
  rm -rf .aider.tags.cache* 2>/dev/null || true
}

_capture_patch() {
  rm -f "$PLAN_FILE"
  # Only capture edits to files that were already tracked before the agent ran.
  # Do NOT use git add -A — that would include aider's internal cache files
  # (.aider.chat.history.md, .aider.tags.cache.v4/*) and any placeholder stub
  # files the model created for paths it hallucinated.
  # git diff (unstaged) shows modifications to tracked files only.
  git diff -- ':!.aider*' ':!plan.md' > "$PATCH_FILE" 2>/dev/null || true
}

_sanitize_phase1() {
  local plan_content=""
  [[ -s "$PLAN_FILE" ]] && plan_content="$(cat "$PLAN_FILE")"

  local extra
  extra="$(
    {
      git diff --name-only 2>/dev/null || true
      git ls-files --others --exclude-standard 2>/dev/null || true
    } | grep -v '^plan\.md$' | sort -u
  )"
  if [[ -n "$extra" ]]; then
    echo "[agent_aider] WARN: Phase 1 touched extra files; discarding non-plan changes:" >> "$LOG"
    echo "$extra" >> "$LOG"
  fi

  _reset_repo
  if [[ -n "$plan_content" ]]; then
    printf '%s' "$plan_content" > "$PLAN_FILE"
  fi
}

# Ensure clean state
_reset_repo

echo "[agent_aider] Starting aider agent" > "$LOG"
echo "[agent_aider] FRAMEWORK=$FRAMEWORK PORT=$PORT DEVICE=$DEVICE NUM_RUNS=$NUM_RUNS" >> "$LOG"
echo "[agent_aider] AIDER_BIN=$AIDER_BIN MODEL=$AIDER_MODEL MAP_TOKENS=$AIDER_MAP_TOKENS" >> "$LOG"

_write_cwv_json
if [[ -s "$CWV_JSON" && "$(cat "$CWV_JSON")" != *'"mobile":null,"desktop":null'* ]]; then
  echo "[agent_aider] CWV baseline loaded from CWV_ENV_FILE ($(wc -c < "$CWV_JSON") bytes)" >> "$LOG"
else
  echo "[agent_aider] WARN: CWV baseline empty or all-null" >> "$LOG"
fi

echo "[agent_aider] Starting Phase 1: Planning" >> "$LOG"
touch "$PLAN_FILE"
echo "[agent_aider] Pre-created plan.md (touch)" >> "$LOG"

cat <<EOF > "$PLAN_PROMPT"
You are a web performance analyst. Create a performance optimization plan.

=== CONTEXT ===
Framework: $FRAMEWORK
Device: $DEVICE
===============

The CWV baseline JSON is already attached as a read-only file. Use it to inform your plan.
Do NOT ask for additional files. Do NOT create new files. Do NOT edit repository source files.

YOUR TASK:
Analyze this repository using the repo map, then fill in the existing plan.md file with a detailed performance optimization plan.

The plan.md file must include:

1. **Baseline Analysis**: Summarize the current CWV metrics (from the attached JSON) without copying raw scores
2. **Files to Modify**: List all files that need changes (full paths)
3. **Proposed Changes**: For each file, describe in plain English:
   - Which function/section needs changes
   - What the change should accomplish
   - Why it will improve performance (consider the $FRAMEWORK framework specifics)
4. **Expected Impact**: Estimated improvements to FCP, LCP, CLS, INP for $DEVICE

IMPORTANT:
- Edit ONLY plan.md
- Do NOT create cwv-baseline.json or any other new files
- Do NOT modify HTML, CSS, JS, or config files in Phase 1
- This is a planning document only — implementation happens in Phase 2

You MUST write plan.md regardless of metric values — always find improvements to make.
EOF

PLAN_MIN_BYTES="${PLAN_MIN_BYTES:-500}"

echo "[agent_aider] Phase 1: Generating plan (read repo map, fill plan.md)..." >> "$LOG"

PHASE1_READ=(--read "$CWV_JSON")
echo "[agent_aider] Phase 1: Passing CWV JSON as read file: $CWV_JSON" >> "$LOG"

set +e
"$AIDER_BIN" \
  "${AIDER_BASE_ARGS[@]}" \
  --edit-format whole \
  --subtree-only \
  --file "$PLAN_FILE" \
  "${PHASE1_READ[@]}" \
  --message-file "$PLAN_PROMPT" \
  >> "$LOG" 2>&1
PHASE1_EXIT=$?
set -e

# Always sanitize first — rescues plan.md content written before any token-overflow exit
_sanitize_phase1

if [[ "$PHASE1_EXIT" -ne 0 ]]; then
  if [[ ! -s "$PLAN_FILE" ]]; then
    echo "[agent_aider] Phase 1 failed (exit=$PHASE1_EXIT) and plan.md empty, aborting" >> "$LOG"
    _reset_repo
    exit 0
  fi
  echo "[agent_aider] Phase 1 exit=$PHASE1_EXIT but plan.md recovered ($(wc -c < "$PLAN_FILE") bytes), continuing" >> "$LOG"
fi

if [[ ! -s "$PLAN_FILE" ]]; then
  echo "[agent_aider] plan.md was not created or is empty, aborting" >> "$LOG"
  _reset_repo
  exit 0
fi

PLAN_BYTES=$(wc -c < "$PLAN_FILE")
if [[ "$PLAN_BYTES" -lt "$PLAN_MIN_BYTES" ]]; then
  echo "[agent_aider] plan.md too small (${PLAN_BYTES} bytes < ${PLAN_MIN_BYTES}), aborting" >> "$LOG"
  _reset_repo
  exit 0
fi

PLAN_CONTENT="$(cat "$PLAN_FILE")"
echo "[agent_aider] Phase 1 complete. Plan saved to plan.md" >> "$LOG"
echo "[agent_aider] Plan size: $(echo "$PLAN_CONTENT" | wc -c) bytes, $(echo "$PLAN_CONTENT" | wc -l) lines" >> "$LOG"

_reset_repo

cat <<EOF > "$EXEC_PROMPT"
You are an expert web performance engineer.

=== CONTEXT ===
Framework: $FRAMEWORK
Device: $DEVICE
===============

Execute the implementation plan below. Make concrete edits to existing repository files.

Rules:
- Do not change visible content
- Do not remove pages
- Do not add build systems
- Only edit existing files
- Apply $FRAMEWORK-specific best practices
- Optimize for both mobile and desktop where applicable
- Do NOT edit or recreate plan.md

=== YOUR PLAN ===
$PLAN_CONTENT
=================

Implement all changes described in the plan. Apply the edits directly to the codebase.
Do not ask the user questions; proceed autonomously with your best judgment.
EOF

if [[ -n "${EVAL_SUGGESTION_FILE:-}" && -f "$EVAL_SUGGESTION_FILE" ]]; then
  {
    echo ""
    echo "### Benchmark harness: external suggestion (same as planning phase)"
    echo '```json'
    cat "$EVAL_SUGGESTION_FILE"
    echo '```'
    echo ""
    echo "Implement changes that satisfy this suggestion; prefer it when it overlaps with the plan."
  } >> "$EXEC_PROMPT"
fi

echo "[agent_aider] Phase 2: Executing plan..." >> "$LOG"

# Extract file paths from plan.md that actually exist in the repo and pass them
# as --file args so the model sees real content rather than creating placeholders.
PHASE2_FILE_ARGS=()
while IFS= read -r candidate; do
  # Strip leading ./  and whitespace
  candidate="${candidate#./}"
  candidate="${candidate#- }"
  candidate="$(echo "$candidate" | sed 's/^[[:space:]]*//')"
  [[ -z "$candidate" ]] && continue
  if [[ -f "$REPO_DIR/$candidate" ]]; then
    PHASE2_FILE_ARGS+=(--file "$REPO_DIR/$candidate")
  fi
done < <(python3 -c "
import re, sys
with open('$PLAN_FILE') as f:
    content = f.read()
# Match lines that look like file paths (contain a . extension, start with ./ or a word char)
for line in content.splitlines():
    line = line.strip().lstrip('- *#').strip()
    # Must have a file extension and look like a path (not a URL)
    if re.match(r'^\.?[a-zA-Z0-9_./\\\\-]+\.[a-zA-Z0-9]{1,6}$', line) and 'http' not in line:
        print(line)
" 2>/dev/null || true)

echo "[agent_aider] Phase 2: found ${#PHASE2_FILE_ARGS[@]} existing files from plan to pass to aider" >> "$LOG"

set +e
"$AIDER_BIN" \
  "${AIDER_BASE_ARGS[@]}" \
  --edit-format diff \
  "${PHASE2_FILE_ARGS[@]}" \
  --message-file "$EXEC_PROMPT" \
  >> "$LOG" 2>&1
PHASE2_EXIT=$?
set -e

if [[ "$PHASE2_EXIT" -eq 0 ]]; then
  _capture_patch
  PATCH_LINES=$(wc -l < "$PATCH_FILE" 2>/dev/null || echo 0)
  echo "[agent_aider] Phase 2 complete. Patch lines=$PATCH_LINES" >> "$LOG"
else
  echo "[agent_aider] Phase 2 failed (exit=$PHASE2_EXIT); capturing partial diff if any" >> "$LOG"
  _capture_patch
fi

_reset_repo
echo "[agent_aider] Done" >> "$LOG"
