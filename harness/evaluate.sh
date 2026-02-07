#!/usr/bin/env bash
set -euo pipefail

AUTO_SNAPSHOT=0

if [[ "${1:-}" == "--auto-snapshot" ]]; then
  AUTO_SNAPSHOT=1
  shift
fi

# =========================
# Resolve paths
# =========================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
CSV="$SCRIPT_DIR/tmp.csv"
# TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv.txt"
TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv_debug.txt"

TMP_ROOT="$SCRIPT_DIR/out/${RUN_TIMESTAMP}/run"
RESULTS_DIR="$SCRIPT_DIR/out/${RUN_TIMESTAMP}/results"

CWV_SCRIPT="$SCRIPT_DIR/../scripts/helper_scripts/cwv_benchmark.py"

clone_and_zip_repo() {
  local github_repo="$1"      # e.g. user/repo
  local commit_id="$2"        # commit SHA (may be empty)
  local zip_path="$3"         # where to write zip
  local work_dir="$4"         # temp dir to clone into

  echo "[snapshot] Cloning $github_repo"

  rm -rf "$work_dir"
  git clone "https://github.com/$github_repo.git" "$work_dir" >/dev/null 2>&1 \
    || { echo "[snapshot] ERROR: failed to clone $github_repo"; return 1; }

  cd "$work_dir"

  if [[ -n "$commit_id" && "$commit_id" != "null" ]]; then
    git checkout "$commit_id" >/dev/null 2>&1 \
      || { echo "[snapshot] ERROR: failed to checkout $commit_id"; return 1; }
  fi

  mkdir -p "$(dirname "$zip_path")"
  zip -qr "$zip_path" . \
    || { echo "[snapshot] ERROR: failed to zip repo"; return 1; }

  echo "[snapshot] Created $zip_path"
}

CWV_SCRIPT="$SCRIPT_DIR/../scripts/helper_scripts/cwv_benchmark.py"

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
  "agents/template_null.sh"
  "agents/template_codex.sh"
)

# =========================
# Sanity checks
# =========================
[[ -f "$CSV" ]] || { echo "Missing repos.csv"; exit 1; }
[[ -f "$TASK_SPEC" ]] || { echo "Missing task spec"; exit 1; }
[[ -f "$CWV_SCRIPT" ]] || { echo "Missing cwv_benchmark.py"; exit 1; }

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"
echo "[run] Output: $SCRIPT_DIR/out/$RUN_TIMESTAMP/ (run + results)"

