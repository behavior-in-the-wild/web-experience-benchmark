#!/usr/bin/env python3
"""
CWV Benchmark Script

Loads HuggingFace dataset, deploys sites locally using framework-specific commands,
measures Core Web Vitals 10 times, and updates the dataset with CWV results.

Usage:
    python scripts/helper_scripts/cwv_benchmark.py [--limit N] [--device mobile|desktop]
"""

import asyncio
import json
import subprocess
import os
import shutil
import tempfile
import time
import signal
import argparse
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

from datasets import load_dataset, Dataset
from tqdm import tqdm

# Import CWV measurement functions from performance_testing
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from cwv_optimizer.services.performance_testing import (
    measure_cwv_metrics,
    calculate_aggregated_metrics,
    DEFAULT_SETTLE_TIME,
)

# =========================
# CONFIG
# =========================
DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
SPLIT = "train"
NUM_CWV_RUNS = 15
DEFAULT_DEVICE = "mobile"

# Timeouts
INSTALL_TIMEOUT = 90   # 90s for npm/bundle install (reduced to fail faster)
SERVE_TIMEOUT = 30     # Wait for server startup

# Checkpointing
CHECKPOINT_EVERY = 5   # Save checkpoint every N successful results
DUMPS_DIR = Path(__file__).parent.parent.parent / "dumps" / "cwv_benchmark"

# Thread-safe port allocation
port_lock = threading.Lock()
allocated_ports = set()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# =========================
# FRAMEWORK COMMANDS (from stack_deploy_test.py)
# =========================

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

# =========================
# UTILITY FUNCTIONS (from stack_deploy_test.py)
# =========================

