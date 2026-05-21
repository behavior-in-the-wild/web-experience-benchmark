#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-Qwen/Qwen3-Coder-Next}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen3-coder-next}"
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_coder}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}"
# A100 server-mode default is 2048 — far too low for 50+ concurrent agents.
# 65536 lets vLLM prefill ~40 requests in a single step.
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-65536}"

exec "$SCRIPT_DIR/serve_model.sh"
