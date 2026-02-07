#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$1"
LOG="$3"
PATCH_FILE="${4:-/dev/null}"  # optional; evaluate.sh passes it
FRAMEWORK="${5:-static_html}"  # jekyll, hugo, static_html, next, react, vue, etc.
PORT="${6:-4000}"
DEVICE="${7:-desktop}"
NUM_RUNS="${8:-3}"

AIDER_MODEL="${AIDER_MODEL:-azure/gpt-5}"

# Resolve script directory for finding host_files and cwv_benchmark.py
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$SCRIPT_DIR/../host_files/host_${FRAMEWORK}.sh"
CWV_SCRIPT="$SCRIPT_DIR/../../scripts/helper_scripts/cwv_benchmark.py"
CWV_JSON="/tmp/cwv_baseline_$$.json"

mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"

# Ensure clean state - reset staged/unstaged changes AND remove untracked files
git reset --hard HEAD 2>/dev/null || true
git clean -fd
rm -f .aider* 2>/dev/null || true
rm -rf .aider.tags.cache* 2>/dev/null || true

echo "[agent_aider] Starting aider agent" > "$LOG"
echo "[agent_aider] FRAMEWORK=$FRAMEWORK PORT=$PORT DEVICE=$DEVICE NUM_RUNS=$NUM_RUNS" >> "$LOG"

# ============================================
# PHASE 0: Measure CWV Baseline
# ============================================
CWV_SUMMARY=""

# if [[ -f "$HOST_SCRIPT" ]] && [[ -f "$CWV_SCRIPT" ]]; then
#   echo "[agent_aider] Phase 0: Measuring CWV baseline..." >> "$LOG"
  
#   # Start host server in background
#   PORT="$PORT" bash "$HOST_SCRIPT" "$REPO_DIR" &
#   HOST_PID=$!
#   echo "[cwv] Started host (PID=$HOST_PID) on port $PORT" >> "$LOG"
  
#   # Wait for server readiness (max 60s)
#   READY=0
#   for _ in {1..60}; do
#     if curl -fs "http://localhost:$PORT/" > /dev/null 2>&1; then
#       READY=1
#       break
#     fi
#     sleep 1
#   done
  
#   if [[ "$READY" -eq 1 ]]; then
#     echo "[cwv] Server ready at http://localhost:$PORT/" >> "$LOG"
    
#     # Measure CWV using existing script
#     if python3 "$CWV_SCRIPT" \
#       --url "http://localhost:$PORT/" \
#       --device "$DEVICE" \
#       --num-runs "$NUM_RUNS" \
#       > "$CWV_JSON" 2>> "$LOG"; then
      
#       echo "[cwv] CWV measurement complete. Results saved to $CWV_JSON" >> "$LOG"
      
#       # Parse JSON and create summary using helper script
#       CWV_PARSER="$SCRIPT_DIR/../parse_cwv_json.py"
#       CWV_SUMMARY=$(python3 "$CWV_PARSER" "$CWV_JSON" 2>> "$LOG")
      
#       echo "[cwv] Summary:" >> "$LOG"
#       echo "$CWV_SUMMARY" >> "$LOG"
#     else
#       echo "[cwv] WARNING: CWV measurement failed" >> "$LOG"
#     fi
#   else
#     echo "[cwv] WARNING: Server did not become ready within 60s" >> "$LOG"
#   fi
  
#   # Kill host server
#   kill "$HOST_PID" 2>/dev/null || true
#   wait "$HOST_PID" 2>/dev/null || true
#   echo "[cwv] Host server stopped" >> "$LOG"
  
#   echo "[agent_aider] Phase 0 complete." >> "$LOG"
# else
#   echo "[agent_aider] Phase 0 skipped: host script or cwv script not found" >> "$LOG"
#   echo "[agent_aider]   HOST_SCRIPT=$HOST_SCRIPT" >> "$LOG"
#   echo "[agent_aider]   CWV_SCRIPT=$CWV_SCRIPT" >> "$LOG"
# fi

echo "[agent_aider] Starting Phase 1: Planning" >> "$LOG"

PLAN_FILE="$REPO_DIR/plan.md"
PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"

# Pre-create plan.md so the LLM fills it (don't make the LLM create the file)
touch "$PLAN_FILE"
echo "[agent_aider] Pre-created plan.md (touch)" >> "$LOG"

# ============================================
# PHASE 1: Fill plan.md (read entire repo, write plan)
# ============================================
cat <<EOF > "$PLAN_PROMPT"
You are a web performance analyst. Create a performance optimization plan.

=== CONTEXT ===
Framework: $FRAMEWORK
Device: $DEVICE
===============

The CWV baseline JSON file is provided as a read-only file in context. Use it to inform your plan.
Do NOT copy the raw scores or JSON into plan.md.

YOUR TASK:
Read the entire repository, then fill in the existing 'plan.md' file with a detailed performance optimization plan.

The plan.md file should include:

1. **Baseline Analysis**: Summarize the current CWV metrics (from the JSON file) without copying raw scores
2. **Files to Modify**: List all files that need changes (full paths)  
3. **Proposed Changes**: For each file, describe in plain English:
   - Which function/section needs changes
   - What the change should accomplish
   - Why it will improve performance (consider the $FRAMEWORK framework specifics)
4. **Expected Impact**: Estimated improvements to FCP, LCP, CLS, INP for $DEVICE

