#!/usr/bin/env python3
"""
Visual Validation Helper for evaluate.sh

Takes a screenshot of a running site using Playwright, then evaluates it
with Azure OpenAI Vision to determine if it shows a properly rendered website.

Usage:
    python3 visual_validate.py \
        --url http://localhost:4000/ \
        --screenshot-path /path/to/screenshot.png \
        --repo-id user/repo \
        --output-json /path/to/visual_result.json

Dependencies (fail hard if missing):
    - playwright  (pip install playwright && playwright install chromium)
    - openai      (pip install openai)

Environment variables (from .env or shell):
    - AZURE_OPENAI_API_KEY
    - AZURE_OPENAI_ENDPOINT
    - AZURE_DEPLOYMENT  (default: gpt-4o)
"""

import argparse
import base64
import json
import sys
from pathlib import Path

# -------------------------
# Hard dependency checks
# -------------------------

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright is not installed.", file=sys.stderr)
    print("Install with: pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(1)

try:
    from openai import AzureOpenAI
except ImportError:
    print("ERROR: openai is not installed.", file=sys.stderr)
    print("Install with: pip install openai", file=sys.stderr)
    sys.exit(1)

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

SCREENSHOT_TIMEOUT = 30000  # ms


# -------------------------
# Screenshot
# -------------------------

def take_screenshot(url: str, output_path: Path) -> bool:
    """Take a screenshot of the URL using headless Chromium via Playwright."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=SCREENSHOT_TIMEOUT)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # continue even if networkidle times out

            # Short pause for late rendering
            try:
                page.wait_for_timeout(1000)
            except Exception:
                import time
                time.sleep(1)

            page.screenshot(path=str(output_path))
            return True
        except Exception as e:
            print(f"ERROR: Screenshot failed: {e}", file=sys.stderr)
            return False
        finally:
            page.close()
            browser.close()


# -------------------------
# Azure OpenAI Vision
# -------------------------

def encode_image_base64(image_path: Path) -> str:
    """Encode image to base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def evaluate_screenshot(screenshot_path: Path, repo_id: str, client: AzureOpenAI, model: str) -> dict:
    """
    Evaluate a screenshot using Azure OpenAI Vision.

    Returns:
        dict with 'is_valid' (bool) and 'reason' (str), or None if evaluation failed.
    """
    if not screenshot_path.exists():
        print(f"ERROR: Screenshot not found: {screenshot_path}", file=sys.stderr)
        return None

    try:
        base64_image = encode_image_base64(screenshot_path)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a QA engineer verifying website deployments.",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Does this screenshot show a properly rendered, valid website? "
                                "It should NOT be a blank page, a generic 'Index of /' directory listing, "
                                "a 404 error, or a broken code view. "
                                "Return JSON: { 'is_valid': boolean, 'reason': string }."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}",
                            },
                        },
                    ],
                },
            ],
            response_format={"type": "json_object"},
        )

        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        print(f"ERROR: [{repo_id}] Evaluation failed: {e}", file=sys.stderr)
        return None


# -------------------------
# Main
# -------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Take a screenshot and evaluate it with Azure OpenAI Vision."
    )
    parser.add_argument("--url", required=True, help="URL to screenshot (e.g. http://localhost:4000/)")
    parser.add_argument("--screenshot-path", required=True, type=Path, help="Output path for the screenshot PNG")
    parser.add_argument("--repo-id", required=True, help="Repository identifier (for logging)")
    parser.add_argument("--output-json", required=True, type=Path, help="Output path for the visual validation JSON")
    args = parser.parse_args()

    # ---- Azure OpenAI credentials (fail hard) ----
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not api_key or not endpoint:
        print("ERROR: AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set.", file=sys.stderr)
        sys.exit(1)

    model = os.getenv("AZURE_DEPLOYMENT", "gpt-4.1")
    client = AzureOpenAI(
        api_key=api_key,
        api_version="2024-02-15-preview",
        azure_endpoint=endpoint,
    )

    # ---- Step 1: Screenshot ----
    print(f"[visual] Taking screenshot of {args.url} ...")
    if not take_screenshot(args.url, args.screenshot_path):
        print("ERROR: Screenshot capture failed.", file=sys.stderr)
        sys.exit(1)
    print(f"[visual] Screenshot saved: {args.screenshot_path}")

    # ---- Step 2: Evaluate ----
    print(f"[visual] Evaluating screenshot with Azure OpenAI Vision ({model}) ...")
    result = evaluate_screenshot(args.screenshot_path, args.repo_id, client, model)

    if result is None:
        print("ERROR: Visual evaluation returned no result.", file=sys.stderr)
        sys.exit(1)

    # ---- Step 3: Write output ----
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    is_valid = result.get("is_valid", False)
    reason = result.get("reason", "")
    status = "VALID ✓" if is_valid else "INVALID ✗"
    print(f"[visual] {status}: {reason}")
    print(f"[visual] Result saved: {args.output_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
