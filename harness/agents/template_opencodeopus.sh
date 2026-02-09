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

echo "[agent] Two-phase CWV agent (opencode)" > "$LOG_FILE"

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
#   azure/gpt-5                - Azure OpenAI (openai.azure.com)
#   azure-cognitive-services/claude-opus-4-5 - Azure Cognitive Services (cognitiveservices.azure.com, Foundry)
#   anthropic/claude-opus-4-5  - Anthropic Claude (ANTHROPIC_API_KEY from console.anthropic.com only)
if [[ -n "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_FOUNDRY_API_KEY:-}" ]]; then
  # Standard Anthropic API key (from console.anthropic.com) - use Anthropic provider
  ANTHROPIC_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-claude-opus-4-5}"
  OPENCODE_MODEL="${OPENCODE_MODEL:-anthropic/$ANTHROPIC_MODEL}"
elif [[ -n "${ANTHROPIC_FOUNDRY_API_KEY:-}" && -n "${ANTHROPIC_FOUNDRY_RESOURCE:-}" ]]; then
  # Anthropic Foundry: use anthropic provider with baseURL (avoids OpenCode sdk.responses bug with Azure provider)
  # Endpoint: https://{resource}.services.ai.azure.com/anthropic/v1/ per docs.claude.com
  AZURE_DEPLOY="${ANTHROPIC_DEFAULT_OPUS_MODEL:-claude-opus-4-5}"
  OPENCODE_MODEL="${OPENCODE_MODEL:-anthropic/$AZURE_DEPLOY}"
  export ANTHROPIC_API_KEY="${ANTHROPIC_FOUNDRY_API_KEY}"
  ANTHROPIC_BASE="${ANTHROPIC_FOUNDRY_BASE_URL:-https://${ANTHROPIC_FOUNDRY_RESOURCE}.services.ai.azure.com/anthropic/v1}"
  export ANTHROPIC_BASE_URL="$ANTHROPIC_BASE"
elif [[ -n "${ANTHROPIC_FOUNDRY_API_KEY:-}" && -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  # Fallback: Azure OpenAI (openai.azure.com) - may hit sdk.responses bug with Claude
  AZURE_DEPLOY="${ANTHROPIC_DEFAULT_OPUS_MODEL:-claude-opus-4-5}"
  OPENCODE_MODEL="${OPENCODE_MODEL:-azure/$AZURE_DEPLOY}"
  export AZURE_OPENAI_API_KEY="${ANTHROPIC_FOUNDRY_API_KEY}"
  if [[ -z "${AZURE_RESOURCE_NAME:-}" && -n "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
    AZURE_RESOURCE_NAME="${AZURE_OPENAI_ENDPOINT#*://}"
    AZURE_RESOURCE_NAME="${AZURE_RESOURCE_NAME%%.*}"
    export AZURE_RESOURCE_NAME
  fi
elif [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
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

# Prepend OpenCode install path (venv can override PATH; opencode installs to ~/.opencode/bin)
OPENCODE_BIN="${OPENCODE_PATH:-$HOME/.opencode/bin}"
[[ -d "$OPENCODE_BIN" ]] && export PATH="$OPENCODE_BIN:$PATH"
if ! command -v opencode &>/dev/null; then
  echo "[agent] ERROR: opencode CLI not found. Install: curl -fsSL https://opencode.ai/install | bash" >> "$LOG_FILE"
  echo "[agent] Tip: If using venv, set OPENCODE_PATH or ensure ~/.local/bin is in PATH before venv activate" >> "$LOG_FILE"
  exit 1
fi

echo "OPENCODE_MODEL: $OPENCODE_MODEL" >> "$LOG_FILE"

# Auth: run `opencode auth login` to configure providers. Common env vars:
#   anthropic/*: ANTHROPIC_API_KEY
#   azure-cognitive-services/*: AZURE_OPENAI_API_KEY, AZURE_COGNITIVE_SERVICES_RESOURCE_NAME (Foundry)
#   azure/*: AZURE_OPENAI_API_KEY, AZURE_RESOURCE_NAME
#   openai/*: OPENAI_API_KEY
#   openrouter/*: OPENROUTER_API_KEY

# OpenCode config: permission + reasoning effort + max output tokens (matches Codex)
# Override via OPENCODE_REASONING_EFFORT (default: medium) or OPENCODE_MAX_TOKENS (default: 50000)
OPENCODE_REASONING="${OPENCODE_REASONING_EFFORT:-medium}"
OPENCODE_MAX="${OPENCODE_MAX_TOKENS:-50000}"
# Deny question tool: in non-interactive mode it blocks indefinitely (opencode run has no TUI to answer)

# Build provider config: anthropic when using Anthropic, azure-cognitive-services for Foundry, else azure+openai
# small_model stays GPT-5 for lightweight tasks (session titles, etc.); override via OPENCODE_SMALL_MODEL
SMALL_MODEL="${OPENCODE_SMALL_MODEL:-azure/gpt-5}"
if [[ "$OPENCODE_MODEL" == anthropic/* ]]; then
  # Add baseURL when using Foundry (ANTHROPIC_BASE_URL set)
  ANTHROPIC_OPTS="{\"maxTokens\":$OPENCODE_MAX}"
  if [[ -n "${ANTHROPIC_BASE_URL:-}" ]]; then
    ANTHROPIC_OPTS="{\"maxTokens\":$OPENCODE_MAX,\"baseURL\":\"$ANTHROPIC_BASE_URL\"}"
  fi
  OPENCODE_CFG="{\"permission\":{\"question\":\"deny\"},\"small_model\":\"$SMALL_MODEL\",\"provider\":{\"anthropic\":{\"options\":$ANTHROPIC_OPTS},\"azure\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}},\"openai\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}}}}"
elif [[ "$OPENCODE_MODEL" == azure-cognitive-services/* ]]; then
  OPENCODE_CFG="{\"permission\":{\"question\":\"deny\"},\"small_model\":\"$SMALL_MODEL\",\"provider\":{\"azure-cognitive-services\":{\"options\":{\"maxTokens\":$OPENCODE_MAX}},\"azure\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}},\"openai\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}}}}"
else
  OPENCODE_CFG="{\"permission\":{\"question\":\"deny\"},\"small_model\":\"$SMALL_MODEL\",\"provider\":{\"azure\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}},\"openai\":{\"options\":{\"maxTokens\":$OPENCODE_MAX,\"reasoning\":{\"effort\":\"$OPENCODE_REASONING\"}}}}}"
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
trap 'chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT" "$PHASE1_STDERR"' EXIT

PHASE1_STDERR="$(mktemp)"
(cd "$PHASE1_DIR" && OPENCODE_CONFIG_CONTENT="$OPENCODE_CFG" opencode run \
  --model "$OPENCODE_MODEL" \
  "$(<"$PLAN_PROMPT")") >> "$LOG_FILE" 2>"$PHASE1_STDERR"
PHASE1_EXIT=$?
# -------------------------------------

# plan.md is the only writable file; repo/ was chmod read-only
PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  echo "[agent] Phase 1 exit=$PHASE1_EXIT" >> "$LOG_FILE"
  echo "[agent] Phase 1 stderr:" >> "$LOG_FILE"
  head -c 2000 "$PHASE1_STDERR" | sed 's/^/[phase1_stderr] /' >> "$LOG_FILE"
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