def git_clone(repo_url: str, dst: Path, commit_sha: Optional[str] = None) -> bool:
    """
    Clone a repository. If commit_sha is provided, checkout that specific commit.
    Otherwise, do a shallow clone (depth 1).
    """
    try:
        if commit_sha:
            # Full clone needed to checkout specific commit
            result = subprocess.run(
                ["git", "clone", repo_url, str(dst)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                return False
            
            # Checkout specific commit
            checkout_result = subprocess.run(
                ["git", "checkout", commit_sha],
                cwd=str(dst),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            return checkout_result.returncode == 0 and dst.exists() and any(dst.iterdir())
        else:
            # Shallow clone
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
    """Find an available port starting from start_port (thread-safe)."""
    import socket
    
    with port_lock:
        port = start_port
        for _ in range(500):  # Increased range for high concurrency
            # Skip already allocated ports
            if port in allocated_ports:
                port += 1
                continue
            
            # Check if port is actually available
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    allocated_ports.add(port)
                    logger.debug(f"Allocated port {port}")
                    return port
                except OSError:
                    port += 1
        
        raise RuntimeError(f"Could not find available port after {start_port}")


def kill_process_tree(process: subprocess.Popen, port: Optional[int] = None):
    """Kill process and all its children, then free the port."""
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
    
    # Free the port for reuse
    if port is not None:
        with port_lock:
            allocated_ports.discard(port)
            logger.debug(f"Freed port {port}")


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


def get_serve_commands(framework: str, repo_path: Path, port: int) -> list:
    """Get the appropriate serve commands for a framework."""
    if framework not in FRAMEWORK_COMMANDS:
        framework = "Static HTML"
    
    for config in FRAMEWORK_COMMANDS.get(framework, []):
        check_file = config.get("check")
        if check_file and not (repo_path / check_file).exists():
            continue
        
        commands = config.get("commands", [])
        commands = [cmd.replace("{port}", str(port)) for cmd in commands]
        return commands
    
    return [f"python -m http.server {port}"]


def run_deployment(repo_path: Path, framework: str, port: int) -> tuple:
    """
    Run deployment for a repo.
    Returns (success: bool, process: Popen or None, error: str or None)
    """
    commands = get_serve_commands(framework, repo_path, port)
    
    if not commands:
        return False, None, "No valid serve commands found"
    
    install_commands = commands[:-1]
    serve_command = commands[-1]
    
    for cmd in install_commands:
        logger.debug(f"  Running: {cmd}")
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
    
    logger.debug(f"  Serving: {serve_command}")
    try:
        process = subprocess.Popen(
            serve_command,
            shell=True,
            cwd=str(repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        
        time.sleep(3)
        
        logger.debug(f"  Checking server health on port {port}...")
        if check_server_health(port, timeout=SERVE_TIMEOUT):
            logger.debug(f"  Server healthy on port {port}")
            return True, process, None
        else:
            logger.warning(f"  Server health check failed after {SERVE_TIMEOUT}s on port {port}")
            kill_process_tree(process, port)
            return False, None, "Server did not respond within timeout"
            
    except Exception as e:
        return False, None, f"Serve error: {e}"


# =========================
# MAIN PROCESSING LOGIC
# =========================

async def measure_cwv_for_url(
    url: str,
    device: str,
    num_runs: int = NUM_CWV_RUNS,
    headless: bool = True,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Measure CWV for a URL multiple times and aggregate results.
    
    Uses retry logic with doubled settle_time if LCP is 0.
    Once a working settle_time is found, it's reused for remaining runs.
    
    Returns dict with raw runs and aggregated metrics.
    """
    runs = []
    current_settle_time = DEFAULT_SETTLE_TIME

    def nan_aggregated(total_runs: int) -> Dict[str, Any]:
        return {
            "LCP_median": float("nan"), "LCP_mean": float("nan"), "LCP_stdev": float("nan"), "LCP_p75": float("nan"),
            "CLS_median": float("nan"), "CLS_mean": float("nan"), "CLS_stdev": float("nan"),
            "FID_median": float("nan"), "FID_mean": float("nan"), "FID_stdev": float("nan"),
            "INP_median": float("nan"), "INP_mean": float("nan"), "INP_stdev": float("nan"), "INP_p75": float("nan"),
            "TTFB_median": float("nan"), "TTFB_mean": float("nan"), "TTFB_stdev": float("nan"),
            "FCP_median": float("nan"), "FCP_mean": float("nan"),
            "valid_runs": 0, "total_runs": total_runs,
        }
    
    for run_num in range(num_runs):
        logger.debug(f"    CWV run {run_num + 1}/{num_runs} (settle_time={current_settle_time}ms)")
        
        # Try with current settle_time, retrying with doubled time if LCP=0
        attempt_settle_time = current_settle_time
        metrics = None
        
        for attempt in range(max_retries):
            metrics = await measure_cwv_metrics(
                url, device, headless=headless, settle_time=attempt_settle_time
            )
            
            if metrics.get("status") == "success" and metrics.get("LCP", 0) > 0:
                # Found working settle_time - keep it for future runs
                if attempt_settle_time > current_settle_time:
                    logger.info(f"    Found working settle_time: {attempt_settle_time}ms (was {current_settle_time}ms)")
                    current_settle_time = attempt_settle_time
                break
            
            if attempt < max_retries - 1:
                attempt_settle_time = attempt_settle_time * 2
                logger.info(
                    f"    Retry {attempt + 1}/{max_retries - 1} (LCP=0) - increasing settle_time to {attempt_settle_time}ms"
                )
                await asyncio.sleep(2)
            else:
                logger.error("    Max retries exceeded; aborting remaining runs with NaN metrics")
                runs.append({
                    "status": "error",
                    "LCP": float("nan"),
                    "CLS": float("nan"),
                    "FID": float("nan"),
                    "INP": float("nan"),
                    "TTFB": float("nan"),
                    "FCP": float("nan"),
                })
                return {
                    "status": "error",
                    "error": "Max retries exceeded",
                    "runs": runs,
                    "aggregated": nan_aggregated(len(runs)),
                    "num_runs": num_runs,
                    "device": device,
                    "final_settle_time": current_settle_time,
                }
        
        runs.append(metrics)
        await asyncio.sleep(0.5)
    
    aggregated = calculate_aggregated_metrics(runs)
    
    return {
        "status": "success",
        "runs": runs,
        "aggregated": aggregated,
        "num_runs": num_runs,
        "device": device,
        "final_settle_time": current_settle_time,
    }


def process_single_entry(entry: dict, device: str, num_runs: int, headless: bool) -> Dict[str, Any]:
    """
    Process a single dataset entry:
    1. Clone the repo
    2. Deploy locally
    3. Measure CWV 10 times
    4. Return CWV results
    """
    repo_id = entry.get("REPO_ID")
    framework = entry.get("framework", "Static HTML")
    commit_sha = entry.get("last_commit_sha")  # Specific commit to checkout
    
    if not repo_id:
        return {"status": "error", "error": "Missing repo_id"}
    
    safe_name = repo_id.replace("/", "_")
    tmpdir = Path(tempfile.mkdtemp(prefix=f"cwv_bench_{safe_name}_"))
    port = None
    process = None
    
    try:
        port = find_available_port()
        
        # Step 1: Clone (with specific commit if available)
        clone_url = f"https://github.com/{repo_id}.git"
        if commit_sha:
            logger.info(f"  Cloning {repo_id} @ {commit_sha[:8]}...")
        else:
            logger.info(f"  Cloning {repo_id}...")
        
        if not git_clone(clone_url, tmpdir, commit_sha):
            # Free port on clone failure
            if port:
                with port_lock:
                    allocated_ports.discard(port)
            return {"status": "clone_failed", "error": "Failed to clone repository"}
        
        # Step 2: Deploy
        logger.info(f"  Deploying ({framework}) on port {port}...")
        success, process, error = run_deployment(tmpdir, framework, port)
        
        if not success:
            # Free the port before returning (already present)
            if port:
                with port_lock:
                    allocated_ports.discard(port)
            return {"status": "deploy_failed", "error": error}
        
        # Step 3: Measure CWV
        url = f"http://localhost:{port}"
        logger.info(f"  Measuring CWV ({num_runs} runs)...")
        
        # Use asyncio.run() - creates a fresh event loop (thread-safe)
        cwv_result = asyncio.run(measure_cwv_for_url(url, device, num_runs, headless=headless))
        
        if cwv_result.get("status") == "error":
            return {
                "status": "error",
                "error": cwv_result.get("error"),
                "cwv": cwv_result,
            }

        return {
            "status": "success",
            "cwv": cwv_result,
        }
        
    except Exception as e:
        # Free port on any exception
        if port:
            with port_lock:
                allocated_ports.discard(port)
        return {"status": "error", "error": str(e)}
    
    finally:
        if process:
            kill_process_tree(process, port)
        elif port:  # Port allocated but no process started
            with port_lock:
                allocated_ports.discard(port)
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Benchmark CWV for HuggingFace dataset repos")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of repos (0 = all)")
    parser.add_argument("--device", default=DEFAULT_DEVICE, choices=["mobile", "desktop"], help="Device type")
    parser.add_argument("--num-runs", type=int, default=NUM_CWV_RUNS, help="Number of CWV measurement runs")
    parser.add_argument("--resume", action="store_true", help="Skip entries that already have cwv data")
    parser.add_argument("-w", "--workers", type=int, default=1, help="Number of parallel workers (default: 1)")
    parser.add_argument("--index", type=int, default=None, help="Run only a specific dataset index (0-based)")
    parser.add_argument("--headed", action="store_true", help="Run CWV in headed mode (headless=False)")
    args = parser.parse_args()
    
    logger.info(f"Loading dataset: {DATASET_NAME}...")
    dataset = load_dataset(DATASET_NAME, split=SPLIT)
    logger.info(f"Loaded {len(dataset)} entries")
    
    # Convert to list for processing
    rows = [dict(row) for row in dataset]
    
    if args.index is not None:
        if args.index < 0 or args.index >= len(rows):
            logger.error(f"Index out of range: {args.index} (0-{len(rows)-1})")
            return 1
        rows = [rows[args.index]]
    elif args.limit > 0:
        rows = rows[:args.limit]
    
    # Column name based on device
    cwv_column = f"cwv_{args.device}"
    
    # Filter out entries that already have CWV data if resuming
    if args.resume:
        rows = [r for r in rows if not r.get(cwv_column)]
        logger.info(f"Resuming: {len(rows)} entries without {cwv_column} data")
    
    if not rows:
        logger.info("No entries to process!")
        return 0
    
    # Thread-safe counters and storage
    results_lock = threading.Lock()
    updated_rows = []
    counters = {"success": 0, "error": 0}
    
    # Create checkpoint directory and file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    checkpoint_dir = DUMPS_DIR / f"{args.device}_{args.workers}workers_{timestamp}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = checkpoint_dir / "checkpoint.jsonl"
    final_results_file = checkpoint_dir / "final_results.json"
    
    logger.info(f"Checkpoint directory: {checkpoint_dir}")
    
    def save_checkpoint():
        """Save current results to checkpoint file (non-blocking)."""
        # Copy data inside lock, write outside lock
        with results_lock:
            rows_snapshot = list(updated_rows)  # Quick copy
        
        # Write to disk WITHOUT holding the lock
        with open(checkpoint_file, 'w') as f:
            for row in rows_snapshot:
                f.write(json.dumps(row) + '\n')
        logger.info(f"💾 Checkpoint saved: {len(rows_snapshot)} entries")
    
    def process_one(idx_row, total_count: int):
        """Process a single entry (for threading)."""
        idx, row = idx_row
        repo_id = row.get("REPO_ID", "unknown")
        logger.info(f"TICK [{idx+1}/{total_count}] Processing: {repo_id}")

        result = process_single_entry(row, args.device, args.num_runs, headless=not args.headed)

        should_checkpoint = False
        with results_lock:
            if result.get("status") == "success":
                row[cwv_column] = result["cwv"]
                counters["success"] += 1
                logger.info(f"  ✓ Success")
            else:
                # If CWV result is available (e.g., NaN metrics), store it
                if result.get("cwv"):
                    row[cwv_column] = result["cwv"]
                else:
                    row[cwv_column] = {"status": result.get("status"), "error": result.get("error")}
                counters["error"] += 1
                logger.warning(f"  ✗ {result.get('status')}: {result.get('error', '')[:50]}")

            updated_rows.append(row)

            # Check if we should checkpoint (but don't do it inside lock!)
            if counters["success"] % CHECKPOINT_EVERY == 0 and counters["success"] > 0:
                should_checkpoint = True

        # Checkpoint OUTSIDE the lock
        if should_checkpoint:
            save_checkpoint()

        return result

    try:
        if args.workers > 1:
            # Multi-threaded processing
            logger.info(f"Using {args.workers} worker threads")
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(process_one, (idx, row), len(rows)): row for idx, row in enumerate(rows)}
                
                with tqdm(total=len(rows), desc="Processing repos") as pbar:
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            logger.error(f"Worker error: {e}")
                        pbar.update(1)
        else:
            # Single-threaded processing (original behavior)
            for idx, row in enumerate(tqdm(rows, desc="Processing repos")):
                process_one((idx, row), len(rows))
    
    except KeyboardInterrupt:
        logger.warning("\n⚠️  Interrupted by user")
        logger.info("Saving checkpoint before exit...")
        save_checkpoint()
    
    # Final checkpoint
    logger.info("\nSaving final checkpoint...")
    save_checkpoint()
    
    success_count = counters["success"]
    error_count = counters["error"]
    
    # Create updated dataset
    logger.info(f"\nCreating updated dataset...")
    logger.info(f"  Success: {success_count}")
    logger.info(f"  Failed: {error_count}")
    
    # If we only processed a subset (limit or resume), merge with original
    if args.limit > 0 or args.resume:
        # Load original again and update processed entries
        original = load_dataset(DATASET_NAME, split=SPLIT)
        all_rows = [dict(row) for row in original]
        
        # Create lookup by repo_id
        processed_lookup = {r["REPO_ID"]: r for r in updated_rows if r.get("REPO_ID")}
        
        # Update original with processed results
        for i, row in enumerate(all_rows):
            if row.get("REPO_ID") in processed_lookup:
                all_rows[i] = processed_lookup[row["REPO_ID"]]
        
        new_dataset = Dataset.from_list(all_rows)
    else:
        new_dataset = Dataset.from_list(updated_rows)
    
    # Save final results locally
    logger.info(f"Saving final results to {final_results_file}")
    with open(final_results_file, 'w') as f:
        json.dump([dict(row) for row in new_dataset], f, indent=2)
    
    # # Push to HuggingFace
    # logger.info(f"Pushing updated dataset to {DATASET_NAME} (column: {cwv_column})...")
    # try:
    #     new_dataset.push_to_hub(DATASET_NAME, split=SPLIT)
    #     logger.info("✓ Done! Pushed to HuggingFace")
    # except Exception as e:
    #     logger.error(f"Failed to push to HuggingFace: {e}")
    #     logger.info(f"Results saved locally in: {checkpoint_dir}")
    #     return 1
    
    logger.info(f"✓ All results saved in: {checkpoint_dir}")
    return 0


if __name__ == "__main__":
    exit(main())
