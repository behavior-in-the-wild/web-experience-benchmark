"""Server lifecycle utilities for branch testing.

Provides reusable functions for starting/stopping servers during
visual regression and performance testing.
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

logger = get_logger(__name__)

# Framework Commands Config (shared with framework_deploy_node)
FRAMEWORK_COMMANDS = {
    "Hexo": [
        {"check": "package.json", "commands": ["npm install", "npx hexo server -p {port}"]},
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],
    "Jekyll": [
        {"check": "Gemfile", "commands": ["bundle install", "bundle exec jekyll serve --port {port}"]},
        {"check": "_config.yml", "commands": ["jekyll serve --port {port}"]},
    ],
    "Static HTML": [
        {"check": "index.html", "commands": ["python -m http.server {port} --bind 0.0.0.0"]},
    ],
}

DEFAULT_PORT = 8000
INSTALL_TIMEOUT = 120
SERVE_TIMEOUT = 30


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


def kill_server(pid: Optional[int]) -> bool:
    """Kill a server process by PID.
    
    Args:
        pid: Process ID to kill
        
    Returns:
        True if killed successfully or already dead, False on error
    """
    if not pid:
        return True
    
    try:
        # Try graceful termination first
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        
        # Check if still running
        try:
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            # Still running, force kill
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            logger.info("Force killed server process: %s", pid)
        except OSError:
            logger.info("Server process terminated: %s", pid)
        
        return True
    except OSError as e:
        if e.errno == 3:  # No such process
            return True
        logger.warning("Error killing server PID %s: %s", pid, e)
        return False
    except Exception as e:
        logger.warning("Error killing server PID %s: %s", pid, e)
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


def _run_install_commands(
    repo_path: Path,
    commands: List[str],
) -> Tuple[bool, str]:
    """Run install commands (all except the last serve command)."""
    install_commands = commands[:-1]
    
    for cmd in install_commands:
        logger.debug("Running install: %s", cmd)
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


async def start_framework_server(
    workspace_dir: str,
    framework: str,
    port: Optional[int] = None,
) -> Dict[str, Any]:
    """Start a server for a branch using framework-specific commands.
    
    Args:
        workspace_dir: Path to the cloned repository
        framework: Framework type (Hexo, Jekyll, Static HTML)
        port: Optional specific port, or will find available
        
    Returns:
        Dict with status, url, pid, and port
    """
    repo_path = Path(workspace_dir)
    
    if port is None:
        port = find_available_port(DEFAULT_PORT)
    
    commands = get_serve_commands(framework, repo_path, port)
    
    if not commands:
        return {"status": "error", "error": f"No commands for framework: {framework}"}
    
    # Run install commands if any
    if len(commands) > 1:
        success, error = _run_install_commands(repo_path, commands)
        if not success:
            return {"status": "error", "error": error}
    
    # Start serve command
    serve_command = commands[-1]
    logger.info("Starting server: %s", serve_command)
    
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
        
        # Wait for server to start (increase to allow slower environments more time)
        max_wait = 60
        elapsed = 0
        check_interval = 2
        
        while elapsed < max_wait:
            await asyncio.sleep(check_interval)
            elapsed += check_interval
            
            if process.poll() is not None:
                with open(log_file, "r") as f:
                    log_content = f.read()[-500:]
                return {
                    "status": "error", 
                    "error": f"Server exited with code {process.returncode}. Log: {log_content}"
                }
            
            if check_server_health(port, timeout=10):
                logger.info("Server responding on port %s (PID: %s)", port, process.pid)
                return {
                    "status": "success",
                    "url": f"http://127.0.0.1:{port}",
                    "pid": process.pid,
                    "port": port,
                }
        
        # Timeout but process still running
        if process.poll() is None:
            return {
                "status": "success",
                "url": f"http://127.0.0.1:{port}",
                "pid": process.pid,
                "port": port,
                "warning": "Server may not be fully ready",
            }
        else:
            return {"status": "error", "error": "Server startup timeout"}
            
    except Exception as e:
        logger.error("Error starting server: %s", e)
        return {"status": "error", "error": str(e)}


def get_default_branch(workspace_dir: str) -> str:
    """Detect the default branch of the repository."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("/")[-1]
    except Exception:
        pass
    
    for branch in ["main", "master"]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=workspace_dir,
            capture_output=True,
        )
        if result.returncode == 0:
            return branch
    
    return "main"


def checkout_branch(workspace_dir: str, branch: str) -> bool:
    """Checkout a git branch."""
    try:
        subprocess.run(
            ["git", "checkout", branch],
            cwd=workspace_dir,
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def get_suggestion_branches(workspace_dir: str) -> List[str]:
    """Get list of suggestion branches in the workspace."""
    try:
        result = subprocess.run(
            ["git", "branch", "--list", "suggestion_*"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
        return branches
    except subprocess.CalledProcessError:
        return []
