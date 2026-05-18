#!/usr/bin/env bash
set -euo pipefail

MODEL_ID="${MODEL_ID:?MODEL_ID is required}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:?SERVED_MODEL_NAME is required}"

VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
TRUST_REMOTE_CODE="${TRUST_REMOTE_CODE:-1}"
ENABLE_AUTO_TOOL_CHOICE="${ENABLE_AUTO_TOOL_CHOICE:-1}"

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

if [[ "$ENABLE_AUTO_TOOL_CHOICE" == "1" ]]; then
  args+=(--enable-auto-tool-choice)
  [[ -n "${TOOL_CALL_PARSER:-}" ]] && args+=(--tool-call-parser "$TOOL_CALL_PARSER")
fi

if [[ -n "${VLLM_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra_args=($VLLM_EXTRA_ARGS)
  args+=("${extra_args[@]}")
fi

echo "[vllm] Serving $MODEL_ID as $SERVED_MODEL_NAME on $VLLM_HOST:$VLLM_PORT"
echo "[vllm] Command: vllm ${args[*]}"
exec vllm "${args[@]}"
