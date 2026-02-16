#!/usr/bin/env python3
"""
CWV Benchmark Script

Loads HuggingFace dataset, deploys sites locally using framework-specific commands,
measures Core Web Vitals 10 times, and updates the dataset with CWV results.

Usage:
    python scripts/helper_scripts/cwv_benchmark.py [--limit N] [--device mobile|desktop]
    python scripts/helper_scripts/cwv_benchmark.py --csv SAMPLE/final_sampled_repos.csv [--limit N] [--device mobile|desktop]

Solutions for "Target page, context or browser has been closed" when using many workers:
    1. Use --processes so each worker runs in its own process with an isolated
       Playwright browser (recommended for -w 4+): e.g. -w 8 --processes
    2. The script retries CWV measurement a few times on "browser closed" errors.
    3. Otherwise reduce -w or run single-threaded.
"""

import asyncio
import csv
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
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

# Load .env from project root so HF_TOKEN is available for push_to_hub (do not commit .env)
try:
    from dotenv import load_dotenv
    _project_root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_project_root / ".env")
except ImportError:
    pass

from datasets import load_dataset, Dataset
from tqdm import tqdm

# Import CWV measurement functions from performance_testing
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from cwv_optimizer.services.performance_testing import (
    measure_multiple_runs,
    # measure_cwv_metrics,
    calculate_aggregated_metrics,
    # DEFAULT_SETTLE_TIME,
)

# =========================
# CONFIG
# =========================
DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
SPLIT = "train"
NUM_CWV_RUNS = 5
DEFAULT_DEVICE = "mobile"

# Timeouts
INSTALL_TIMEOUT = 90   # 90s for npm/bundle install (reduced to fail faster)
SERVE_TIMEOUT = 30     # Wait for server startup

# Checkpointing
CHECKPOINT_EVERY = 2   # Save checkpoint every N successful results
PUSH_TO_HUB_EVERY = 10  # Push dataset to HuggingFace every N completed repos (0 = only at end)
DUMPS_DIR = Path(__file__).parent.parent.parent / "dumps" / "cwv_benchmark"

# CWV retry when Playwright fails with "browser/page closed" (common with many threads)
CWV_RETRY_ON_CLOSED = 2   # number of retries
CWV_RETRY_DELAY_SEC = 2   # seconds between retries

# Thread-safe port allocation (used only when using thread workers)
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


def find_available_port(start_port: int = 8080, use_global_alloc: bool = True) -> int:
    """Find an available port. If use_global_alloc=True (thread mode), track in allocated_ports."""
    import socket

    with (port_lock if use_global_alloc else _noop_context()):
        port = start_port
        for _ in range(500):
            if use_global_alloc and port in allocated_ports:
                port += 1
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    if use_global_alloc:
                        allocated_ports.add(port)
                    logger.debug(f"Allocated port {port}")
                    return port
                except OSError:
                    port += 1
        raise RuntimeError(f"Could not find available port after {start_port}")


class _noop_context:
    """Context manager that does nothing (for optional lock)."""

    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


def kill_process_tree(process: subprocess.Popen, port: Optional[int] = None, free_port: bool = True):
    """Kill process and all its children; if free_port, release port from global allocated set."""
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
    if free_port and port is not None:
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


def load_rows_from_csv(csv_path: Path) -> List[Dict[str, Any]]:
    """
    Load repo rows from a CSV file (e.g. final_sampled_repos.csv).
    Normalizes column names so rows match what process_single_entry expects:
    REPO_ID, ID, framework, last_commit_sha, HOST_FILE_PATH, SOURCE.
    """
    # CSV has very large fields (e.g. webpages, CODE_STATS); raise limit to avoid _csv.Error
    old_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(10 * 1024 * 1024)  # 10 MB per field
    except OverflowError:
        csv.field_size_limit(int(10e6))
    try:
        rows = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Preserve all original columns and add normalized keys
                r = dict(row)
                # Script uses entry.get("framework", "Static HTML")
                if "framework" not in r or not str(r.get("framework", "")).strip():
                    r["framework"] = (r.get("FRAMEWORK") or "").strip() or "Static HTML"
                # Script uses entry.get("last_commit_sha")
                if r.get("COMMIT_ID"):
                    r["last_commit_sha"] = (r.get("COMMIT_ID") or "").strip()
                rows.append(r)
        return rows
    finally:
        csv.field_size_limit(old_limit)


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


