#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$1"
TASK_SPEC="$2"
LOG="$3"
PATCH_FILE="$4"
FRAMEWORK="${5:-static_html}"  # jekyll, hugo, static_html, next, react, vue, etc.
PORT="${6:-4000}"
DEVICE="${7:-mobile}"
NUM_RUNS="${8:-3}"

AIDER_MODEL="${AIDER_MODEL:-azure/gpt-5}"

# Resolve script directory for finding host_files and cwv_benchmark.py
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$SCRIPT_DIR/../host_files/host_${FRAMEWORK}.sh"
CWV_SCRIPT="$SCRIPT_DIR/../../scripts/helper_scripts/cwv_benchmark.py"
CWV_JSON="/tmp/cwv_baseline_$$.json"

mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"

# Ensure clean state
git restore .

echo "[agent_aider] Starting aider agent" > "$LOG"
echo "[agent_aider] FRAMEWORK=$FRAMEWORK PORT=$PORT DEVICE=$DEVICE NUM_RUNS=$NUM_RUNS" >> "$LOG"

# ============================================
# PHASE 0: Measure CWV Baseline
# ============================================
CWV_SUMMARY=""

if [[ -f "$HOST_SCRIPT" ]] && [[ -f "$CWV_SCRIPT" ]]; then
  echo "[agent_aider] Phase 0: Measuring CWV baseline..." >> "$LOG"
  
  # Start host server in background
  PORT="$PORT" bash "$HOST_SCRIPT" "$REPO_DIR" &
  HOST_PID=$!
  echo "[cwv] Started host (PID=$HOST_PID) on port $PORT" >> "$LOG"
  
  # Wait for server readiness (max 60s)
  READY=0
  for _ in {1..60}; do
    if curl -fs "http://localhost:$PORT/" > /dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 1
  done
  
  if [[ "$READY" -eq 1 ]]; then
    echo "[cwv] Server ready at http://localhost:$PORT/" >> "$LOG"
    
    # Measure CWV using existing script
    if python3 "$CWV_SCRIPT" \
      --url "http://localhost:$PORT/" \
      --device "$DEVICE" \
      --num-runs "$NUM_RUNS" \
      > "$CWV_JSON" 2>> "$LOG"; then
      
      echo "[cwv] CWV measurement complete. Results saved to $CWV_JSON" >> "$LOG"
      
      # Parse JSON and create summary using helper script
      CWV_PARSER="$SCRIPT_DIR/../parse_cwv_json.py"
      CWV_SUMMARY=$(python3 "$CWV_PARSER" "$CWV_JSON" 2>> "$LOG")
      
      echo "[cwv] Summary:" >> "$LOG"
      echo "$CWV_SUMMARY" >> "$LOG"
    else
      echo "[cwv] WARNING: CWV measurement failed" >> "$LOG"
    fi
  else
    echo "[cwv] WARNING: Server did not become ready within 60s" >> "$LOG"
  fi
  
  # Kill host server
  kill "$HOST_PID" 2>/dev/null || true
  wait "$HOST_PID" 2>/dev/null || true
  echo "[cwv] Host server stopped" >> "$LOG"
  
  echo "[agent_aider] Phase 0 complete." >> "$LOG"
else
  echo "[agent_aider] Phase 0 skipped: host script or cwv script not found" >> "$LOG"
  echo "[agent_aider]   HOST_SCRIPT=$HOST_SCRIPT" >> "$LOG"
  echo "[agent_aider]   CWV_SCRIPT=$CWV_SCRIPT" >> "$LOG"
fi

echo "[agent_aider] Starting Phase 1: Planning" >> "$LOG"

PLAN_FILE="$REPO_DIR/plan.md"
PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"

# ============================================
# PHASE 1: Generate plan.md (with CWV context)
# ============================================
cat <<EOF > "$PLAN_PROMPT"
You are an expert web performance engineer.

Your job is ONLY to create a detailed implementation plan.

=== CURRENT CWV BASELINE (measured on $DEVICE, $NUM_RUNS runs) ===
$CWV_SUMMARY
================================================================

STRICT RULES FOR PLANNING PHASE:
1. DO NOT change any code files yet.
2. You must ONLY search, read, and understand the codebase.
3. Your output must be written to a new file named 'plan.md'.
4. Do NOT modify any other existing files in the repository.

Task:
$(cat "$TASK_SPEC")

Instructions:
1. Analyze the repository structure.
2. Review the CWV baseline above to understand current performance issues.
3. Identify the EXACT files that need to be modified to improve the metrics.
4. For each file, describe the precise changes needed.
5. Write the detailed plan to 'plan.md'.

The 'plan.md' file must include:
- List of files to modify (with full paths)
- Specific changes for each file
- Expected impact on LCP, CLS, INP based on the baseline measurements
EOF

echo "[agent_aider] Phase 1: Generating plan..." >> "$LOG"

# Pre-create plan.md so we can pass it to aider (focusing the agent on this file)
touch "$PLAN_FILE"

if ! aider \
  --yes \
  --no-auto-commits \
  --no-pretty \
  --architect \
  --model "$AIDER_MODEL" \
  --editor-model "$AIDER_MODEL" \
  --weak-model "$AIDER_MODEL" \
  --message "$(cat "$PLAN_PROMPT")" \
  "$PLAN_FILE" \
  >> "$LOG" 2>&1; then
    echo "[agent_aider] Phase 1 failed" >> "$LOG"
    git restore .
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    exit 0
fi

# Check if plan.md has content
if [ ! -s "$PLAN_FILE" ]; then
    echo "[agent_aider] plan.md is empty, aborting" >> "$LOG"
    git restore .
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    exit 0
fi

echo "[agent_aider] Phase 1 complete. Plan saved to plan.md" >> "$LOG"

# Restore before phase 2 (keep plan.md by moving it temporarily)
PLAN_CONTENT="$(cat "$PLAN_FILE")"
git restore .

# ============================================
# PHASE 2: Execute the plan
# ============================================
cat <<EOF > "$EXEC_PROMPT"
You are an expert web performance engineer.

You have created the following implementation plan. Now execute it precisely.

Rules:
- Do not change visible content
- Do not remove pages
- Do not add build systems
- Only edit existing files

=== YOUR PLAN ===
$PLAN_CONTENT
=================

Now implement all the changes described in the plan above.
Make the exact edits to each file as specified.
EOF

echo "[agent_aider] Phase 2: Executing plan..." >> "$LOG"

if aider \
  --yes \
  --no-auto-commits \
  --no-pretty \
  --model "$AIDER_MODEL" \
  --weak-model "$AIDER_MODEL" \
  --message "$(cat "$EXEC_PROMPT")" \
  >> "$LOG" 2>&1; then
    
    # Success: Generate patch and clean up
    git diff > "$PATCH_FILE"
    git restore .
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    echo "[agent_aider] Phase 2 complete. Patch saved." >> "$LOG"
    echo "[agent_aider] Done" >> "$LOG"
    exit 0

else
    # Failure: Clean up and log error
    git restore .
    echo "[agent_aider] Phase 2 failed" >> "$LOG"
    rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"
    exit 0
fi