IMPORTANT:
- Write ONLY to plan.md - do NOT edit any existing files
- This is a PLANNING document only - do NOT write actual code
- Implementation happens in Phase 2
- Consider $FRAMEWORK-specific optimizations and best practices
EOF

echo "[agent_aider] Phase 1: Generating plan (read entire repo, fill plan.md)..." >> "$LOG"

# Build aider args: pass CWV JSON as read-only file when available
AIDER_READ_FILES=()
if [[ -s "$CWV_JSON" ]]; then
  AIDER_READ_FILES=(--read "$CWV_JSON")
  echo "[agent_aider] Phase 1: Passing CWV JSON as read file: $CWV_JSON" >> "$LOG"
fi

# Aider fills the pre-created plan.md (read entire repo, write plan)
# Using --message-file instead of --message for better reliability
if ! aider \
  --yes-always \
  --no-auto-commits \
  --no-pretty \
  --no-stream \
  --no-show-model-warnings \
  --no-suggest-shell-commands \
  --no-detect-urls \
  --no-gitignore \
  --architect \
  --model "$AIDER_MODEL" \
  --editor-model "$AIDER_MODEL" \
  "${AIDER_READ_FILES[@]}" \
  --message-file "$PLAN_PROMPT" \
  >> "$LOG" 2>&1; then
    echo "[agent_aider] Phase 1 failed or timed out" >> "$LOG"
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    exit 0
fi

# Check if plan.md was created and has content
if [ ! -s "$PLAN_FILE" ]; then
    echo "[agent_aider] plan.md was not created or is empty, aborting" >> "$LOG"
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    exit 0
fi

# CRITICAL: Verify that ONLY plan.md was modified (reject if aider touched code files)
MODIFIED_FILES=$(git diff --name-only 2>/dev/null || true)
if [ -n "$MODIFIED_FILES" ]; then
    NON_PLAN_FILES=$(echo "$MODIFIED_FILES" | grep -v '^plan\.md$' || true)
    if [ -n "$NON_PLAN_FILES" ]; then
        echo "[agent_aider] ERROR: Phase 1 modified files other than plan.md!" >> "$LOG"
        echo "[agent_aider] Illegally modified files:" >> "$LOG"
        echo "$NON_PLAN_FILES" >> "$LOG"
        echo "[agent_aider] Aborting and restoring clean state." >> "$LOG"
        git reset --hard HEAD 2>/dev/null || true
        git clean -fd
        rm -f .aider* 2>/dev/null || true
        rm -rf .aider.tags.cache* 2>/dev/null || true
        rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
        exit 0
    fi
fi

echo "[agent_aider] Phase 1 complete. Plan saved to plan.md" >> "$LOG"

# Restore before phase 2 (keep plan.md by moving it temporarily)
PLAN_CONTENT="$(cat "$PLAN_FILE")"

# Log the extracted plan
echo "[agent_aider] ========== EXTRACTED PLAN START ==========" >> "$LOG"
echo "$PLAN_CONTENT" >> "$LOG"
echo "[agent_aider] ========== EXTRACTED PLAN END ==========" >> "$LOG"
echo "[agent_aider] Plan size: $(echo "$PLAN_CONTENT" | wc -c) bytes, $(echo "$PLAN_CONTENT" | wc -l) lines" >> "$LOG"

git reset --hard HEAD 2>/dev/null || true
git clean -fd
rm -f .aider* 2>/dev/null || true
rm -rf .aider.tags.cache* 2>/dev/null || true

# ============================================
# PHASE 2: Execute the plan
# ============================================
cat <<EOF > "$EXEC_PROMPT"
You are an expert web performance engineer.

=== CONTEXT ===
Framework: $FRAMEWORK
Device: $DEVICE
===============

You have created the following implementation plan. Now execute it precisely.

Rules:
- Do not change visible content
- Do not remove pages
- Do not add build systems
- Only edit existing files
- Apply $FRAMEWORK-specific best practices
- Optimize specifically for $DEVICE viewport and behavior

=== YOUR PLAN ===
$PLAN_CONTENT
=================

Now implement all the changes described in the plan above.
Make the exact edits to each file as specified.
EOF

echo "[agent_aider] Phase 2: Executing plan..." >> "$LOG"

if aider \
  --yes-always \
  --no-auto-commits \
  --no-pretty \
  --no-stream \
  --no-show-model-warnings \
  --no-suggest-shell-commands \
  --no-detect-urls \
  --no-gitignore \
  --architect \
  --model "$AIDER_MODEL" \
  --editor-model "$AIDER_MODEL" \
  --weak-model "$AIDER_MODEL" \
  --message-file "$EXEC_PROMPT" \
  >> "$LOG" 2>&1; then
    
    # CRITICAL: Remove plan.md before CWV analysis (must not be in patch)
    rm -f "$PLAN_FILE"
    echo "[agent_aider] Removed plan.md before patch capture" >> "$LOG"
    
    # Success: Generate patch and clean up
    git diff > "$PATCH_FILE"
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    echo "[agent_aider] Phase 2 complete. Patch saved." >> "$LOG"
    echo "[agent_aider] Done" >> "$LOG"
    exit 0

else
    # Failure: Clean up and log error
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    echo "[agent_aider] Phase 2 failed" >> "$LOG"
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    exit 0
fi
