#!/usr/bin/env bash
set -euo pipefail

# =========================
# Resolve paths
# =========================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CSV="$SCRIPT_DIR/repos.csv"
TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv.txt"

TMP_ROOT="$SCRIPT_DIR/out/run"
RESULTS_DIR="$SCRIPT_DIR/out/results"

CWV_SCRIPT="$SCRIPT_DIR/../cwv-agent-main/cwv-agent/scripts/helper_scripts/cwv_benchmark.py"

PORT=4000
DEVICE="mobile"
NUM_RUNS=3

# =========================
# Load environment
# =========================
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# =========================
# Agents to benchmark
# =========================
AGENTS=(
  "agents/agent_null.sh"
  "agents/agent_aider.sh"
)

# =========================
# Sanity checks
# =========================
[[ -f "$CSV" ]] || { echo "Missing repos.csv"; exit 1; }
[[ -f "$TASK_SPEC" ]] || { echo "Missing task spec"; exit 1; }
[[ -f "$CWV_SCRIPT" ]] || { echo "Missing cwv_benchmark.py"; exit 1; }

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"

# =========================
# Main loop
# =========================
awk -F',' 'NR>1 { print $1 "|" $2 "|" $3 "|" $4 "|" $5 }' "$CSV" |
while IFS='|' read -r ID GITHUB COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH
do
  for AGENT in "${AGENTS[@]}"; do
    AGENT_NAME="$(basename "$AGENT" .sh)"

    echo "======================================"
    echo "Repo ID=$ID"
    echo "Repo=$GITHUB"
    echo "Agent=$AGENT_NAME"
    echo "======================================"

    RUN_DIR="$TMP_ROOT/${ID}_${AGENT_NAME}"
    REPO_DIR="$RUN_DIR/repo"

    # Hard cleanup (important!)
    pkill -f "jekyll serve" 2>/dev/null || true
    pkill -f "http.server $PORT" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    mkdir -p "$RUN_DIR" "$REPO_DIR"

    # -------------------------
    # 1. Unzip snapshot (SAFE)
    # -------------------------
    unzip -q "$SCRIPT_DIR/$ZIP_REPO_PATH" -d "$RUN_DIR"

    # Case analysis:
    # 1) ZIP → single top-level folder (common GitHub ZIP)
    # 2) ZIP → files directly
    # 3) ZIP already extracted into repo/ (reruns / weird zips)

    ITEMS=("$RUN_DIR"/*)
    TOP_ITEMS=()
    for item in "${ITEMS[@]}"; do
      [[ "$item" == "$REPO_DIR" ]] && continue
      TOP_ITEMS+=("$item")
    done

    # Case 3: repo already exists → do nothing
    if [[ -d "$REPO_DIR" && "$(ls -A "$REPO_DIR" 2>/dev/null)" ]]; then
      echo "[debug] Repo directory already populated, skipping unzip normalization"

    # Case 1: single directory wrapper
    elif [[ ${#TOP_ITEMS[@]} -eq 1 && -d "${TOP_ITEMS[0]}" ]]; then
      shopt -s dotglob nullglob
      mv "${TOP_ITEMS[0]}"/* "$REPO_DIR"
      shopt -u dotglob nullglob
      rmdir "${TOP_ITEMS[0]}" 2>/dev/null || true

    # Case 2: files at top level
    else
      for item in "${TOP_ITEMS[@]}"; do
        mv "$item" "$REPO_DIR"
      done
    fi

    # -------------------------
    # 2. Initialize git baseline for patching
    # -------------------------
    # We want to:
    #  - capture all agent edits as a git patch
    #  - be able to re-apply that patch on a clean baseline
    #
    # This makes the evaluation depend ONLY on the patch, not on any
    # transient state the agent might leave behind.
    if [[ ! -d "$REPO_DIR/.git" ]]; then
      echo "[git] Initializing baseline git repo in $REPO_DIR"
      if git -C "$REPO_DIR" init -q && \
         git -C "$REPO_DIR" add -A && \
         git -C "$REPO_DIR" commit -qm "baseline"; then
        :
      else
        echo "[git] WARNING: failed to create baseline git repo; patch capture may be skipped" >&2
      fi
    fi

    # -------------------------
    # 3. Run agent
    # -------------------------
    AGENT_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_agent.log"
    echo "[2/6] Running agent: $AGENT_NAME"

    timeout 900 bash "$SCRIPT_DIR/$AGENT" \
      "$REPO_DIR" \
      "$TASK_SPEC" \
      "$AGENT_LOG" \
      || echo "[agent] Agent failed or timed out (continuing)"

    # Capture patch after agent edits (if repo has .git),
    # then reset to baseline and re-apply the patch.
    PATCH_FILE="$RESULTS_DIR/${ID}_${AGENT_NAME}.patch"
    DIFF_LOG="$RESULTS_DIR/${ID}_${AGENT_NAME}_git.diff"
    if [[ -d "$REPO_DIR/.git" ]]; then
      echo "[patch] Capturing agent edits as git patch"
      (
        set +e
        cd "$REPO_DIR" || exit 0
        # Stage all changes (including new/deleted files)
        git add -A >/dev/null 2>&1
        # Write patch against baseline commit
        git diff --cached > "$PATCH_FILE" 2>/dev/null
        # Keep a copy as a human-readable diff log
        cp "$PATCH_FILE" "$DIFF_LOG" 2>/dev/null || git diff > "$DIFF_LOG" 2>/dev/null

        # Reset back to clean baseline
        git reset --hard HEAD >/dev/null 2>&1 || true
        git clean -fd >/dev/null 2>&1 || true

        # Re-apply the patch we just captured (if non-empty)
        if [[ -s "$PATCH_FILE" ]]; then
          echo "[patch] Applying captured patch to clean baseline"
          git apply "$PATCH_FILE" >/dev/null 2>&1 || {
            echo "[patch] WARNING: failed to apply patch; proceeding with baseline files" >&2
          }
        else
          echo "[patch] No changes detected from agent (empty patch)"
        fi
      )
    else
      echo "[diff] No .git directory; skipping diff/patch" > "$DIFF_LOG"
    fi

    # -------------------------
    # 4. Launch host
    # -------------------------
    echo "[3/6] Launching host"
    rm -f /tmp/host_*.log

    PORT="$PORT" bash "$SCRIPT_DIR/$HOST_FILE_PATH" "$REPO_DIR" &
    HOST_PID=$!

    # -------------------------
    # 5. Wait for readiness
    # -------------------------
    echo "[4/6] Waiting for site readiness"

    READY=0
    for _ in {1..90}; do
      if curl -fs "http://localhost:$PORT/" >/dev/null; then
        READY=1
        break
      fi
      sleep 1
    done

    if [[ "$READY" -ne 1 ]]; then
      echo "ERROR: Site never became ready"
      tail -n 50 /tmp/host_jekyll.log || true
      kill "$HOST_PID" 2>/dev/null || true
      continue
    fi

    # -------------------------
    # 6. Measure CWV
    # -------------------------
    RESULT_JSON="$RESULTS_DIR/${ID}_${AGENT_NAME}.json"
    echo "[5/6] Measuring CWV"

    python3 "$CWV_SCRIPT" \
      --device "$DEVICE" \
      --num-runs "$NUM_RUNS" \
      --url "http://localhost:$PORT/" \
      > "$RESULT_JSON"

    # -------------------------
    # 7. Teardown
    # -------------------------
    echo "[6/6] Teardown"
    kill "$HOST_PID" 2>/dev/null || true
    wait "$HOST_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"

    echo "✓ Done: ID=$ID Agent=$AGENT_NAME"
  done
done
