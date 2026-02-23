#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Common agent template (Claude variant)
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

echo "[agent] Two-phase CWV agent (claude)" > "$LOG_FILE"

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
# evaluate.sh exports: CWV_BASELINE_MOBILE, CWV_BASELINE_DESKTOP, LCP_ENTRIES_MOBILE, LCP_ENTRIES_DESKTOP,
#                       CLS_SHIFTS_MOBILE, CLS_SHIFTS_DESKTOP, INP_INTERACTIONS_MOBILE, INP_INTERACTIONS_DESKTOP
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

# plan.md is the only writable file in the workspace (same as Codex)
touch "$PHASE1_DIR/plan.md"

# -------------------------
# Model config
# -------------------------
CLAUDE_MODEL="${CLAUDE_MODEL:-sonnet}"
export CLAUDE_CODE_EFFORT_LEVEL="${CLAUDE_CODE_EFFORT_LEVEL:-medium}"
export CLAUDE_MAX_TOKENS="${CLAUDE_MAX_TOKENS:-50000}"

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "[agent] WARNING: ANTHROPIC_API_KEY not set; relying on existing claude login" >> "$LOG_FILE"
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
EOF

cp "$PLAN_PROMPT" "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase1_prompt.txt"
echo "[agent] DEBUG: LOG_DIR=$LOG_DIR, saved Phase 1 prompt" >> "$LOG_FILE"

# -------- CLAUDE CALL (PHASE 1) — repo read-only, plan.md writable (same as Codex) --------
echo "[agent] DEBUG: Starting Phase 1 (planning)..." >> "$LOG_FILE"
trap 'chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR"; rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"' EXIT

# -p --> headless and non-interactive mode


PHASE1_START=$(date +%s)
(cd "$PHASE1_DIR" && claude -p \
  --model "$CLAUDE_MODEL" \
  --dangerously-skip-permissions \
  --output-format text \
  --verbose \
  --no-session-persistence \
  "$(<"$PLAN_PROMPT")") \
  >> "$LOG_FILE" 2>&1
PHASE1_END=$(date +%s)
echo "[agent] DEBUG: Phase 1 complete, duration=$((PHASE1_END - PHASE1_START))s" >> "$LOG_FILE"
# -------------------------------------

# plan.md is the only writable file; repo/ was chmod read-only
PLAN_COPY="$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_plan.md"

if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  touch "$PLAN_COPY"
  exit 0
fi

PLAN_SIZE=$(wc -c < "$PHASE1_DIR/plan.md")
echo "[agent] DEBUG: Phase 1 complete, plan.md size=$PLAN_SIZE bytes" >> "$LOG_FILE"

# Copy plan to REPO_DIR so Phase 2 can read it (avoids passing huge prompt as CLI arg)
cp "$PHASE1_DIR/plan.md" "$REPO_DIR/plan.md"
echo "[agent] DEBUG: Copied plan.md to REPO_DIR=$REPO_DIR" >> "$LOG_FILE"
[[ -s "$REPO_DIR/plan.md" ]] || { echo "[agent] ERROR: plan.md copy failed or empty" >> "$LOG_FILE"; exit 1; }

# Copy plan.md to results folder for post-analysis
cp "$REPO_DIR/plan.md" "$PLAN_COPY"
echo "[agent] DEBUG: Copied plan.md to results: $PLAN_COPY" >> "$LOG_FILE"

echo "[agent] DEBUG: Phase 2 will read plan.md from cwd=$REPO_DIR (plan.md exists, $(wc -l < "$REPO_DIR/plan.md") lines)" >> "$LOG_FILE"

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
} > "$EXEC_PROMPT"

EXEC_PROMPT_SIZE=$(wc -c < "$EXEC_PROMPT")
EXEC_PROMPT_CONTENT="$(cat "$EXEC_PROMPT")"

printf "%s" "$EXEC_PROMPT_CONTENT" > "$LOG_DIR/$(basename "$LOG_FILE" _agent.log)_phase2_prompt.txt"

echo "[agent] DEBUG: EXEC_PROMPT size=$EXEC_PROMPT_SIZE bytes" >> "$LOG_FILE"
echo "[agent] DEBUG: REPO_DIR=$REPO_DIR plan.md exists=$( [[ -f "$REPO_DIR/plan.md" ]] && echo yes || echo no )" >> "$LOG_FILE"
echo "[agent] DEBUG: Starting Phase 2 (execution)..." >> "$LOG_FILE"

# -------- CLAUDE CALL (PHASE 2) — full autonomy --------
set +e
PHASE2_START=$(date +%s)
claude -p \
  --model "$CLAUDE_MODEL" \
  --dangerously-skip-permissions \
  --output-format text \
  --verbose \
  --no-session-persistence \
  "$EXEC_PROMPT_CONTENT" \
  >> "$LOG_FILE" 2>&1
PHASE2_EXIT=$?
set -e

PHASE2_END=$(date +%s)
echo "[agent] DEBUG: Phase 2 exit code=$PHASE2_EXIT, duration=$((PHASE2_END - PHASE2_START))s" >> "$LOG_FILE"

if [[ "$PHASE2_EXIT" -ne 0 ]]; then
  echo "[agent] WARN: Phase 2 claude returned non-zero ($PHASE2_EXIT), continuing to capture diff" >> "$LOG_FILE"
fi

# -------------------------------------

# Remove plan.md from repo before capturing diff (planning artifact, not a code change)
# plan.md copy remains in LOG_DIR for post-analysis
echo "[agent] DEBUG: Removing plan.md from REPO_DIR before git diff" >> "$LOG_FILE"
rm -f "$REPO_DIR/plan.md"

git diff > "$PATCH_FILE"
PATCH_LINES=$(wc -l < "$PATCH_FILE")
echo "[agent] DEBUG: git diff captured, patch lines=$PATCH_LINES" >> "$LOG_FILE"
git reset --hard HEAD
git clean -fd
rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"

echo "[agent] Done" >> "$LOG_FILE"
echo "[agent] DEBUG: Results in $LOG_DIR: $(basename "$LOG_FILE") ($(wc -c < "$LOG_FILE") bytes), $(basename "$PLAN_COPY") ($(wc -c < "$PLAN_COPY") bytes)" >> "$LOG_FILE"
