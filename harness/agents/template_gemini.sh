#!/usr/bin/env bash
set -euo pipefail

# Route all temp files to /dev/shm (overlay /tmp is small; /dev/shm has ~1 TB free)
export TMPDIR="${BENCH_TMPDIR:-/dev/shm}"

# ============================================================
# Common agent template (OpenCode + Gemini via Vertex AI)
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

echo "[agent] Two-phase CWV agent (opencode + Gemini)" > "$LOG_FILE"

# -------------------------
# Google Vertex AI Authentication
# -------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GEMINI_KEY_FILE="${GEMINI_KEY_FILE:-$SCRIPT_DIR/gemini_key.json}"

if [[ ! -f "$GEMINI_KEY_FILE" ]]; then
  echo "[agent] ERROR: gemini_key.json not found at $GEMINI_KEY_FILE" >> "$LOG_FILE"
  exit 1
fi

export GOOGLE_APPLICATION_CREDENTIALS="$GEMINI_KEY_FILE"
export GOOGLE_CLOUD_PROJECT="adbe-gcp0792"
export VERTEX_LOCATION="${VERTEX_LOCATION:-us-central1}"

# Obtain a short-lived OAuth2 access token for Vertex AI via google-auth.
# opencode uses the openai provider pointed at Vertex's OpenAI-compatible endpoint,
# so the token is passed as the OPENAI_API_KEY Bearer credential.
VERTEX_TOKEN=$(python3 - <<PY
from google.oauth2 import service_account
import google.auth.transport.requests, sys
try:
    creds = service_account.Credentials.from_service_account_file(
        "$GEMINI_KEY_FILE",
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    print(creds.token)
except Exception as e:
    print("ERROR:" + str(e), file=sys.stderr)
    sys.exit(1)
PY
)

if [[ -z "$VERTEX_TOKEN" || "$VERTEX_TOKEN" == ERROR* ]]; then
  echo "[agent] ERROR: Failed to obtain Vertex AI access token" >> "$LOG_FILE"
  exit 1
fi

# Route opencode's openai provider to Vertex AI's OpenAI-compatible endpoint
export OPENAI_API_KEY="$VERTEX_TOKEN"
export OPENAI_BASE_URL="https://${VERTEX_LOCATION}-aiplatform.googleapis.com/v1beta1/projects/${GOOGLE_CLOUD_PROJECT}/locations/${VERTEX_LOCATION}/endpoints/openapi"

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

# Write opencode.json into PHASE1_DIR so opencode recognises it as the project root.
# Without this, opencode falls back to HOME as root and rejects writes/reads in /tmp as "external".
cat > "$PHASE1_DIR/opencode.json" <<'OCJSON'
{
  "permission": { "question": "deny" },
  "autoupdate": false,
  "snapshot": false
}
OCJSON

# -------------------------
# Model config (opencode → openai provider → Vertex AI OpenAI-compatible endpoint)
# -------------------------
# Model format: openai/google/<gemini-model-name>
# opencode strips the "openai/" prefix and forwards the rest to OPENAI_BASE_URL.
# Vertex AI model names: google/gemini-2.5-flash, google/gemini-2.0-flash, etc.
OPENCODE_MODEL="${OPENCODE_MODEL:-vertex/gemini-2.5-flash}"

if ! command -v opencode &>/dev/null; then
  echo "[agent] ERROR: opencode CLI not found. Install: curl -fsSL https://opencode.ai/install | bash" >> "$LOG_FILE"
  exit 1
fi

# OpenCode config: permission + max output tokens
# Override via OPENCODE_MAX_TOKENS (default: 50000)
OPENCODE_MAX="${OPENCODE_MAX_TOKENS:-50000}"
# Whitelist the Gemini model ID on the openai provider so opencode doesn't reject it,
# and point the provider's baseURL at Vertex AI's OpenAI-compatible endpoint.
# Deny question tool: in non-interactive mode it blocks indefinitely (opencode run has no TUI to answer)
OPENCODE_CFG=$(python3 -c "
import json, sys
cfg = {
  'permission': {'question': 'deny', 'write': 'allow', 'bash': 'allow'},
  'small_model': '$OPENCODE_MODEL',
  'provider': {
    'vertex': {
      'api': 'openai',
      'options': {
        'apiKey': '$VERTEX_TOKEN',
        'baseURL': '$OPENAI_BASE_URL'
      },
      'models': {
        'gemini-2.5-pro': {
          'id': 'google/gemini-2.5-pro',
          'name': 'Gemini 2.5 Pro',
          'tool_call': True,
          'temperature': True,
          'attachment': False,
          'reasoning': False
        },
        'gemini-2.5-flash': {
          'id': 'google/gemini-2.5-flash',
          'name': 'Gemini 2.5 Flash',
          'tool_call': True,
          'temperature': True,
          'attachment': False,
          'reasoning': False
        },
        'gemini-2.5-flash-lite': {
          'id': 'google/gemini-2.5-flash-lite',
          'name': 'Gemini 2.5 Flash Lite',
          'tool_call': True,
          'temperature': True,
          'attachment': False,
          'reasoning': False
        }
      }
    }
  }
}
print(json.dumps(cfg))
")

echo "OPENCODE_MODEL: $OPENCODE_MODEL" >> "$LOG_FILE"
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

cp "$PLAN_PROMPT" "$LOG_DIR/phase1_prompt.txt"

# -------- OPENCODE RUN (PHASE 1) — matches Codex/Claude: workspace=PHASE1_DIR, repo read-only, plan.md writable --------
# Note: OpenCode may output the plan to stdout instead of editing plan.md; we extract it as fallback.
# Discard stderr (opencode logs) so plan extraction and log stay clean.
PHASE1_ERR="$(mktemp)"
OPENCODE_DATA_DIR="$(mktemp -d)"
trap 'chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR" "$OPENCODE_DATA_DIR"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT" "$PHASE1_OUTPUT" "$PHASE1_ERR"' EXIT

PHASE1_OUTPUT="$(mktemp)"
# Retry up to 3 times with backoff — handles transient rate-limit rejections.
PHASE1_EXIT=1
for _p1_attempt in 1 2 3; do
  if [[ $_p1_attempt -gt 1 ]]; then
    _p1_wait=$(( (_p1_attempt - 1) * 30 ))
    echo "[agent] Phase 1 retry $_p1_attempt after ${_p1_wait}s (previous attempt produced no output)" >> "$LOG_FILE"
    sleep "$_p1_wait"
  fi
  : > "$PHASE1_OUTPUT"
  (cd "$PHASE1_DIR" && XDG_DATA_HOME="$OPENCODE_DATA_DIR" OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
    --model "$OPENCODE_MODEL" \
    "$(<"$PLAN_PROMPT")") > "$PHASE1_OUTPUT" 2>> "$LOG_FILE"
  PHASE1_EXIT=$?
  [[ -s "$PHASE1_OUTPUT" ]] || [[ -s "$PHASE1_DIR/plan.md" ]] && break
  echo "[agent] Phase 1 attempt $_p1_attempt: OpenCode produced no output (exit=$PHASE1_EXIT)" >> "$LOG_FILE"
done
# -------------------------------------

# plan.md is the only writable file; repo/ was chmod read-only
PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  # OpenCode often outputs the plan to stdout instead of editing plan.md.
  # Try to extract starting from the required header; fall back to all stdout content.
  if [[ -s "$PHASE1_OUTPUT" ]]; then
    if grep -q '## Performance Issues Identified' "$PHASE1_OUTPUT"; then
      sed -n '/## Performance Issues Identified/,$p' "$PHASE1_OUTPUT" \
        | sed 's/\x1b\[[0-9;]*m//g' | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' \
        > "$PHASE1_DIR/plan.md"
    else
      # Fallback: strip ANSI and use all stdout as plan content
      sed 's/\x1b\[[0-9;]*m//g' "$PHASE1_OUTPUT" \
        | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' \
        > "$PHASE1_DIR/plan.md"
    fi
  fi
fi

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  if [[ -s "$PHASE1_ERR" ]]; then
    echo "[agent] OpenCode stderr:" >> "$LOG_FILE"
    cat "$PHASE1_ERR" >> "$LOG_FILE"
  fi
  touch "$PLAN_COPY"
  exit 1
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

printf "%s" "$EXEC_PROMPT_CONTENT" > "$LOG_DIR/phase2_prompt.txt"

set +e
PHASE2_EXIT=1
for _p2_attempt in 1 2; do
  if [[ $_p2_attempt -gt 1 ]]; then
    echo "[agent] Phase 2 retry $_p2_attempt after 30s" >> "$LOG_FILE"
    sleep 30
  fi
  (cd "$REPO_DIR" && XDG_DATA_HOME="$OPENCODE_DATA_DIR" OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
    --model "$OPENCODE_MODEL" \
    "$EXEC_PROMPT_CONTENT") 2>> "$LOG_FILE"
  PHASE2_EXIT=$?
  [[ $PHASE2_EXIT -eq 0 ]] && break
done
set -e

if [[ "$PHASE2_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: Phase 2 opencode returned non-zero ($PHASE2_EXIT), continuing to capture diff" >> "$LOG_FILE"
fi

# -------------------------------------

# Remove plan.md from repo before capturing diff (planning artifact, not a code change)
rm -f "$REPO_DIR/plan.md"

git diff > "$PATCH_FILE"
git reset --hard HEAD
git clean -fd
rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"

echo "[agent] Done" >> "$LOG_FILE"