def _is_browser_closed_error(err: BaseException) -> bool:
    """True if the error indicates page/context/browser was closed (retryable with fresh browser)."""
    msg = (getattr(err, "message", "") or str(err)).lower()
    return (
        "has been closed" in msg
        or "target closed" in msg
        or "browser closed" in msg
        or "context closed" in msg
    )


async def measure_cwv_for_url(
    url: str,
    device: str,
    num_runs: int,
    headless: bool = True,
):
    runs, final_settle_time, success = await measure_multiple_runs(
        url=url,
        device=device,
        headless=headless,
        num_runs=num_runs,
    )

    aggregated = calculate_aggregated_metrics(runs)

    # Collect per-run attribution details for convenience
    lcp_elements = [r.get("lcp_element") if r.get("status") == "success" else None for r in runs]
    cls_shifts = [r.get("cls_shifts") if r.get("status") == "success" else [] for r in runs]
    inp_interactions = [r.get("inp_interactions") if r.get("status") == "success" else [] for r in runs]

    return {
        "status": "success" if success else "error",
        "runs": runs,
        "aggregated": aggregated,
        "lcp_element": lcp_elements,
        "cls_shifts": cls_shifts,
        "inp_interactions": inp_interactions,
        "num_runs": num_runs,
        "device": device,
        "final_settle_time": final_settle_time,
    }



