#!/bin/bash

# Configuration for Azure OpenAI (VLM)
# ------------------------------------------------------------------------------
# Source .env if it exists
if [ -f .env ]; then
    echo "Sourcing .env..."
    set -a
    source .env
    set +a
fi

# Check for required variables
if [ -z "$AZURE_OPENAI_API_KEY" ]; then
    echo "Error: AZURE_OPENAI_API_KEY is not set. Please set it in .env or environment."
    exit 1
fi

# Script Settings
# ------------------------------------------------------------------------------
OUTPUT_FILE="webpage_classifications_vlm.jsonl"
SCREENSHOT_DIR=".cache/screenshots"
# Number of parallel rows to process
# Each worker requires its own Playwright browser instance.
# 5 is a conservative default. Increase if you have sufficient RAM/CPU.
VLM_WORKERS=5

# Run the classification script
# ------------------------------------------------------------------------------
# Uses .venvpython if available, else system python
PYTHON_CMD=".venv/bin/python"
if [ ! -f "$PYTHON_CMD" ]; then
    PYTHON_CMD="python3"
fi

# Limit setting (default to 0 = all)
LIMIT=${1:-0}

echo "Starting VLM classification run..."
echo "Limit: $LIMIT (dataset rows)"
echo "Output: $OUTPUT_FILE"
echo "Screenshots: $SCREENSHOT_DIR"
echo "Workers: $VLM_WORKERS"

$PYTHON_CMD scripts/helper_scripts/classify_webpages.py \
    --vlm \
    --vlm-resume \
    --limit "$LIMIT" \
    --max-vlm-calls 1000000 \
    --vlm-workers "$VLM_WORKERS" \
    --output "$OUTPUT_FILE" \
    --screenshot-dir "$SCREENSHOT_DIR"

echo "Done."
