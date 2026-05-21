#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-zai-org/GLM-4.7-Flash}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-glm-4.7-flash}"
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-glm47}"
# GLM-4.7-Flash has 20 attention heads — TP must divide 20 (max TP=4).
# PP=2 distributes the other half of layers across the remaining 4 GPUs.
export PIPELINE_PARALLEL_SIZE="${PIPELINE_PARALLEL_SIZE:-2}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-65536}"
export REASONING_PARSER="${REASONING_PARSER:-glm45}"

exec "$SCRIPT_DIR/serve_model.sh"
