"""Node for framework-based deployment without AI analysis.

This node deploys repositories using pre-configured shell commands based
on the framework type (Hexo, Jekyll, Static HTML) from the input state.
No Aider AI involvement - just deterministic commands.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)

# ------------------------
# Framework Commands Config
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

DEFAULT_PORT = 8000
INSTALL_TIMEOUT = 120  # 2 min for npm/bundle install
SERVE_TIMEOUT = 30     # Wait for server startup


# ------------------------
# Utility Functions
# ------------------------

def find_available_port(start_port: int = DEFAULT_PORT) -> int:
    """Find an available port starting from start_port."""
    port = start_port
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    return start_port


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


def get_serve_commands(framework: str, repo_path: Path, port: int) -> List[str]:
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
    
    # Fallback: Python HTTP server
    return [f"python -m http.server {port}"]


def run_install_commands(
    repo_path: Path,
    commands: List[str],
    run_logger: logging.Logger,
) -> Tuple[bool, str]:
    """Run install commands (all except the last serve command)."""
    install_commands = commands[:-1]
    
    for cmd in install_commands:
        run_logger.info(f"  Running: {cmd}")
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
                return False, f"Install failed: {error_msg}"
        except subprocess.TimeoutExpired:
            return False, f"Install timeout: {cmd}"
        except Exception as e:
            return False, f"Install error: {e}"
    
    return True, ""


async def start_server_background(
    repo_path: Path,
    serve_command: str,
    port: int,
    run_logger: logging.Logger,
) -> Dict[str, Any]:
    """Start the serve command in background and verify health."""
    run_logger.info(f"  Starting server: {serve_command}")
    
    log_file = repo_path.parent / "server.log"
    
    try:
        with open(log_file, "w") as f:
            process = subprocess.Popen(
                serve_command,
                shell=True,
                cwd=str(repo_path),
                stdout=f,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
        
        run_logger.info(f"Server process started with PID: {process.pid}")
        
        # Wait for server to become healthy
        max_wait = 30
        check_interval = 2
        elapsed = 0
        
        while elapsed < max_wait:
            await asyncio.sleep(check_interval)
            elapsed += check_interval
            
            if process.poll() is not None:
                with open(log_file, "r") as f:
                    log_content = f.read()[-1000:]
                return {
                    "status": "error",
                    "error": f"Server exited with code {process.returncode}. Log: {log_content}"
                }
            
            if check_server_health(port, timeout=3):
                run_logger.info(f"Server is responding after {elapsed}s")
                return {
                    "status": "success",
                    "pid": process.pid,
                    "deployed_url": f"http://127.0.0.1:{port}",
                }
        
        # Timeout but process still running
        if process.poll() is None:
            run_logger.warning("Server running but not responding - assuming OK")
            return {
                "status": "success",
                "pid": process.pid,
                "deployed_url": f"http://127.0.0.1:{port}",
                "warning": "Server may not be fully ready",
            }
        else:
            with open(log_file, "r") as f:
                log_content = f.read()[-1000:]
            return {"status": "error", "error": f"Server failed: {log_content}"}
            
    except Exception as e:
        run_logger.error(f"Error starting server: {e}")
        return {"status": "error", "error": str(e)}


def get_run_logger(log_file: Optional[str], repo_name: str) -> logging.Logger:
    """Get or create a run logger for file logging."""
    if not log_file:
        return logger
    
    run_logger = logging.getLogger(f"cwv_run.framework.{repo_name}")
    if not run_logger.handlers:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)
        run_logger.addHandler(file_handler)
    
    return run_logger


# ------------------------
# Main Node
# ------------------------

async def framework_deploy_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that deploys using framework-specific commands.
    
    Uses pre-detected framework from input state to run appropriate
    shell commands. No AI involvement.
    
    Input from state:
        - workspace_dir: Path to cloned repository
        - framework: Framework type (Hexo, Jekyll, Static HTML)
        - repo_name: Repository name
        - log_file: Path to run log file
        - reports_dir: Path to reports directory
    
    Output to state:
        - deployed_url: URL where server is running
        - server_pid: PID of server process
        - url: Same as deployed_url (for compatibility)
    """
    
    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        workspace_dir = current_state.get("workspace_dir")
        if not workspace_dir:
            raise RuntimeError("workspace_dir is required (run clone_repo first)")
        
        framework = current_state.get("framework", "Static HTML")
        repo_name = current_state.get("repo_name", "unknown")
        log_file = current_state.get("log_file")
        
        run_logger = get_run_logger(log_file, repo_name)
        repo_path = Path(workspace_dir)
        
        run_logger.info("=" * 60)
        run_logger.info("FRAMEWORK DEPLOY NODE")
        run_logger.info("=" * 60)
        run_logger.info(f"Framework: {framework}")
        run_logger.info(f"Workspace: {workspace_dir}")
        
        # Find available port
        port = find_available_port(DEFAULT_PORT)
        run_logger.info(f"Using port: {port}")
        
        # Get commands for this framework
        commands = get_serve_commands(framework, repo_path, port)
        run_logger.info(f"Commands: {commands}")
        
        if not commands:
            raise RuntimeError(f"No deployment commands found for framework: {framework}")
        
        # Run install commands
        if len(commands) > 1:
            success, error = run_install_commands(repo_path, commands, run_logger)
            if not success:
                raise RuntimeError(error)
        
        # Start server
        serve_command = commands[-1]
        server_result = await start_server_background(repo_path, serve_command, port, run_logger)
        
        if server_result.get("status") != "success":
            error = server_result.get("error", "Failed to start server")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)
        
        # Update state
        current_state["deployed_url"] = server_result["deployed_url"]
        current_state["server_pid"] = server_result.get("pid")
        current_state["url"] = server_result["deployed_url"]
        
        run_logger.info("=" * 60)
        run_logger.info("SERVER DEPLOYED SUCCESSFULLY")
        run_logger.info(f"URL: {server_result['deployed_url']}")
        run_logger.info(f"PID: {server_result.get('pid')}")
        run_logger.info("=" * 60)
        
        logger.info("Server deployed at: %s (framework: %s)", server_result["deployed_url"], framework)
        return current_state
    
    return await run_with_timing("framework_deploy", state, _impl)
