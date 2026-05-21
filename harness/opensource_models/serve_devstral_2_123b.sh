#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-mistralai/Devstral-2-123B-Instruct-2512}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-devstral-2-123b}"
# Ministral3/Mistral tool-call wire format
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-mistral}"
# 123B dense FP8 (static) — weights ~123GB, KV cache headroom is generous on 8×A100-80GB.
# 256K native context; 131K is a safe starting point.
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-32768}"
# KV cache in FP8 saves ~2× memory vs BF16 KV
export KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"

exec "$SCRIPT_DIR/serve_model.sh"
