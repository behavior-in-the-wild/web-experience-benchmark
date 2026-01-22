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
import webbrowser

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

    "Hugo": [
        # Hugo (source) – prefers hugo server if config exists
        {"check": "hugo.toml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "hugo.yaml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "hugo.yml",  "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "config.toml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "config.yaml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "config.yml",  "commands": ["hugo server -p {port} --bind 0.0.0.0"]},

        # Hugo (built output) – serve common output dirs
        {"check": "public/index.html", "commands": ["python -m http.server {port} --directory public"]},
        {"check": "docs/index.html",   "commands": ["python -m http.server {port} --directory docs"]},
        {"check": "index.html",        "commands": ["python -m http.server {port}"]},
    ],

    "Express": [
        # Express apps are Node servers; start via npm when possible
        {"check": "package.json", "commands": ["npm install", "npm run start -- --port {port}"]},

        # Common direct entry files (fallback). We set PORT for apps that read env.
        {"check": "server.js", "commands": ["npm install", "cmd /c \"set PORT={port}&& node server.js\""]},
        {"check": "app.js",    "commands": ["npm install", "cmd /c \"set PORT={port}&& node app.js\""]},
        {"check": "index.js",  "commands": ["npm install", "cmd /c \"set PORT={port}&& node index.js\""]},

        # Backend subdir patterns (common in GH Pages repos with backend folder)
        {"check": "backend/package.json", "commands": ["cd backend && npm install", "cmd /c \"cd backend && set PORT={port}&& npm run start\""]},
        {"check": "backend/server.js",    "commands": ["cd backend && npm install", "cmd /c \"cd backend && set PORT={port}&& node server.js\""]},
    ],

    "Next.js": [
        # Next source app (root)
        {"check": "package.json", "commands": ["npm install", "npm run build", "npm run start -- -p {port}"]},

        # Next monorepo patterns
        {"check": "website/package.json", "commands": ["cd website && npm install", "cd website && npm run build", "cd website && npm run start -- -p {port}"]},
        {"check": "web/package.json",     "commands": ["cd web && npm install", "cd web && npm run build", "cd web && npm run start -- -p {port}"]},

        # Next static export output (commonly out/)
        {"check": "out/index.html", "commands": ["python -m http.server {port} --directory out"]},

        # Next committed build output only (has _next/ but no out/): serve repo root
        {"check": "_next/static", "commands": ["python -m http.server {port}"]},
        {"check": "index.html",   "commands": ["python -m http.server {port}"]},
    ],

    "React": [
        # CRA (react-scripts) – standard
        {"check": "package.json", "commands": ["npm install", "cmd /c \"set PORT={port}&& npm start\""]},

        # Vite (common) – must pass --port explicitly
        {"check": "vite.config.ts", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        {"check": "vite.config.js", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},

        # Built React outputs (serve dist/ or build/)
        {"check": "dist/index.html",  "commands": ["python -m http.server {port} --directory dist"]},
        {"check": "build/index.html", "commands": ["python -m http.server {port} --directory build"]},

        # Fallback: plain static
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],

    "Vue.js": [
        # Vue CLI (vue-cli-service)
        {"check": "package.json", "commands": ["npm install", "cmd /c \"set PORT={port}&& npm run serve\""]},

        # Vite (Vue) – explicit port + host
        {"check": "vite.config.ts", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        {"check": "vite.config.js", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},

        # Built Vue outputs (most common on github.io)
        {"check": "dist/index.html",  "commands": ["python -m http.server {port} --directory dist"]},

        # Fallback static
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],

    "Pelican": [
        # Pelican source repo – build then serve output
        {
            "check": "pelicanconf.py",
            "commands": [
                "pip install -r requirements.txt",
                "pelican content",
                "python -m http.server {port} --directory output",
            ],
        },

        # Alternate publish config (common)
        {
            "check": "publishconf.py",
            "commands": [
                "pip install -r requirements.txt",
                "pelican content -s publishconf.py",
                "python -m http.server {port} --directory output",
            ],
        },

        # Already-built Pelican output committed
        {"check": "output/index.html", "commands": ["python -m http.server {port} --directory output"]},

        # Fallback static
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],

    "Quarto": [
        # Quarto source project – render then serve
        {
            "check": "_quarto.yml",
            "commands": [
                "quarto render",
                "python -m http.server {port} --directory _site",
            ],
        },
        {
            "check": "_quarto.yaml",
            "commands": [
                "quarto render",
                "python -m http.server {port} --directory _site",
            ],
        },

        # Default Quarto output directory
        {"check": "_site/index.html", "commands": ["python -m http.server {port} --directory _site"]},

        # Sometimes Quarto outputs directly to docs/ for GH Pages
        {"check": "docs/index.html", "commands": ["python -m http.server {port} --directory docs"]},

        # Fallback static
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],

    "Flask": [
        # Canonical Flask app.py
        {
            "check": "app.py",
            "commands": [
                "pip install -r requirements.txt",
                "cmd /c \"set FLASK_APP=app.py&& set FLASK_ENV=development&& set FLASK_RUN_PORT={port}&& flask run --host=0.0.0.0\"",
            ],
        },

        # Alternative entry point
        {
            "check": "wsgi.py",
            "commands": [
                "pip install -r requirements.txt",
                "cmd /c \"set FLASK_APP=wsgi.py&& set FLASK_ENV=development&& set FLASK_RUN_PORT={port}&& flask run --host=0.0.0.0\"",
            ],
        },

        # Flask repo that actually commits static output (rare but seen)
        {"check": "static/index.html", "commands": ["python -m http.server {port} --directory static"]},
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
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=5)
        elif os.name == "nt":
            # Use taskkill to terminate the process tree on Windows
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            try:
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
        else:
            process.kill()
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

    print(f"Requested framework: {framework}")

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
        popen_kwargs = dict(
            shell=True,
            cwd=str(repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if os.name == "posix":
            popen_kwargs["preexec_fn"] = os.setsid
        elif os.name == "nt":
            # Windows: create a new process group so we can terminate it with taskkill
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        process = subprocess.Popen(serve_command, **popen_kwargs)

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


def take_screenshot(url: str, output_path: Path, browser, console_log_path: Path = None) -> bool:
    """Take a screenshot of the URL using playwright and optionally log console output.

    If `console_log_path` is provided, console messages will be appended to that file.
    """
    if not PLAYWRIGHT_AVAILABLE or browser is None:
        return False

    try:
        page = browser.new_page()

        if console_log_path is not None:
            console_log_path.parent.mkdir(parents=True, exist_ok=True)

            with open(console_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n=== Console log for: {url} ===\n")

            def _on_console(msg):
                try:
                    parts = []

                    # Console event type: log / error / warn / info / debug / clear
                    parts.append(f"type={msg.type()}")

                    # Text payload (often empty)
                    text = msg.text()
                    if text:
                        parts.append(f"text={text}")

                    # Arguments (important: object logs live here)
                    try:
                        for i, arg in enumerate(msg.args()):
                            try:
                                val = arg.json_value()
                                parts.append(f"arg[{i}]={val}")
                            except Exception:
                                parts.append(f"arg[{i}]=<non-serializable>")
                    except Exception:
                        pass

                    # Source location
                    try:
                        loc = msg.location()
                        if loc and loc.get("url"):
                            parts.append(
                                f"at {loc.get('url')}:{loc.get('lineNumber')}"
                            )
                    except Exception:
                        pass

                    with open(console_log_path, "a", encoding="utf-8") as f:
                        f.write("[console] " + " | ".join(parts) + "\n")

                except Exception:
                    pass

            # Register console handler
            page.on("console", _on_console)

            # Also capture page errors and failed requests/responses to the same log
            def _on_page_error(err):
                try:
                    with open(console_log_path, "a", encoding="utf-8") as f:
                        f.write(f"[pageerror] {err}\n")
                except Exception:
                    pass

            def _on_request_failed(request):
                try:
                    failure = None
                    try:
                        failure = request.failure
                    except Exception:
                        pass
                    with open(console_log_path, "a", encoding="utf-8") as f:
                        f.write(f"[requestfailed] {request.url} {getattr(failure, 'error_text', '')}\n")
                except Exception:
                    pass

            def _on_response(response):
                try:
                    status = response.status
                    if status >= 400:
                        with open(console_log_path, "a", encoding="utf-8") as f:
                            f.write(f"[response] {response.url} status={status}\n")
                except Exception:
                    pass

            page.on("pageerror", _on_page_error)
            page.on("requestfailed", _on_request_failed)
            page.on("response", _on_response)
        page.goto(url, timeout=SCREENSHOT_TIMEOUT)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # Continue even if networkidle times out

        # Give the page a short moment to emit any late console messages
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
        page.screenshot(path=str(output_path))
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
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        return result
    except Exception as e:
        logger.error(f"[{repo_id}] Evaluation failed: {e}")
        return None


def process_single_repo(entry: dict, output_dir: Path, source_dataset: str, browser=None, evaluate: bool = True, interactive: bool = False, hold_seconds: int = 10) -> dict:
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
    if (source_dataset == "stack"):
        repo_id = entry.get("repo_name")
    elif (source_dataset == "gh25"):
        repo_id = entry.get("repo_id")
    
    framework = entry.get("framework", "Static HTML")

    print(f"Processing repo: {repo_id} with framework: {framework}")
    
    if not repo_id:
        return {"repo_id": None, "status": "error", "error": "Missing repo_id"}
    
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
        repo_url = f"https://github.com/{repo_id}.git"
        logger.info(f"[{repo_id}] Step 1/5: Cloning repository...")
        
        if not git_clone(repo_url, tmpdir):
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
        
        # If interactive: open the URL in the user's default browser and wait
        url = f"http://localhost:{port}"
        if interactive:
            try:
                logger.info(f"[{repo_id}] Interactive mode: opening {url} in browser for {hold_seconds}s")
                webbrowser.open(url)
                time.sleep(hold_seconds)
            except Exception as e:
                logger.warning(f"[{repo_id}] Interactive wait failed: {e}")

        # Step 3/5: Take screenshot
        screenshot_path = output_dir / f"{safe_name}.png"
        
        if browser:
            logger.info(f"[{repo_id}] Step 3/5: Taking screenshot...")
            # Save console logs in a dedicated folder next to screenshots
            console_log_dir = output_dir.parent / "console_logs"
            console_log_dir.mkdir(parents=True, exist_ok=True)
            console_log_path = console_log_dir / f"{safe_name}.console.log"
            if take_screenshot(url, screenshot_path, browser, console_log_path=console_log_path):
                result["screenshot_path"] = str(screenshot_path)
                result["console_log"] = str(console_log_path)
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
                            result["status"] = "failed_visual_verification"
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
        default="stack_deploy_results",
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
        help="Number of parallel worker threads (default: 1). Note: screenshots require browser, so use 1 for screenshots."
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Open the deployed site in the default browser and hold for a few seconds before cleanup. Use only with single worker."
    )
    parser.add_argument(
        "--hold-seconds",
        type=int,
        default=0,
        help="Number of seconds to keep the site open in the browser when --interactive is used (default: 10)."
    )
    parser.add_argument(
        "--source-dataset",
        type=str,
        required=True,
        help="Source dataset to use (stack or gh25)"
    )
    
    args = parser.parse_args()
    
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
    logger.info(f"Processing {total} repos from {input_path}")
    
    # Thread-safe counters
    results = []
    results_lock = threading.Lock()
    counters = {"success": 0, "clone_failed": 0, "deploy_failed": 0, "error": 0}
    
    # Initialize browser if needed (only for single-threaded mode)
    browser = None
    playwright_context = None
    
    if args.workers > 1 and not args.no_screenshots:
        logger.warning("Multi-threading with screenshots is not recommended. Screenshots will be skipped.")
        args.no_screenshots = True

    if args.workers > 1 and args.interactive:
        logger.warning("Interactive mode requires single-threaded mode. Disabling interactive.")
        args.interactive = False
    
    if PLAYWRIGHT_AVAILABLE and not args.no_screenshots:
        try:
            playwright_context = sync_playwright().start()
            browser = playwright_context.chromium.launch()
            logger.info("Browser initialized for screenshots")
        except Exception as e:
            logger.warning(f"Could not initialize browser: {e}")
            browser = None
    
    source_dataset = args.source_dataset
    def process_one(entry: dict) -> dict:
        """Process a single entry (for threading)."""
        result = process_single_repo(
            entry,
            screenshots_dir,
            source_dataset,
            browser,
            evaluate=not args.no_eval,
            interactive=args.interactive,
            hold_seconds=args.hold_seconds,
        )
        
        with results_lock:
            results.append(result)
            status = result.get("status", "error")
            if status in counters:
                counters[status] += 1
        
        return result
    
    try:
        if args.workers > 1:
            # Multi-threaded processing
            logger.info(f"Using {args.workers} worker threads")
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(process_one, entry): entry for entry in entries}
                
                with tqdm(total=total, desc="Testing deployments", unit="repo") as pbar:
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
        # Cleanup browser
        if browser:
            browser.close()
        if playwright_context:
            playwright_context.stop()
    
    # Save results
    results_file = output_dir / "results.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    
    # Save successful deployments as JSONL
    success_file = output_dir / "successful_deployments.jsonl"
    with open(success_file, "w") as f:
        for r in results:
            if r["status"] == "success":
                f.write(json.dumps(r) + "\n")
    
    # Summary
    logger.info("=" * 50)
    logger.info(f"✅ Done — {counters['success']}/{total} repos deployed successfully")
    logger.info(f"   Clone failed: {counters['clone_failed']}")
    logger.info(f"   Deploy failed: {counters['deploy_failed']}")
    if counters['error'] > 0:
        logger.info(f"   Errors: {counters['error']}")
    logger.info(f"Results saved to: {results_file}")
    logger.info(f"Screenshots saved to: {screenshots_dir}")
    
    return 0


if __name__ == "__main__":
    exit(main())
