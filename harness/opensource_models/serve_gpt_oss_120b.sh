#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-openai/gpt-oss-120b}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-gpt-oss-120b}"
# OpenAI function-calling wire format; reasoning via GPT-OSS chain-of-thought
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-openai}"
export REASONING_PARSER="${REASONING_PARSER:-openai_gptoss}"
# 117B MoE, MXFP4 weights (~58GB) — tiny memory footprint on 8×A100-80GB.
# vLLM's gpt_oss.py handles MXFP4 loading natively from config.json.
# 36 layers, 64 attn heads / TP=8 = 8 per GPU; 8 KV heads / TP=8 = 1 per GPU.
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-32768}"
export ENABLE_EXPERT_PARALLEL="${ENABLE_EXPERT_PARALLEL:-1}"

exec "$SCRIPT_DIR/serve_model.sh"
