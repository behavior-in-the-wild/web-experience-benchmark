#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-MiniMaxAI/MiniMax-M2.7}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-minimax-m2.7}"
# Native MiniMax-M2 tool-call and reasoning wire formats
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-minimax_m2}"
export REASONING_PARSER="${REASONING_PARSER:-minimax_m2}"
# 229B MoE BF16 (~458GB weights) on 8×A100-80GB (640GB total).
# 8 GQA KV heads / TP=8 → 1 KV head per GPU → minimal KV footprint.
# FP8 KV cache squeezes the remaining VRAM budget.
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-65536}"
export KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"
export ENABLE_EXPERT_PARALLEL="${ENABLE_EXPERT_PARALLEL:-1}"
# Cap in-flight sequences to prevent preemption-induced blank outputs under
# high parallelism; 16 GB swap gives headroom for burst traffic above MAX_NUM_SEQS.
export MAX_NUM_SEQS="${MAX_NUM_SEQS:-64}"

exec "$SCRIPT_DIR/serve_model.sh"
