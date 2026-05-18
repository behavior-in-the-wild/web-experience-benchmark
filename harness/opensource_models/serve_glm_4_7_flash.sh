#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-zai-org/GLM-4.7-Flash}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-glm-4.7-flash}"
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-glm4_moe}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"

exec "$SCRIPT_DIR/serve_model.sh"
