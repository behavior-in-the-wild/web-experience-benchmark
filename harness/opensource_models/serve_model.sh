#!/usr/bin/env bash
set -euo pipefail

# Activate the project venv so vllm and its dependencies are on PATH
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -f "$REPO_ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi

# Load HF_TOKEN (and any other secrets) from .env if not already set
if [[ -z "${HF_TOKEN:-}" && -f "$REPO_ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$REPO_ROOT/.env"; set +a
fi

MODEL_ID="${MODEL_ID:?MODEL_ID is required}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:?SERVED_MODEL_NAME is required}"

VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"
ENABLE_AUTO_TOOL_CHOICE="${ENABLE_AUTO_TOOL_CHOICE:-1}"
# Disable each model's own generation_config.json so temperature is not overridden
# per-model (e.g. Gemma ships temp=1.0, top_k=64). Then pin temperature=0 (greedy)
# to match SWE-bench agentic methodology (Yang et al. 2405.15793, §Experimental Setup).
GENERATION_CONFIG="${GENERATION_CONFIG:-vllm}"
OVERRIDE_GENERATION_CONFIG="${OVERRIDE_GENERATION_CONFIG:-{\"temperature\":0.0}}"

args=(
  serve "$MODEL_ID"
  --host "$VLLM_HOST"
  --port "$VLLM_PORT"
  --served-model-name "$SERVED_MODEL_NAME"
  --api-key "$VLLM_API_KEY"
)

[[ "$TRUST_REMOTE_CODE" == "1" ]] && args+=(--trust-remote-code)
[[ -n "${DTYPE:-}" ]] && args+=(--dtype "$DTYPE")
[[ -n "${TENSOR_PARALLEL_SIZE:-}" ]] && args+=(--tensor-parallel-size "$TENSOR_PARALLEL_SIZE")
[[ -n "${PIPELINE_PARALLEL_SIZE:-}" ]] && args+=(--pipeline-parallel-size "$PIPELINE_PARALLEL_SIZE")
[[ -n "${GPU_MEMORY_UTILIZATION:-}" ]] && args+=(--gpu-memory-utilization "$GPU_MEMORY_UTILIZATION")
[[ -n "${MAX_MODEL_LEN:-}" ]] && args+=(--max-model-len "$MAX_MODEL_LEN")
[[ -n "${MAX_NUM_BATCHED_TOKENS:-}" ]] && args+=(--max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS")
[[ -n "${QUANTIZATION:-}" ]] && args+=(--quantization "$QUANTIZATION")
[[ -n "${CHAT_TEMPLATE:-}" ]] && args+=(--chat-template "$CHAT_TEMPLATE")
[[ -n "${TOKENIZER_MODE:-}" ]] && args+=(--tokenizer-mode "$TOKENIZER_MODE")
[[ -n "${KV_CACHE_DTYPE:-}" ]] && args+=(--kv-cache-dtype "$KV_CACHE_DTYPE")
[[ -n "${BLOCK_SIZE:-}" ]] && args+=(--block-size "$BLOCK_SIZE")
[[ "${ENABLE_EXPERT_PARALLEL:-0}" == "1" ]] && args+=(--enable-expert-parallel)
[[ -n "${GENERATION_CONFIG:-}" ]] && args+=(--generation-config "$GENERATION_CONFIG")
[[ -n "${OVERRIDE_GENERATION_CONFIG:-}" ]] && args+=(--override-generation-config "$OVERRIDE_GENERATION_CONFIG")

if [[ "$ENABLE_AUTO_TOOL_CHOICE" == "1" ]]; then
  args+=(--enable-auto-tool-choice)
  [[ -n "${TOOL_CALL_PARSER:-}" ]] && args+=(--tool-call-parser "$TOOL_CALL_PARSER")
fi

[[ -n "${REASONING_PARSER:-}" ]] && args+=(--reasoning-parser "$REASONING_PARSER")
[[ -n "${DEFAULT_CHAT_TEMPLATE_KWARGS:-}" ]] && args+=(--default-chat-template-kwargs "$DEFAULT_CHAT_TEMPLATE_KWARGS")

if [[ -n "${VLLM_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra_args=($VLLM_EXTRA_ARGS)
  args+=("${extra_args[@]}")
fi

echo "[vllm] Serving $MODEL_ID as $SERVED_MODEL_NAME on $VLLM_HOST:$VLLM_PORT"
echo "[vllm] Command: vllm ${args[*]}"
exec vllm "${args[@]}"