def process_single_entry(
    entry: dict,
    device: str,
    num_runs: int,
    headless: bool,
    use_global_port_alloc: bool = True,
) -> Dict[str, Any]:
    """
    Process a single dataset entry:
    1. Clone the repo
    2. Deploy locally
    3. Measure CWV 10 times
    4. Return CWV results

    use_global_port_alloc: If False (process-worker mode), find a port locally and do not
    track in shared allocated_ports, so multiple processes don't conflict.
    """
    repo_id = entry.get("REPO_ID")
    row_index = entry.get("_row_index")
    row_id = entry.get("ID")
    framework = entry.get("framework", "Static HTML")
    source = entry.get("SOURCE") or entry.get("source")
    host_file = entry.get("HOST_FILE_PATH") or entry.get("host_file_path")
    commit_sha = (entry.get("COMMIT_ID") or entry.get("last_commit_sha") or "").strip() or None
    entry_start = time.time()

    if not repo_id:
        return {"status": "error", "error": "Missing repo_id"}

    logger.info(
        "[T%s] [%s] [%s] Meta: framework=%s, source=%s, commit=%s, device=%s, num_runs=%s, headless=%s, host_script=%s, use_global_port_alloc=%s",
        threading.get_ident(),
        row_id,
        repo_id,
        framework,
        source,
        (commit_sha[:8] if commit_sha else "HEAD"),
        device,
        num_runs,
        headless,
        host_file,
        use_global_port_alloc,
    )

    safe_name = repo_id.replace("/", "_")
    tmpdir = Path(tempfile.mkdtemp(prefix=f"cwv_bench_{safe_name}_"))
    port = None
    process = None

    try:
        if use_global_port_alloc:
            port = find_available_port(use_global_alloc=True)
        else:
            # Process-worker mode: avoid port collision with other processes
            port = find_available_port(
                start_port=8080 + (os.getpid() % 5000),
                use_global_alloc=False,
            )
        
        # Step 1: Clone (with specific commit if available)
        clone_url = f"https://github.com/{repo_id}.git"
        t0 = time.time()
        if commit_sha:
            logger.info(
                "[T%s] [%s] [%s] [1/3] Cloning @ %s...",
                threading.get_ident(),
                row_id,
                repo_id,
                commit_sha[:8],
            )
        else:
            logger.info(
                "[T%s] [%s] [%s] [1/3] Cloning...",
                threading.get_ident(),
                row_id,
                repo_id,
            )

        if not git_clone(clone_url, tmpdir, commit_sha):
            # Free port on clone failure
            if port:
                with port_lock:
                    allocated_ports.discard(port)
            logger.warning(f"  Clone failed after {time.time() - t0:.1f}s")
            return {"status": "clone_failed", "error": "Failed to clone repository"}
        logger.info(
            "[T%s] [%s] [%s] Clone done in %.1fs",
            threading.get_ident(),
            row_id,
            repo_id,
            time.time() - t0,
        )

        # Step 2: Deploy
        t1 = time.time()
        logger.info(
            "[T%s] [%s] [%s] [2/3] Deploying (%s) on port %s...",
            threading.get_ident(),
            row_id,
            repo_id,
            framework,
            port,
        )
        success, process, error = run_deployment(tmpdir, framework, port)
        
        if not success:
            logger.warning(
                "[T%s] [%s] [%s] Deploy failed after %.1fs: %s",
                threading.get_ident(),
                row_id,
                repo_id,
                time.time() - t1,
                error,
            )
            # Free the port before returning (already present)
            if port:
                with port_lock:
                    allocated_ports.discard(port)
            return {"status": "deploy_failed", "error": error}
        
        # Step 3: Measure CWV (with retries on "browser closed" when using many workers)
        t2 = time.time()
        url = f"http://localhost:{port}"
        logger.info(
            "[T%s] [%s] [%s] [3/3] Measuring CWV (%s runs)...",
            threading.get_ident(),
            row_id,
            repo_id,
            num_runs,
        )
        cwv_result = None
        for attempt in range(CWV_RETRY_ON_CLOSED + 1):
            try:
                cwv_result = asyncio.run(measure_cwv_for_url(url, device, num_runs, headless=headless))
                break
            except Exception as e:
                if attempt < CWV_RETRY_ON_CLOSED and _is_browser_closed_error(e):
                    logger.warning(
                        "[T%s] [%s] [%s] CWV attempt %s failed (browser closed), retrying in %ss...",
                        threading.get_ident(),
                        row_id,
                        repo_id,
                        attempt + 1,
                        CWV_RETRY_DELAY_SEC,
                    )
                    time.sleep(CWV_RETRY_DELAY_SEC)
                else:
                    cwv_result = {
                        "status": "error",
                        "error": str(e),
                        "runs": [],
                        "aggregated": {},
                        "LCP_ENTRIES": [],
                        "num_runs": num_runs,
                        "device": device,
                        "final_settle_time": 0,
                    }
                    break
        if cwv_result is None:
            cwv_result = {"status": "error", "error": "CWV measurement failed after retries", "runs": [], "aggregated": {}, "LCP_ENTRIES": [], "num_runs": num_runs, "device": device, "final_settle_time": 0}
        logger.info(
            "[T%s] [%s] [%s] CWV measurement done in %.1fs",
            threading.get_ident(),
            row_id,
            repo_id,
            time.time() - t2,
        )

        if cwv_result.get("status") == "error":
            logger.warning(
                "[T%s] [%s] [%s] CWV measurement error: %s",
                threading.get_ident(),
                row_id,
                repo_id,
                cwv_result.get("error"),
            )
            return {
                "status": "error",
                "error": cwv_result.get("error"),
                "cwv": cwv_result,
            }

        # Log a compact CWV summary so we can debug outliers later
        agg = cwv_result.get("aggregated") or {}
        logger.info(
            "[T%s] [%s] [%s] CWV aggregated: LCP_p75=%s ms, INP_p75=%s ms, CLS_median=%s, TTFB_median=%s ms, valid_runs=%s/%s, settle_time=%s ms",
            threading.get_ident(),
            row_id,
            repo_id,
            agg.get("LCP_p75"),
            agg.get("INP_p75"),
            agg.get("CLS_median"),
            agg.get("TTFB_median"),
            agg.get("valid_runs"),
            agg.get("total_runs"),
            cwv_result.get("final_settle_time"),
        )

        total_sec = time.time() - entry_start
        logger.info(
            "[T%s] [%s] [%s] Entry total time: %.1fs",
            threading.get_ident(),
            row_id,
            repo_id,
            total_sec,
        )
        return {
            "status": "success",
            "cwv": cwv_result,
        }

    except Exception as e:
        # Free port on any exception (only from global set in thread mode)
        if port and use_global_port_alloc:
            with port_lock:
                allocated_ports.discard(port)
        logger.warning(f"  [{repo_id}] Exception: {e}")
        return {"status": "error", "error": str(e)}
    
    finally:
        if process:
            kill_process_tree(process, port, free_port=use_global_port_alloc)
        elif port and use_global_port_alloc:
            with port_lock:
                allocated_ports.discard(port)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _process_one_standalone(
    row: dict,
    device: str,
    num_runs: int,
    headless: bool,
) -> tuple:
    """
    Run in a separate process (ProcessPoolExecutor). Each process has its own
    Playwright browser and port space, avoiding "browser has been closed" races.
    Returns (row, result) where row is the same dict with cwv_column not yet set;
    caller should set it from result and append to updated_rows.
    """
    result = process_single_entry(
        row,
        device=device,
        num_runs=num_runs,
        headless=headless,
        use_global_port_alloc=False,
    )
    return (row, result)


