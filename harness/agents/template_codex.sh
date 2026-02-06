# #!/usr/bin/env bash
# set -euo pipefail

# REPO_DIR="$1"
# TASK_SPEC="$2"
# LOG_FILE="$3"

# # Load harness .env for API keys and Azure/OpenAI settings
# HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# if [[ -f "$HARNESS_DIR/.env" ]]; then
#   set -a
#   source "$HARNESS_DIR/.env"
#   set +a
# fi

# cd "$REPO_DIR"

# echo "[codex] Running Codex agent" > "$LOG_FILE"

# # Prefer Azure OpenAI (AZURE_OPENAI_API_KEY); fall back to OpenAI (OPENAI_API_KEY)
# if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
#   # Azure: base_url must end with /openai/v1 (Codex v1 Responses API)
#   AZURE_BASE="${AZURE_OPENAI_ENDPOINT:-https://mdsr-foundry-resource.cognitiveservices.azure.com/}"
#   AZURE_BASE="${AZURE_BASE%/}"
#   AZURE_BASE="${AZURE_BASE}/openai/v1"
#   AZURE_MODEL="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5}"
#   echo "[codex] Using Azure OpenAI: $AZURE_BASE, model=$AZURE_MODEL" >> "$LOG_FILE"
#   CODEX_EXTRA=(
#     -c "model_provider=azure"
#     -c "model=$AZURE_MODEL"
#     -c "model_providers.azure.name=Azure OpenAI"
#     -c "model_providers.azure.base_url=$AZURE_BASE"
#     -c "model_providers.azure.env_key=AZURE_OPENAI_API_KEY"
#     -c "model_providers.azure.wire_api=responses"
#   )
# elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
#   echo "[codex] Using OpenAI (api.openai.com)" >> "$LOG_FILE"
#   CODEX_EXTRA=(-m gpt-5)
# else
#   echo "ERROR: Set AZURE_OPENAI_API_KEY or OPENAI_API_KEY in harness/.env" >> "$LOG_FILE"
#   exit 1
# fi

# TASK="$(cat "$TASK_SPEC")"

# codex exec \
#   -C "$REPO_DIR" \
#   "${CODEX_EXTRA[@]}" \
#   --full-auto \
#   --sandbox workspace-write \
#   "$TASK" \
#   >> "$LOG_FILE" 2>&1

# echo "[codex] Done" >> "$LOG_FILE"

#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$1"
TASK_SPEC="$2"          # will be optimize_cwv_debug.txt
LOG_FILE="$3"

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS_DIR="$HARNESS_DIR/tasks"

DEBUG_TASK="$TASKS_DIR/optimize_cwv_debug.txt"
APPLY_TASK_TEMPLATE="$TASKS_DIR/optimize_cwv_apply.txt"

if [[ -f "$HARNESS_DIR/.env" ]]; then
  set -a
  source "$HARNESS_DIR/.env"
  set +a
fi

cd "$REPO_DIR"

echo "[codex] Two-phase LCP agent" > "$LOG_FILE"

# -------------------------
# Model config (unchanged)
# -------------------------
if [[ -n "${AZURE_OPENAI_API_KEY:-}" ]]; then
  AZURE_BASE="${AZURE_OPENAI_ENDPOINT%/}/openai/v1"
  AZURE_MODEL="${AZURE_OPENAI_API_DEPLOYMENT_NAME:-gpt-5}"
  CODEX_EXTRA=(
    -c "model_provider=azure"
    -c "model=$AZURE_MODEL"
    -c "model_providers.azure.base_url=$AZURE_BASE"
    -c "model_providers.azure.env_key=AZURE_OPENAI_API_KEY"
    -c "model_providers.azure.wire_api=responses"
  )
elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
  CODEX_EXTRA=(-m gpt-5)
else
  echo "ERROR: Missing API key" >> "$LOG_FILE"
  exit 1
fi

# -------------------------
# Phase 1: Diagnosis
# -------------------------
echo "[codex] Phase 1: LCP diagnosis" >> "$LOG_FILE"

ANALYSIS_OUT="$(mktemp)"

codex exec \
  -C "$REPO_DIR" \
  "${CODEX_EXTRA[@]}" \
  --sandbox read-only \
  "$(<"$DEBUG_TASK")" \
  > "$ANALYSIS_OUT" 2>>"$LOG_FILE"

echo "[codex] Diagnosis output:" >> "$LOG_FILE"
sed 's/^/[analysis] /' "$ANALYSIS_OUT" >> "$LOG_FILE"

# -------------------------
# Phase 2: Apply fixes
# -------------------------
echo "[codex] Phase 2: Applying fixes" >> "$LOG_FILE"

FINAL_TASK="$(mktemp)"
sed "s|{{ANALYSIS}}|$(sed 's/[&/\]/\\&/g' "$ANALYSIS_OUT")|" \
  "$APPLY_TASK_TEMPLATE" > "$FINAL_TASK"

codex exec \
  -C "$REPO_DIR" \
  "${CODEX_EXTRA[@]}" \
  --full-auto \
  --sandbox workspace-write \
  "$(<"$FINAL_TASK")" \
  >> "$LOG_FILE" 2>&1

echo "[codex] Done" >> "$LOG_FILE"
