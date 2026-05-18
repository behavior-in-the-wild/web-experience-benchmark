#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-Qwen/Qwen3-Coder-Next}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-qwen3-coder-next}"
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_coder}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"

exec "$SCRIPT_DIR/serve_model.sh"
