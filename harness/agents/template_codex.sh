#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Common agent template (Codex variant)
# ============================================================

REPO_DIR="$1"
TASK_SPEC="$2"
LOG_FILE="$3"
PATCH_FILE="${4:-/dev/null}"

FRAMEWORK="${FRAMEWORK:-unknown}"
DEVICE="${DEVICE:-unknown}"

cd "$REPO_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

echo "[agent] Two-phase CWV agent (codex)" > "$LOG_FILE"

PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"

# ============================================================
# Phase 1 workspace: repo read-only, plan.md writable only
# ============================================================
PHASE1_DIR="$(mktemp -d)"
trap 'chmod -R u+w "$PHASE1_DIR" 2>/dev/null; rm -rf "$PHASE1_DIR"' EXIT

# Copy repo to phase1 workspace (repo will be made read-only)
cp -r "$REPO_DIR" "$PHASE1_DIR/repo"

# Write init CWV data for the model to read
CWV_BASELINE="${CWV_BASELINE_JSON:-null}"
LCP_ENTRIES="${LCP_ENTRIES_JSON:-null}"
printf '{"baseline":%s,"lcp_entries":%s}\n' "$CWV_BASELINE" "$LCP_ENTRIES" > "$PHASE1_DIR/repo/init_cwv.json"

# Make repo read-only so model can only write plan.md
chmod -R a-w "$PHASE1_DIR/repo"

# plan.md is the only writable file in the workspace
touch "$PHASE1_DIR/plan.md"

# -------------------------
# Model config
# -------------------------
if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  AZURE_BASE="${AZURE_OPENAI_ENDPOINT%/}/openai/v1"
  AZURE_MODEL="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5}"
  CODEX_EXTRA=(
    -c "model_provider=azure"
    -c "model=$AZURE_MODEL"
    -c "model_providers.azure.name=Azure"
    -c "model_providers.azure.base_url=$AZURE_BASE"
    -c "model_providers.azure.env_key=AZURE_OPENAI_API_KEY"
    -c "model_providers.azure.wire_api=responses"
    -c "reasoning.effort=medium"
    -c "max_output_tokens=50000"
  )
else
  echo "ERROR: Missing AZURE_OPENAI_API_KEY" >> "$LOG_FILE"
  exit 1
fi

# ============================================================
# Phase 1 — Planning
# ============================================================
cat <<EOF > "$PLAN_PROMPT"
You are a web performance analyst.

Context:
- Framework: $FRAMEWORK
- Device: $DEVICE

Read:
- repo/ directory (entire repository)
- repo/init_cwv.json (baseline CWV metrics and LCP element entries)

Write:
- plan.md ONLY (pre-created empty file at workspace root)

Rules:
- Do not modify anything in repo/
- Do not create files other than editing plan.md
EOF

# -------- CODEX CALL (PHASE 1) — two workspaces: repo read-only, plan.md writable --------
codex exec \
  -C "$PHASE1_DIR" \
  "${CODEX_EXTRA[@]}" \
  --skip-git-repo-check \
  --sandbox workspace-write \
  "$(<"$PLAN_PROMPT")" \
  >> "$LOG_FILE" 2>&1
# -------------------------------------

# plan.md is the only writable file; repo/ was chmod read-only
if [[ ! -s "$PHASE1_DIR/plan.md" ]]; then
  echo "[agent] ERROR: Phase 1 did not produce plan.md or it is empty" >> "$LOG_FILE"
  exit 0
fi

PLAN_CONTENT="$(cat "$PHASE1_DIR/plan.md")"

# ============================================================
# Phase 2 — Execution (plan content in prompt, no plan.md in repo)
# ============================================================
# Use printf to avoid shell expansion of backticks/$() in plan content
printf 'You are an expert web performance engineer.

Context:
- Framework: %s
- Device: %s

Plan:
%s

Rules:
- Do not change visible content
- Do not remove pages
- Do not add build systems
- Edit existing files only

Execute the plan exactly.
' "$FRAMEWORK" "$DEVICE" "$PLAN_CONTENT" > "$EXEC_PROMPT"

# -------- CODEX CALL (PHASE 2) --------
codex exec \
  -C "$REPO_DIR" \
  "${CODEX_EXTRA[@]}" \
  --skip-git-repo-check \
  --sandbox workspace-write \
  "$(<"$EXEC_PROMPT")" \
  >> "$LOG_FILE" 2>&1
# -------------------------------------

git diff > "$PATCH_FILE"
git reset --hard HEAD
git clean -fd
rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"

echo "[agent] Done" >> "$LOG_FILE"
