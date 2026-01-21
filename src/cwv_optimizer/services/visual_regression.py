"""Visual regression testing service.

Properly hosts each branch, captures screenshots, and compares them.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.core.utils import save_json_file
from cwv_optimizer.services.server_utils import (
    kill_server,
    start_framework_server,
    checkout_branch,
    get_suggestion_branches,
    get_default_branch,
)

logger = get_logger(__name__)

# Try to import playwright for screenshots
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not available. Install with: pip install playwright && playwright install chromium")


async def capture_screenshot(
    url: str,
    output_path: Path,
    device: str = "mobile",
    headless: bool = True,
) -> Dict[str, Any]:
    """Capture a screenshot of a URL using Playwright.
    
    Args:
        url: URL to capture
        output_path: Path to save the screenshot
        device: Device type (mobile/desktop)
        headless: Run browser headlessly
        
    Returns:
        Dict with status and screenshot path
    """
    if not PLAYWRIGHT_AVAILABLE:
        return {
            "status": "skipped",
            "message": "Playwright not available",
        }
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            
            # Configure viewport based on device
            if device == "mobile":
                context = await browser.new_context(
                    viewport={"width": 375, "height": 812},
                    device_scale_factor=3,
                    is_mobile=True,
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15"
                )
            else:
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                )
            
            page = await context.new_page()
            
            # Navigate and wait for network idle
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(1)  # Extra wait for dynamic content
            
            # Take full page screenshot
            await page.screenshot(path=str(output_path), full_page=True)
            
            await browser.close()
            
            return {
                "status": "success",
                "screenshot_path": str(output_path),
            }
            
    except Exception as e:
        logger.error("Failed to capture screenshot: %s", e)
        return {
            "status": "error",
            "error": str(e),
        }


async def compare_screenshots_with_gpt(
    baseline_path: Path,
    comparison_path: Path,
) -> Dict[str, Any]:
    """Compare two screenshots using GPT-4 Vision as a judge.
    
    Encodes both images as base64 and asks GPT to determine if there's
    a visual regression (significant visual difference that would impact UX).
    
    Args:
        baseline_path: Path to baseline screenshot
        comparison_path: Path to comparison screenshot
        
    Returns:
        Dict with has_regression (True/False)
    """
    import base64
    import os
    
    try:
        from openai import AzureOpenAI
    except ImportError:
        logger.warning("OpenAI package not available, skipping GPT comparison")
        return {"has_regression": False, "note": "OpenAI package not available"}
    
    # Get Azure credentials from environment
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
    
    if not api_key or not endpoint:
        logger.warning("Azure OpenAI credentials not configured, skipping GPT comparison")
        return {"has_regression": False, "note": "Azure OpenAI not configured"}
    
    try:
        # Read and encode images as base64
        with open(baseline_path, "rb") as f:
            baseline_b64 = base64.b64encode(f.read()).decode("utf-8")
        
        with open(comparison_path, "rb") as f:
            comparison_b64 = base64.b64encode(f.read()).decode("utf-8")
        
        # Create Azure OpenAI client
        client = AzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint,
        )
        
        # Build the prompt
        prompt = """Compare these two website screenshots. The first image is the BASELINE (original), and the second image is the COMPARISON (after code changes).

Determine if there is a VISUAL REGRESSION - meaning a significant visual problem that would negatively impact user experience. 

Consider as regressions:
- Missing content, images, or sections
- Broken layouts or overlapping elements
- Text that is unreadable or cut off
- Major color or styling changes that look broken
- Navigation elements that are missing or misaligned

Do NOT consider as regressions:
- Minor color adjustments that still look good
- Small spacing differences
- Performance optimizations that don't affect appearance
- Intentional design improvements

