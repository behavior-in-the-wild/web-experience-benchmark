#!/usr/bin/env python3
"""
Stack-Based Deployment Tester

Clones repos, runs standard serve commands based on their framework type 
(Hexo, Jekyll, Static HTML), checks if deployment succeeds, captures screenshots,
and evaluates them using Azure OpenAI Vision.

Usage:
    python scripts/stack_deploy_test.py -i <input_jsonl> -o <output_dir>
"""

import json
import subprocess
import os
import shutil
import tempfile
import time
import signal
import argparse
import logging
import base64
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Optional: Azure OpenAI for screenshot evaluation
try:
    from openai import AzureOpenAI
    AZURE_OPENAI_AVAILABLE = True
except ImportError:
    AZURE_OPENAI_AVAILABLE = False

# Optional: playwright for screenshots
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("Warning: playwright not installed. Screenshots will be skipped.")
    print("Install with: pip install playwright && playwright install chromium")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------
# Azure OpenAI Client Setup
# ------------------------

openai_client = None
if AZURE_OPENAI_AVAILABLE:
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if api_key and endpoint:
        openai_client = AzureOpenAI(
            api_key=api_key,
            api_version="2024-02-15-preview",
            azure_endpoint=endpoint
        )
        DEPLOYMENT_NAME = os.getenv("AZURE_DEPLOYMENT", "gpt-4o")
    else:
        logger.warning("Azure OpenAI credentials not found. Screenshot evaluation disabled.")

# ------------------------
# Serve Commands by Framework
# ------------------------

FRAMEWORK_COMMANDS = {
    "Hexo": [
        # Hexo serve (if package.json exists with hexo)
        {"check": "package.json", "commands": ["npm install", "npx hexo server -p {port}"]},
        # Built hexo site (just static files)
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],
    "Jekyll": [
        # Jekyll with Bundler
        {"check": "Gemfile", "commands": ["bundle install", "bundle exec jekyll serve --port {port}"]},
        # Jekyll without Bundler
        {"check": "_config.yml", "commands": ["jekyll serve --port {port}"]},
    ],
    "Static HTML": [
        # Simple Python HTTP server for static files
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],
}

# Default port
DEFAULT_PORT = 8080

# Timeouts
INSTALL_TIMEOUT = 120  # 2 min for npm/bundle install
SERVE_TIMEOUT = 30     # Wait for server startup
SCREENSHOT_TIMEOUT = 30000  # ms


# ------------------------
# Utility Functions
# ------------------------

def git_clone(repo_url: str, dst: Path) -> bool:
    """Clone a repository with depth 1."""
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dst)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        return dst.exists() and any(dst.iterdir())
    except Exception as e:
        logger.error(f"Clone failed: {e}")
        return False


def find_available_port(start_port: int = 8080) -> int:
    """Find an available port starting from start_port."""
    import socket
    port = start_port
    for _ in range(100):  # Try up to 100 ports
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    return start_port  # Fallback


def kill_process_tree(process: subprocess.Popen):
    """Kill process and all its children."""
    if process is None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


def check_server_health(port: int, timeout: int = 10) -> bool:
    """Check if server is responding on the given port."""
    import urllib.request
    import urllib.error
    
    start_time = time.time()
    url = f"http://localhost:{port}"
    
    while time.time() - start_time < timeout:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, Exception):
            time.sleep(0.5)
    return False


# ------------------------
# Main Deployment Logic
# ------------------------

def get_serve_commands(framework: str, repo_path: Path, port: int) -> list:
    """
    Get the appropriate serve commands for a framework.
    Returns list of (install_cmd, serve_cmd) or None if not applicable.
    """
    if framework not in FRAMEWORK_COMMANDS:
        # Default to static HTML serving
        framework = "Static HTML"
    
    for config in FRAMEWORK_COMMANDS.get(framework, []):
        check_file = config.get("check")
        if check_file and not (repo_path / check_file).exists():
            continue
        
        commands = config.get("commands", [])
        # Replace {port} placeholder
        commands = [cmd.replace("{port}", str(port)) for cmd in commands]
        return commands
    
    # Fallback: Python HTTP server
    return [f"python -m http.server {port}"]


