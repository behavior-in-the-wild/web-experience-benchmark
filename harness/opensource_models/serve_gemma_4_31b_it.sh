#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export MODEL_ID="${MODEL_ID:-google/gemma-4-31B-it}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-gemma-4-31b-it}"
export TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-gemma4}"
export MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}"
# Gemma 4 is multimodal; vLLM forces --disable_chunked_mm_input which requires
# max_num_batched_tokens >= max_tokens_per_mm_item (2496). Set generously.
export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-32768}"
export REASONING_PARSER="${REASONING_PARSER:-gemma4}"
# Inject <|think|> (token 98) into the system turn so the model enters thinking mode.
# Without this flag the reasoning parser is wired up but thinking is never triggered.
if [[ -z "${DEFAULT_CHAT_TEMPLATE_KWARGS:-}" ]]; then
  export DEFAULT_CHAT_TEMPLATE_KWARGS='{"enable_thinking": true}'
fi

exec "$SCRIPT_DIR/serve_model.sh"