def main():
    parser = argparse.ArgumentParser(description="Benchmark CWV for HuggingFace dataset repos or a single URL")
    parser.add_argument("--url", type=str, default=None, help="Measure a single URL (skips dataset); output JSON to stdout")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of repos (0 = all)")
    parser.add_argument("--device", default=DEFAULT_DEVICE, choices=["mobile", "desktop"], help="Device type")
    parser.add_argument("--num-runs", type=int, default=NUM_CWV_RUNS, help="Number of CWV measurement runs")
    parser.add_argument("--resume", action="store_true", help="Skip entries that already have cwv data")
    parser.add_argument("-w", "--workers", type=int, default=4, help="Number of parallel workers (default: 1)")
    parser.add_argument("--processes", action="store_true", default = True, help="Use process-based workers instead of threads. Each worker runs in its own process with an isolated Playwright browser, reducing 'browser has been closed' errors when using many workers. Recommended for -w 4 or more.")
    parser.add_argument("--index", type=int, default=None, help="Run only a specific dataset index (0-based)")
    parser.add_argument("--headed", action="store_true", help="Run CWV in headed mode (headless=False)")
    parser.add_argument("--push-every", type=int, default=0, help="Push dataset to HuggingFace every N repos (0 = only at end). Default: 100")
    parser.add_argument("--csv", type=str, default=None, help="Path to CSV input (e.g. SAMPLE/final_sampled_repos.csv). When set, load repos from CSV instead of HuggingFace; push to hub is disabled.")
    args = parser.parse_args()

    # Single-URL mode: measure one URL and print JSON (used by harness evaluate.sh)
    if args.url:
        headless = not getattr(args, "headed", False)
        result = asyncio.run(
            measure_cwv_for_url(
                url=args.url,
                device=args.device,
                num_runs=args.num_runs,
                headless=headless,
            )
        )
        def _json_default(o):
            if hasattr(o, "item"):
                return o.item()
            raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
        print(json.dumps(result, indent=2, default=_json_default))
        return 0

    run_start_time = time.time()
    logger.info("=" * 60)
    logger.info("CWV Benchmark run started")
    logger.info("=" * 60)

    use_csv = bool(args.csv)
    if use_csv:
        csv_path = Path(args.csv)
        if not csv_path.is_absolute():
            csv_path = (Path(__file__).resolve().parent.parent.parent / args.csv)
        if not csv_path.exists():
            logger.error(f"CSV file not found: {csv_path}")
            return 1
        logger.info(f"Loading repos from CSV: {csv_path}")
        rows = load_rows_from_csv(csv_path)
        total_in_dataset = len(rows)
        logger.info(f"Loaded {total_in_dataset} entries from CSV")
        # Disable push to HuggingFace when using CSV input
        if args.push_every != 0:
            logger.info("Push to HuggingFace disabled when using --csv")
            args.push_every = 0
    else:
        logger.info(f"Loading dataset: {DATASET_NAME}...")
        dataset = load_dataset(DATASET_NAME, split=SPLIT)
        total_in_dataset = len(dataset)
        logger.info(f"Loaded {total_in_dataset} entries")
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
    checkpoint_dir = DUMPS_DIR / f"{args.device}_{args.workers}workers_{NUM_CWV_RUNS}runs_{timestamp}"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_file = checkpoint_dir / "checkpoint.jsonl"
    final_results_file = checkpoint_dir / "final_results.json"
    log_file = checkpoint_dir / "run.log"

    # Add file handler so full run is logged to checkpoint dir
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)

    logger.info(f"Checkpoint directory: {checkpoint_dir}")
    logger.info(f"Log file: {log_file}")
    if args.push_every > 0:
        logger.info(f"Will push to HuggingFace every {args.push_every} repos")
    logger.info("")

    def push_to_hub():
        """Merge current updated_rows into full dataset and push to HuggingFace."""
        with results_lock:
            rows_snapshot = list(updated_rows)
        if not rows_snapshot:
            return
        try:
            logger.info(f"📤 Pushing to HuggingFace ({DATASET_NAME})... ({len(rows_snapshot)} results)")
            original = load_dataset(DATASET_NAME, split=SPLIT)
            all_rows = [dict(row) for row in original]
            processed_lookup = {r["REPO_ID"]: r for r in rows_snapshot if r.get("REPO_ID")}
            for i, row in enumerate(all_rows):
                if row.get("REPO_ID") in processed_lookup:
                    all_rows[i] = processed_lookup[row["REPO_ID"]]
            new_dataset = Dataset.from_list(all_rows)
            new_dataset.push_to_hub(DATASET_NAME, split=SPLIT)
            logger.info(f"📤 Push to HuggingFace completed successfully ({DATASET_NAME}): {len(rows_snapshot)} results so far")
        except Exception as e:
            logger.error(f"Failed to push to HuggingFace: {e}")

    def save_checkpoint():
        """Save current results to checkpoint file (non-blocking)."""
        # Copy data inside lock, write outside lock
        with results_lock:
            rows_snapshot = list(updated_rows)  # Quick copy
        
        # Write to disk WITHOUT holding the lock
        with open(checkpoint_file, 'w') as f:
            for row in rows_snapshot:
                f.write(json.dumps(row) + '\n')
        with results_lock:
            s, e = counters["success"], counters["error"]
        logger.info(f"💾 Checkpoint saved: {len(rows_snapshot)} entries (success={s}, failed={e})")

    def process_one(idx_row, total_count: int):
        """Process a single entry (for threading)."""
        idx, row = idx_row
        repo_id = row.get("REPO_ID", "unknown")
        # 1-based index within this run (for context only)
        row["_row_index"] = idx + 1
        row_id = row.get("ID")
        item_start = time.time()
        logger.info("")
        logger.info(
            "[T%s] [%s] [%s] >>> [%s/%s] Processing",
            threading.get_ident(),
            repo_id,
            row_id,
            idx + 1,
            total_count,
        )

        result = process_single_entry(row, args.device, args.num_runs, headless=not args.headed)
        item_elapsed = time.time() - item_start

        should_checkpoint = False
        with results_lock:
            if result.get("status") == "success":
                row[cwv_column] = result["cwv"]
                counters["success"] += 1
                logger.info(
                    "[T%s] [%s] [%s] ✓ Success (elapsed: %.1fs)",
                    threading.get_ident(),
                    row_id,
                    repo_id,
                    item_elapsed,
                )
            else:
                # If CWV result is available (e.g., NaN metrics), store it
                if result.get("cwv"):
                    row[cwv_column] = result["cwv"]
                else:
                    row[cwv_column] = {"status": result.get("status"), "error": result.get("error")}
                counters["error"] += 1
                logger.warning(
                    "[T%s] [%s] [%s] ✗ %s: %s (elapsed: %.1fs)",
                    threading.get_ident(),
                    row_id,
                    repo_id,
                    result.get("status"),
                    # (result.get("error", "")[:80]),
                    (str(result.get("error") or "")[:80]),
                    item_elapsed,
                )

            cur_row = {"ID": row["ID"], "REPO_ID": row["REPO_ID"], f"{cwv_column}": row[cwv_column]}
            updated_rows.append(cur_row)
            done = len(updated_rows)

            # Check if we should checkpoint (but don't do it inside lock!)
            if counters["success"] % CHECKPOINT_EVERY == 0 and counters["success"] > 0:
                should_checkpoint = True

        # Checkpoint OUTSIDE the lock
        if should_checkpoint:
            print("---------------------------CHECKPOINTING---------------------------")
            save_checkpoint()
        if args.push_every > 0 and done % args.push_every == 0 and done > 0:
            push_to_hub()

        # Progress summary every 10 entries
        if done % 5 == 0:
            with results_lock:
                s, e = counters["success"], counters["error"]
            now = time.time()
            elapsed_total = now - run_start_time
            rate = done / (elapsed_total / 60.0) if elapsed_total > 0 else 0  # per minute
            remaining = total_count - done
            eta_min = (remaining / rate) if rate > 0 else 0
            total_est_min = (elapsed_total / 60.0) + eta_min if rate > 0 else 0
            finish_ts = datetime.fromtimestamp(run_start_time + elapsed_total + eta_min * 60.0)
            logger.info(
                "--- Progress: %s/%s done | success=%s failed=%s | %.1f/min | ETA ~%.0f min | est_total=%.1f min | est_finish=%s ---",
                done,
                total_count,
                s,
                e,
                rate,
                eta_min,
                total_est_min,
                finish_ts.strftime("%Y-%m-%d %H:%M"),
            )

        return result

    headless = not args.headed
    use_processes = args.processes and args.workers > 1

    try:
        if use_processes:
            # Process-based workers: each has its own Python process and Playwright browser,
            # avoiding "browser has been closed" races when using many workers.
            logger.info(f"Using {args.workers} process workers (isolated browsers)")
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                futures = {}
                for idx, row in enumerate(rows):
                    row["_row_index"] = idx + 1
                    fut = executor.submit(
                        _process_one_standalone,
                        row,
                        args.device,
                        args.num_runs,
                        headless,
                    )
                    futures[fut] = (idx, row)
                with tqdm(total=len(rows), desc="Processing repos") as pbar:
                    for future in as_completed(futures):
                        idx, row = futures[future]
                        repo_id = row.get("REPO_ID", "unknown")
                        try:
                            row_back, result = future.result()
                            row = row_back
                        except Exception as e:
                            logger.error(f"Worker error for {repo_id}: {e}")
                            result = {"status": "error", "error": str(e)}
                        # Same result handling as process_one
                        should_checkpoint = False
                        with results_lock:
                            row_id = row.get("ID")
                            if result.get("status") == "success":
                                row[cwv_column] = result["cwv"]
                                counters["success"] += 1
                                logger.info(
                                    "[T%s] [%s] [%s] ✓ Success",
                                    threading.get_ident(),
                                    row_id,
                                    repo_id,
                                )
                            else:
                                if result.get("cwv"):
                                    row[cwv_column] = result["cwv"]
                                else:
                                    row[cwv_column] = {"status": result.get("status"), "error": result.get("error")}
                                counters["error"] += 1
                                logger.warning(
                                    "[T%s] [%s] [%s] ✗ %s: %s",
                                    threading.get_ident(),
                                    row_id,
                                    repo_id,
                                    result.get("status"),
                                    # (result.get("error", "")[:80]),
                                    (str(result.get("error") or "")[:80]),
                                )
                            cur_row = {"ID": row["ID"], "REPO_ID": row["REPO_ID"], f"{cwv_column}": row[cwv_column]}
                            updated_rows.append(cur_row)
                            done = len(updated_rows)
                            if counters["success"] % CHECKPOINT_EVERY == 0 and counters["success"] > 0:
                                should_checkpoint = True
                        if should_checkpoint:
                            save_checkpoint()
                        if done % 10 == 0:
                            with results_lock:
                                s, e = counters["success"], counters["error"]
                            now = time.time()
                            elapsed_total = now - run_start_time
                            rate = done / (elapsed_total / 60.0) if elapsed_total > 0 else 0
                            remaining = len(rows) - done
                            eta_min = (remaining / rate) if rate > 0 else 0
                            total_est_min = (elapsed_total / 60.0) + eta_min if rate > 0 else 0
                            finish_ts = datetime.fromtimestamp(run_start_time + elapsed_total + eta_min * 60.0)
                            logger.info(
                                "--- Progress: %s/%s done | success=%s failed=%s | %.1f/min | ETA ~%.0f min | est_total=%.1f min | est_finish=%s ---",
                                done,
                                len(rows),
                                s,
                                e,
                                rate,
                                eta_min,
                                total_est_min,
                                finish_ts.strftime("%Y-%m-%d %H:%M"),
                            )
                        pbar.update(1)
        elif args.workers > 1:
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
    total_elapsed = time.time() - run_start_time
    logger.info("")
    logger.info("=" * 60)
    logger.info("Run finished")
    logger.info("=" * 60)
    logger.info("Saving final checkpoint...")
    save_checkpoint()
    if args.push_every > 0:
        logger.info("Pushing final state to HuggingFace...")
        push_to_hub()

    success_count = counters["success"]
    error_count = counters["error"]

    # Create updated dataset
    logger.info(f"\nCreating updated dataset...")
    logger.info(f"  Success: {success_count}")
    logger.info(f"  Failed: {error_count}")
    logger.info(f"  Total time: {total_elapsed / 60:.1f} min ({total_elapsed / 3600:.2f} h)")
    if success_count + error_count > 0:
        logger.info(f"  Avg per entry: {total_elapsed / (success_count + error_count):.1f} s")
    
    # Build final result: from CSV we only have updated_rows; from HF we merge if subset was processed
    if use_csv:
        new_dataset = Dataset.from_list(updated_rows)
    elif args.limit > 0 or args.resume:
        # Load original again and update processed entries
        original = load_dataset(DATASET_NAME, split=SPLIT)
        all_rows = [dict(row) for row in original]
        processed_lookup = {r["REPO_ID"]: r for r in updated_rows if r.get("REPO_ID")}
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

    logger.info(f"✓ All results saved in: {checkpoint_dir}")
    return 0


if __name__ == "__main__":
    exit(main())
