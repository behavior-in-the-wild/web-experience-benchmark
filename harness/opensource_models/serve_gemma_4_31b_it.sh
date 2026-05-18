#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-google/gemma-4-31B-it}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-gemma-4-31b-it}"
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-functiongemma}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"

exec "$SCRIPT_DIR/serve_model.sh"