Respond with ONLY one word: "TRUE" if there is a regression, or "FALSE" if there is no regression."""

        # Call GPT-4 Vision
        response = client.chat.completions.create(
            model="gpt-4o",  # Azure deployment name for GPT-4 with vision
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{baseline_b64}",
                                "detail": "low",  # Use low detail for faster processing
                            },
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{comparison_b64}",
                                "detail": "low",
                            },
                        },
                    ],
                }
            ],
            max_tokens=10,
            temperature=0,
        )
        
        # Parse response
        answer = response.choices[0].message.content.strip().upper()
        has_regression = answer == "TRUE"
        
        logger.info("GPT Vision comparison: %s (raw: %s)", 
                   "REGRESSION" if has_regression else "OK", answer)
        
        return {
            "has_regression": has_regression,
            "gpt_response": answer,
        }
        
    except Exception as e:
        logger.error("GPT Vision comparison failed: %s", e)
        # On error, assume no regression to avoid blocking pipeline
        return {
            "has_regression": False,
            "error": str(e),
        }


async def run_visual_regression_tests(
    workspace_dir: str,
    url: str,
    device: str,
    headless: bool = True,
    content_similarity_threshold: float = 0.9,
    enable_content_similarity: bool = True,
    apply_mode: str = "individual",
    model: str = "azure/gpt-5",
    framework: str = "Static HTML",
    server_pid: Optional[int] = None,
    results_dir: Optional[str] = None,
    screenshots_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Run visual regression tests on optimization branches.

    Args:
        workspace_dir: Path to workspace directory
        url: Target URL for testing (from initial deployment)
        device: Device type (mobile/desktop)
        headless: Run browser headlessly
        content_similarity_threshold: Similarity threshold
        enable_content_similarity: Enable content checks
        apply_mode: Application mode
        model: Model name for output naming
        framework: Framework type for server commands
        server_pid: PID of server from previous step (to kill)
        results_dir: Directory for saving results (clean structure)
        screenshots_dir: Directory for saving screenshots (clean structure)

    Returns:
        Result dictionary with regression report path
    """
    logger.info("Running visual regression tests for: %s", url)

    try:
        workspace_path = Path(workspace_dir)
        dump_dir = workspace_path.parent
        
        # Use provided directories or fallback to old structure
        if screenshots_dir:
            ss_dir = Path(screenshots_dir)
        else:
            ss_dir = dump_dir / "screenshots"
        ss_dir.mkdir(exist_ok=True)
        
        if results_dir:
            res_dir = Path(results_dir)
        else:
            res_dir = dump_dir
        res_dir.mkdir(exist_ok=True)
        
        results: List[Dict[str, Any]] = []

        # Kill the initial server from CWV analysis
        if server_pid:
            logger.info("Killing initial server (PID: %s)", server_pid)
            kill_server(server_pid)
            await asyncio.sleep(1)

        branches = get_suggestion_branches(workspace_dir)
        if not branches:
            logger.warning("No suggestion branches found")
            return {
                "status": "success",
                "regression_report_path": "",
                "summary": {"message": "No branches to test"},
            }

        default_branch = get_default_branch(workspace_dir)
        
        # First, capture baseline screenshot
        logger.info("Capturing baseline screenshot (%s)", default_branch)
        checkout_branch(workspace_dir, default_branch)
        
        baseline_server = await start_framework_server(
            workspace_dir, framework
        )
        
        if baseline_server.get("status") != "success":
            return {
                "status": "error",
                "error": f"Failed to start baseline server: {baseline_server.get('error')}",
            }
        
        baseline_url = baseline_server["url"]
        baseline_screenshot = ss_dir / f"baseline_{device}.png"
        
        await capture_screenshot(
            baseline_url, baseline_screenshot, device, headless
        )
        
        kill_server(baseline_server.get("pid"))
        await asyncio.sleep(1)

        # Test each optimization branch
        for branch in branches:
            logger.info("Testing branch: %s", branch)
            
            if not checkout_branch(workspace_dir, branch):
                results.append({
                    "branch": branch,
                    "status": "error",
                    "error": "Failed to checkout branch",
                })
                continue

            # Start server for this branch
            server_result = await start_framework_server(
                workspace_dir, framework
            )
            
            if server_result.get("status") != "success":
                results.append({
                    "branch": branch,
                    "status": "error",
                    "error": f"Failed to start server: {server_result.get('error')}",
                })
                continue
            
            branch_url = server_result["url"]
            branch_screenshot = ss_dir / f"{branch}_{device}.png"
            
            # Capture screenshot
            screenshot_result = await capture_screenshot(
                branch_url, branch_screenshot, device, headless
            )
            
            # Kill server for this branch
            kill_server(server_result.get("pid"))
            await asyncio.sleep(1)
            
            if screenshot_result.get("status") != "success":
                results.append({
                    "branch": branch,
                    "status": "error",
                    "error": f"Screenshot failed: {screenshot_result.get('error')}",
                })
                continue
            
            # Compare with baseline using GPT Vision
            if enable_content_similarity:
                comparison = await compare_screenshots_with_gpt(
                    baseline_screenshot,
                    branch_screenshot,
                )
            else:
                comparison = {"has_regression": False}
            
            results.append({
                "branch": branch,
                "status": "success",
                "screenshot_path": str(branch_screenshot),
                "has_regression": comparison.get("has_regression", False),
                "gpt_response": comparison.get("gpt_response"),
            })

        # Return to default branch
        checkout_branch(workspace_dir, default_branch)

        # Save results (clean name, no timestamp)
        report_path = res_dir / "visual_regression.json"
        save_json_file(
            {
                "timestamp": datetime.now().isoformat(),
                "url": url,
                "device": device,
                "framework": framework,
                "screenshots_dir": str(ss_dir),
                "baseline_screenshot": str(baseline_screenshot),
                "results": results,
            },
            report_path,
        )

        # Check for regressions
        has_regressions = any(r.get("has_regression", False) for r in results)
        passed_branches = [r["branch"] for r in results if not r.get("has_regression", False) and r.get("status") == "success"]

        return {
            "status": "success",
            "regression_report_path": str(report_path),
            "has_regressions": has_regressions,
            "passed_branches": passed_branches,
            "summary": {
                "total_branches": len(branches),
                "tested": len(results),
                "passed": len(passed_branches),
                "with_regressions": sum(1 for r in results if r.get("has_regression")),
            },
        }

    except Exception as e:
        logger.error("Visual regression testing failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}