def run_deployment(repo_path: Path, framework: str, port: int) -> tuple:
    """
    Run deployment for a repo.
    Returns (success: bool, process: Popen or None, error: str or None)
    """
    commands = get_serve_commands(framework, repo_path, port)
    
    if not commands:
        return False, None, "No valid serve commands found"
    
    # Run install commands first (all except the last one)
    install_commands = commands[:-1]
    serve_command = commands[-1]
    
    for cmd in install_commands:
        logger.info(f"  Running: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=str(repo_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=INSTALL_TIMEOUT,
            )
            if result.returncode != 0:
                error_msg = result.stderr.decode("utf-8", errors="ignore")[:500]
                return False, None, f"Install failed: {error_msg}"
        except subprocess.TimeoutExpired:
            return False, None, f"Install timeout: {cmd}"
        except Exception as e:
            return False, None, f"Install error: {e}"
    
    # Start the serve command in background
    logger.info(f"  Serving: {serve_command}")
    try:
        process = subprocess.Popen(
            serve_command,
            shell=True,
            cwd=str(repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        
        # Wait a bit for server to start
        time.sleep(3)
        
        # Check if server is running
        if check_server_health(port, timeout=SERVE_TIMEOUT):
            return True, process, None
        else:
            kill_process_tree(process)
            return False, None, "Server did not respond within timeout"
            
    except Exception as e:
        return False, None, f"Serve error: {e}"


def take_screenshot(url: str, output_path: Path, browser, viewport_width: int = 1280, max_height: int = 8000) -> bool:
    """Take a screenshot of the URL using playwright.
    
    Args:
        url: URL to screenshot
        output_path: Path to save the screenshot
        browser: Playwright browser instance
        viewport_width: Width of viewport (default 1280px for readable screenshots)
        max_height: Maximum height to capture (default 8000px to prevent extremely tall images)
    """
    if not PLAYWRIGHT_AVAILABLE or browser is None:
        return False
    
    try:
        # Create page with a reasonable viewport size
        page = browser.new_page(viewport={"width": viewport_width, "height": 800})
        page.goto(url, timeout=SCREENSHOT_TIMEOUT)
        
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # Continue even if networkidle times out
        
        # Get the actual page height
        page_height = page.evaluate("() => document.documentElement.scrollHeight")
        
        # Cap the height to prevent extremely tall screenshots
        if page_height > max_height:
            logger.debug(f"Page height {page_height}px exceeds max {max_height}px, capping screenshot")
            # Take viewport-only screenshot at the top of the page
            page.screenshot(path=str(output_path), full_page=False)
        else:
            # Take full page screenshot
            page.screenshot(path=str(output_path), full_page=True)
        
        page.close()
        return True
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        return False


# ------------------------
# Screenshot Evaluation (Azure OpenAI Vision)
# ------------------------

def encode_image_base64(image_path: Path) -> str:
    """Encode image to base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def evaluate_screenshot(screenshot_path: Path, repo_id: str) -> dict:
    """
    Evaluate a screenshot using Azure OpenAI Vision.
    
    Returns:
        dict with 'is_valid' (bool) and 'reason' (str), or None if evaluation failed.
    """
    if not openai_client or not screenshot_path.exists():
        return None
    
    try:
        base64_image = encode_image_base64(screenshot_path)
        
        response = openai_client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[
                {
                    "role": "system",
                    "content": "You are a QA engineer verifying website deployments."
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Does this screenshot show a properly rendered, valid website? "
                                    "It should NOT be a blank page, a generic 'Index of /' directory listing, "
                                    "a 404 error, or a broken code view. "
                                    "Return JSON: { 'is_valid': boolean, 'reason': string }."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            response_format={"type": "json_object"},
            timeout=60  # 60 second timeout to prevent hanging
        )
        
        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        logger.error(f"[{repo_id}] Evaluation failed: {e}")
        return None


def process_single_repo(entry: dict, output_dir: Path, browser=None, evaluate: bool = True) -> dict:
    """
    Process a single repository.
    
    Returns result dict with:
    - repo_id
    - framework
    - status: "success" | "clone_failed" | "deploy_failed"
    - screenshot_path (if successful)
    - visual_verification (if evaluation enabled)
    - error (if failed)
    """
    # Support both repo_id and repo_url/repo_name field naming
    repo_id = entry.get("repo_id")
    repo_url = entry.get("repo_url")
    repo_name = entry.get("repo_name")
    
    # Derive repo_id from repo_url if not present
    if not repo_id and repo_url:
        # Extract from URL like https://github.com/owner/repo
        parts = repo_url.rstrip("/").rstrip(".git").split("/")
        if len(parts) >= 2:
            repo_id = f"{parts[-2]}/{parts[-1]}"
    
    # Use repo_name as fallback identifier
    if not repo_id:
        repo_id = repo_name
    
    framework = entry.get("framework", "Static HTML")
    
    if not repo_id:
        return {"repo_id": None, "status": "error", "error": "Missing repo_id or repo_url"}
    
    safe_name = repo_id.replace("/", "_")
    result = {
        "repo_id": repo_id,
        "framework": framework,
        "status": "unknown",
    }
    
    # Create temp directory for cloning
    tmpdir = Path(tempfile.mkdtemp(prefix=f"deploy_test_{safe_name}_"))
    port = find_available_port()
    process = None
    
    try:
        # Step 1/5: Clone repository
        # Use provided repo_url or construct from repo_id
        clone_url = repo_url if repo_url else f"https://github.com/{repo_id}.git"
        if not clone_url.endswith(".git"):
            clone_url = clone_url + ".git"
        
        logger.info(f"[{repo_id}] Step 1/5: Cloning repository...")
        
        if not git_clone(clone_url, tmpdir):
            result["status"] = "clone_failed"
            result["error"] = "Failed to clone repository"
            logger.error(f"[{repo_id}] ✗ Clone failed")
            return result
        
        logger.info(f"[{repo_id}] Step 1/5: Clone complete ✓")
        
        # Step 2/5: Run deployment
        logger.info(f"[{repo_id}] Step 2/5: Deploying ({framework}) on port {port}...")
        success, process, error = run_deployment(tmpdir, framework, port)
        
        if not success:
            result["status"] = "deploy_failed"
            result["error"] = error
            logger.error(f"[{repo_id}] ✗ Deploy failed: {error}")
            return result
        
        logger.info(f"[{repo_id}] Step 2/5: Server running ✓")
        
        # Step 3/5: Take screenshot
        url = f"http://localhost:{port}"
        screenshot_path = output_dir / f"{safe_name}.png"
        
        if browser:
            logger.info(f"[{repo_id}] Step 3/5: Taking screenshot...")
            if take_screenshot(url, screenshot_path, browser):
                result["screenshot_path"] = str(screenshot_path)
                result["status"] = "success"
                logger.info(f"[{repo_id}] Step 3/5: Screenshot saved ✓")
                
                # Step 4/5: Evaluate screenshot using Azure OpenAI Vision
                if evaluate and openai_client:
                    logger.info(f"[{repo_id}] Step 4/5: Evaluating screenshot with Vision AI...")
                    eval_result = evaluate_screenshot(screenshot_path, repo_id)
                    if eval_result:
                        result["visual_verification"] = eval_result
                        if eval_result.get("is_valid"):
                            logger.info(f"[{repo_id}] Step 4/5: Valid website ✓ - {eval_result.get('reason', '')[:50]}")
                        else:
                            logger.warning(f"[{repo_id}] Step 4/5: Invalid ✗ - {eval_result.get('reason', '')[:50]}")
                    else:
                        logger.info(f"[{repo_id}] Step 4/5: Evaluation skipped")
                else:
                    logger.info(f"[{repo_id}] Step 4/5: Evaluation skipped (disabled)")
            else:
                result["status"] = "success"  # Deployment worked, screenshot failed
                result["screenshot_error"] = "Screenshot capture failed"
                logger.warning(f"[{repo_id}] Step 3/5: Screenshot failed ✗")
        else:
            result["status"] = "success"
            logger.info(f"[{repo_id}] Step 3/5: Screenshots disabled, skipping")
        
        # Step 5/5: Complete
        result["url"] = url
        logger.info(f"[{repo_id}] Step 5/5: Complete ✓ SUCCESS")
        
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        
    finally:
        # Cleanup
        if process:
            kill_process_tree(process)
        shutil.rmtree(tmpdir, ignore_errors=True)
    
    return result


# ------------------------
# Main Entry Point
# ------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test local deployment of Hexo/Jekyll/Static HTML repos and capture screenshots"
    )
    parser.add_argument(
        "-i", "--input",
        required=True,
        help="Path to input JSONL file with framework-detected repos"
    )
    parser.add_argument(
        "-o", "--output",
        default="stack_deploy_results_jekyll_hexo_static",
        help="Output directory for screenshots and results (default: stack_deploy_results)"
    )
    parser.add_argument(
        "-l", "--limit",
        type=int,
        default=0,
        help="Limit number of repos to process (0 = all)"
    )
    parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="Skip taking screenshots"
    )
    parser.add_argument(
        "--no-eval",
        action="store_true",
        help="Skip screenshot evaluation (Azure OpenAI Vision)"
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=1,
        help="Number of parallel worker threads (default: 1). Each thread gets its own browser for screenshots."
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous checkpoint (skip already processed repos)"
    )
    
    args = parser.parse_args()
    
    # If no screenshots, evaluation is automatically disabled (nothing to evaluate)
    if args.no_screenshots:
        args.no_eval = True
    
    input_path = Path(args.input)
    output_dir = Path(args.output)
    
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        return 1
    
    # Create output directories
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshots_dir = output_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    
    # Load entries
    entries = []
    with open(input_path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    
    if args.limit > 0:
        entries = entries[:args.limit]
    
    total = len(entries)
    logger.info(f"Loaded {total} repos from {input_path}")
    
    # Checkpoint file for incremental saving
    checkpoint_file = output_dir / "checkpoint.jsonl"
    
    # Load existing checkpoint if resuming
    processed_repo_ids = set()
    if args.resume and checkpoint_file.exists():
        logger.info(f"Resuming from checkpoint: {checkpoint_file}")
        with open(checkpoint_file, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("repo_id"):
                        processed_repo_ids.add(entry["repo_id"])
                except json.JSONDecodeError:
                    continue
        logger.info(f"Already processed: {len(processed_repo_ids)} repos")
    
    # Filter out already processed entries
    if processed_repo_ids:
        entries = [e for e in entries if e.get("repo_id") not in processed_repo_ids]
        logger.info(f"Remaining to process: {len(entries)} repos")
    
    if not entries:
        logger.info("No repos to process. All done!")
        return 0
    
    # Thread-safe counters
    results = []
    results_lock = threading.Lock()
    counters = {"success": 0, "clone_failed": 0, "deploy_failed": 0, "error": 0}
    
    # Thread-local storage for browser instances
    thread_local = threading.local()
    
    def get_thread_browser():
        """Get or create a browser for the current thread."""
        if not hasattr(thread_local, 'browser'):
            if PLAYWRIGHT_AVAILABLE and not args.no_screenshots:
                try:
                    thread_local.playwright = sync_playwright().start()
                    thread_local.browser = thread_local.playwright.chromium.launch()
                    logger.debug(f"Browser initialized for thread {threading.current_thread().name}")
                except Exception as e:
                    logger.warning(f"Could not initialize browser for thread: {e}")
                    thread_local.browser = None
                    thread_local.playwright = None
            else:
                thread_local.browser = None
                thread_local.playwright = None
        return thread_local.browser
    
    def cleanup_thread_browser():
        """Cleanup browser for current thread."""
        if hasattr(thread_local, 'browser') and thread_local.browser:
            try:
                thread_local.browser.close()
            except:
                pass
        if hasattr(thread_local, 'playwright') and thread_local.playwright:
            try:
                thread_local.playwright.stop()
            except:
                pass
    
    # Single-threaded browser (for workers=1)
    browser = None
    playwright_context = None
    
    if args.workers == 1 and PLAYWRIGHT_AVAILABLE and not args.no_screenshots:
        try:
            playwright_context = sync_playwright().start()
            browser = playwright_context.chromium.launch()
            logger.info("Browser initialized for screenshots")
        except Exception as e:
            logger.warning(f"Could not initialize browser: {e}")
            browser = None
    
    def process_one(entry: dict) -> dict:
        """Process a single entry (for threading)."""
        # Get thread-local browser if multi-threaded, else use shared browser
        if args.workers > 1:
            thread_browser = get_thread_browser()
        else:
            thread_browser = browser
        
        result = process_single_repo(entry, screenshots_dir, thread_browser, evaluate=not args.no_eval)
        
        with results_lock:
            results.append(result)
            status = result.get("status", "error")
            if status in counters:
                counters[status] += 1
            
            # Checkpoint: append result to file immediately
            with open(checkpoint_file, "a") as f:
                f.write(json.dumps(result) + "\n")
        
        return result
    
    try:
        if args.workers > 1:
            # Multi-threaded processing with per-thread browsers
            logger.info(f"Using {args.workers} worker threads (each with its own browser)")
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(process_one, entry): entry for entry in entries}
                
                with tqdm(total=len(entries), desc="Testing deployments", unit="repo") as pbar:
                    for future in as_completed(futures):
                        entry = futures[future]
                        try:
                            future.result()
                        except Exception as e:
                            logger.error(f"Error processing {entry.get('repo_id', 'unknown')}: {e}")
                        pbar.update(1)
        else:
            # Single-threaded processing (original behavior)
            for entry in tqdm(entries, desc="Testing deployments", unit="repo"):
                process_one(entry)
    
    finally:
        # Cleanup browser (handle connection errors gracefully)
        try:
            if browser:
                browser.close()
        except Exception as e:
            logger.debug(f"Browser close error (ignored): {e}")
        try:
            if playwright_context:
                playwright_context.stop()
        except Exception as e:
            logger.debug(f"Playwright stop error (ignored): {e}")
    
    # Build final results from checkpoint (includes all runs)
    all_results = []
    if checkpoint_file.exists():
        with open(checkpoint_file, "r") as f:
            for line in f:
                try:
                    all_results.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
    
    # Save final results.json
    results_file = output_dir / "results.json"
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)
    
    # Save successful deployments as JSONL
    success_file = output_dir / "successful_deployments.jsonl"
    with open(success_file, "w") as f:
        for r in all_results:
            if r.get("status") == "success":
                f.write(json.dumps(r) + "\n")
    
    # Final stats from all results
    final_counts = {"success": 0, "clone_failed": 0, "deploy_failed": 0, "error": 0}
    for r in all_results:
        status = r.get("status", "error")
        if status in final_counts:
            final_counts[status] += 1
    
    # Summary
    total_processed = len(all_results)
    logger.info("=" * 50)
    logger.info(f"✅ Done — {final_counts['success']}/{total_processed} repos deployed successfully")
    logger.info(f"   Clone failed: {final_counts['clone_failed']}")
    logger.info(f"   Deploy failed: {final_counts['deploy_failed']}")
    if final_counts['error'] > 0:
        logger.info(f"   Errors: {final_counts['error']}")
    logger.info(f"Results saved to: {results_file}")
    logger.info(f"Checkpoint: {checkpoint_file}")
    logger.info(f"Screenshots: {screenshots_dir}")
    
    return 0


if __name__ == "__main__":
    exit(main())
