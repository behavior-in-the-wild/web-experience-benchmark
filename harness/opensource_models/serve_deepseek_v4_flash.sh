#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-deepseek-ai/DeepSeek-V4-Flash}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-deepseek-v4-flash}"
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-deepseek_v4}"
export REASONING_PARSER="${REASONING_PARSER:-deepseek_v4}"
export TOKENIZER_MODE="${TOKENIZER_MODE:-deepseek_v4}"
# 284B MoE (13B activated) with MLA — KV cache is tiny (compressed latent vector),
# so 128K fits comfortably on 8×A100-80GB. Drop to 65536 if vLLM refuses to start.
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-65536}"
export KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"
export BLOCK_SIZE="${BLOCK_SIZE:-256}"
# DeepEP expert-parallel kernels require Hopper/Blackwell NVLink fabric — disabled on A100
export ENABLE_EXPERT_PARALLEL="${ENABLE_EXPERT_PARALLEL:-0}"
export VLLM_USE_DEEP_GEMM="${VLLM_USE_DEEP_GEMM:-0}"

exec "$SCRIPT_DIR/serve_model.sh"