# =========================
# Main loop
# =========================
awk -F',' 'NR>1 { print $1 "|" $2 "|" $3 "|" $4 "|" $5 }' "$CSV" |
while IFS='|' read -r ID REPO_ID COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH
do
  for AGENT in "${AGENTS[@]}"; do
    AGENT_NAME="$(basename "$AGENT" .sh)"

    echo "======================================"
    echo "Repo ID=$ID"
    echo "Repo=$REPO_ID"
    echo "Agent=$AGENT_NAME"
    echo "======================================"
    echo "[debug] RESULTS_DIR=$RESULTS_DIR"
    echo "[debug] TMP_ROOT=$TMP_ROOT"

    RUN_DIR="$TMP_ROOT/${ID}_${AGENT_NAME}"
    REPO_DIR="$RUN_DIR/repo"
    echo "[debug] RUN_DIR=$RUN_DIR REPO_DIR=$REPO_DIR"

    # Hard cleanup (important!)
    pkill -f "jekyll serve" 2>/dev/null || true
    pkill -f "http.server $PORT" 2>/dev/null || true
    rm -rf "$RUN_DIR"
    mkdir -p "$RUN_DIR" "$REPO_DIR"

    # -------------------------
    # 1. Ensure snapshot exists (clone+zip if needed)
    # -------------------------
    ZIP_ABS_PATH="$SCRIPT_DIR/$ZIP_REPO_PATH"

    if [[ ! -f "$ZIP_ABS_PATH" ]]; then
      if [[ "$AUTO_SNAPSHOT" -eq 1 ]]; then
        echo "[snapshot] Missing ZIP, auto-snapshot enabled"

        SNAPSHOT_TMP="$RUN_DIR/_snapshot_tmp"
        clone_and_zip_repo \
          "$REPO_ID" \
          "$COMMIT_ID" \
          "$ZIP_ABS_PATH" \
          "$SNAPSHOT_TMP" \
          || { echo "[snapshot] Failed; skipping repo"; continue; }

      else
        echo "ERROR: Missing snapshot $ZIP_REPO_PATH"
        echo "       Re-run with --auto-snapshot to clone+zip automatically"
        continue
      fi
    fi

    # -------------------------
    # 2. Unzip snapshot (SAFE)
    # -------------------------
    unzip -q "$ZIP_ABS_PATH" -d "$RUN_DIR"


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

    echo "[debug] Agent log will be: $AGENT_LOG"
    timeout 900 bash "$SCRIPT_DIR/$AGENT" \
      "$REPO_DIR" \
      "$TASK_SPEC" \
      "$AGENT_LOG" \
      || echo "[agent] Agent failed or timed out (continuing)"
    if [[ -f "$AGENT_LOG" ]]; then
      echo "[debug] Agent log exists, size=$(wc -c < "$AGENT_LOG") bytes"
    else
      echo "[debug] WARNING: Agent log not created: $AGENT_LOG"
    fi

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
    if [[ -f "$PATCH_FILE" ]]; then
      echo "[debug] Patch file: $PATCH_FILE size=$(wc -c < "$PATCH_FILE") bytes"
    else
      echo "[debug] WARNING: Patch file not created: $PATCH_FILE"
    fi
    if [[ -f "$DIFF_LOG" ]]; then
      echo "[debug] Diff log: $DIFF_LOG size=$(wc -c < "$DIFF_LOG") bytes"
    else
      echo "[debug] WARNING: Diff log not created: $DIFF_LOG"
    fi

    # -------------------------
    # 4. Launch host
    # -------------------------
    echo "[3/6] Launching host"
    rm -f /tmp/host_*.log
    
    echo "[debug] SCRIPT_DIR=$SCRIPT_DIR"
    echo "[debug] HOST_FILE_PATH=$HOST_FILE_PATH"
    PORT="$PORT" bash "$SCRIPT_DIR/$HOST_FILE_PATH" "$REPO_DIR" &
    HOST_PID=$!
    echo "[debug] Host started PID=$HOST_PID PORT=$PORT (host script: $HOST_FILE_PATH)"

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
      echo "[debug] Last curl failed for http://localhost:$PORT/"
      tail -n 50 /tmp/host_jekyll.log 2>/dev/null || true
      kill "$HOST_PID" 2>/dev/null || true
      continue
    fi
    echo "[debug] Site ready at http://localhost:$PORT/"

    # -------------------------
    # 6. Measure CWV
    # -------------------------
    RESULT_JSON="$RESULTS_DIR/${ID}_${AGENT_NAME}.json"
    CWV_STDERR="$RESULTS_DIR/${ID}_${AGENT_NAME}_cwv_stderr.txt"
    echo "[5/6] Measuring CWV"
    echo "[debug] CWV output will be: $RESULT_JSON"

    python3 "$CWV_SCRIPT" \
      --device "$DEVICE" \
      --num-runs "$NUM_RUNS" \
      --url "http://localhost:$PORT/" \
      > "$RESULT_JSON" 2> "$CWV_STDERR"
    CWV_EXIT=$?
    if [[ "$CWV_EXIT" -ne 0 ]]; then
      echo "[debug] WARNING: CWV script exited with $CWV_EXIT"
      echo "[debug] CWV stderr (first 30 lines):"
      head -n 30 "$CWV_STDERR" 2>/dev/null || true
    fi
    if [[ -f "$RESULT_JSON" ]]; then
      echo "[debug] Result JSON exists, size=$(wc -c < "$RESULT_JSON") bytes"
    else
      echo "[debug] WARNING: Result JSON not created: $RESULT_JSON"
    fi

    # -------------------------
    # 7. Teardown
    # -------------------------
    echo "[6/6] Teardown"
    kill "$HOST_PID" 2>/dev/null || true
    wait "$HOST_PID" 2>/dev/null || true
    rm -rf "$RUN_DIR"

    echo "[debug] Results in $RESULTS_DIR:"
    ls -la "$RESULTS_DIR" 2>/dev/null || true
    echo "✓ Done: ID=$ID Agent=$AGENT_NAME"
  done
done
