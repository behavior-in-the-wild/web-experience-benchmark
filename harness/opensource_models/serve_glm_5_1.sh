#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-cyankiwi/GLM-5.1-AWQ-4bit}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-glm-5.1}"
# GLM-5.1 uses the same tool-call wire format as GLM-4.7
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-glm47}"
# 767B MoE, AWQ 4-bit (~384GB weights); 8x A100-80GB leaves ~256GB for KV cache.
# 64K context gives good concurrency headroom.
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-65536}"
export REASONING_PARSER="${REASONING_PARSER:-glm45}"

export ENABLE_EXPERT_PARALLEL="${ENABLE_EXPERT_PARALLEL:-1}"

exec "$SCRIPT_DIR/serve_model.sh"