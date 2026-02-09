#!/usr/bin/env bash
set -euo pipefail

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
# evaluate.sh exports: CWV_BASELINE_MOBILE, CWV_BASELINE_DESKTOP, LCP_ENTRIES_MOBILE, LCP_ENTRIES_DESKTOP
CWV_MOBILE="${CWV_BASELINE_MOBILE:-}"
CWV_DESKTOP="${CWV_BASELINE_DESKTOP:-}"
LCP_MOBILE="${LCP_ENTRIES_MOBILE:-}"
LCP_DESKTOP="${LCP_ENTRIES_DESKTOP:-}"
# Use null for empty (evaluate.sh uses " " as placeholder for empty CSV cells)
[[ "$CWV_MOBILE" == " " || -z "$CWV_MOBILE" ]] && CWV_MOBILE="null"
[[ "$CWV_DESKTOP" == " " || -z "$CWV_DESKTOP" ]] && CWV_DESKTOP="null"
[[ "$LCP_MOBILE" == " " || -z "$LCP_MOBILE" ]] && LCP_MOBILE="null"
[[ "$LCP_DESKTOP" == " " || -z "$LCP_DESKTOP" ]] && LCP_DESKTOP="null"
printf '{"mobile":%s,"desktop":%s,"lcp_entries_mobile":%s,"lcp_entries_desktop":%s}\n' \
  "$CWV_MOBILE" "$CWV_DESKTOP" "$LCP_MOBILE" "$LCP_DESKTOP" > "$PHASE1_DIR/repo/init_cwv.json"

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
  AZURE_DEPLOY="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-4.1}"
  OPENCODE_MODEL="${OPENCODE_MODEL:-azure/$AZURE_DEPLOY}"
  # OpenCode requires AZURE_RESOURCE_NAME; derive from AZURE_OPENAI_ENDPOINT if unset
  if [[ -z "${AZURE_RESOURCE_NAME:-}" && -n "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
    # e.g. https://myresource.openai.azure.com -> myresource
    AZURE_RESOURCE_NAME="${AZURE_OPENAI_ENDPOINT#*://}"
    AZURE_RESOURCE_NAME="${AZURE_RESOURCE_NAME%%.*}"
    export AZURE_RESOURCE_NAME
  fi
else
  OPENCODE_MODEL="${OPENCODE_MODEL:-openai/gpt-4.1}"
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
- repo/init_cwv.json: Contains full CWV data (scores + lcp_entries for mobile and desktop)
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

cp "$PLAN_PROMPT" "$LOG_DIR/phase1_prompt.txt"

# -------- OPENCODE RUN (PHASE 1) — matches Codex/Claude: workspace=PHASE1_DIR, repo read-only, plan.md writable --------
# Note: OpenCode may output the plan to stdout instead of editing plan.md; we extract it as fallback.
# Capture stderr for debugging when Phase 1 fails.
trap 'chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT" "$PHASE1_OUTPUT"' EXIT

PHASE1_OUTPUT="$(mktemp)"
PHASE1_STDERR="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_stderr.txt"
(cd "$PHASE1_DIR" && OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
  --model "$OPENCODE_MODEL" \
  "$(<"$PLAN_PROMPT")") 2>"$PHASE1_STDERR" > "$PHASE1_OUTPUT"
PHASE1_EXIT=$?
# -------------------------------------

# plan.md is the only writable file; repo/ was chmod read-only
PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  # OpenCode often outputs the plan to stdout instead of editing plan.md; extract it.
  if grep -q '## Performance Issues Identified' "$PHASE1_OUTPUT"; then
    sed -n '/## Performance Issues Identified/,$p' "$PHASE1_OUTPUT" | sed 's/\x1b\[[0-9;]*m//g' | sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' > "$PHASE1_DIR/plan.md"
  fi
fi

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  echo "[agent] Phase 1 exit code: $PHASE1_EXIT" >> "$LOG_FILE"
  cp "$PHASE1_OUTPUT" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_stdout.txt"
  echo "[agent] Debug: Phase 1 stdout saved to *_phase1_stdout.txt, stderr to *_phase1_stderr.txt" >> "$LOG_FILE"
  if [[ -s "$PHASE1_STDERR" ]]; then
    echo "[agent] === Phase 1 stderr (last 50 lines) ===" >> "$LOG_FILE"
    tail -50 "$PHASE1_STDERR" >> "$LOG_FILE"
    echo "[agent] === end stderr ===" >> "$LOG_FILE"
  fi
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

EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"

printf "%s" "$EXEC_PROMPT_CONTENT" > "$LOG_DIR/phase2_prompt.txt"

set +e
(cd "$REPO_DIR" && OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
  --model "$OPENCODE_MODEL" \
  "$EXEC_PROMPT_CONTENT") 2>/dev/null
PHASE2_EXIT=$?
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
