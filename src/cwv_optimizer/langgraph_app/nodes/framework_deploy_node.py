"""Node for framework-based deployment without AI analysis.

This node deploys repositories using pre-configured shell commands based
on the framework type (Hexo, Jekyll, Static HTML) from the input state.
No Aider AI involvement - just deterministic commands.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)

# Active tunnel handles (module-level so we can clean up on exit)
_ngrok_tunnel = None
_bore_tunnel = None  # BoreTunnel instance when bore provider is used


def start_ngrok_tunnel(port: int, run_logger: logging.Logger) -> Optional[str]:
    """Start an ngrok tunnel to expose a local port publicly.

    Reads NGROK_AUTHTOKEN (or NGROK_AUTH_TOKEN) from the environment.
    Returns the public HTTPS URL, or None if ngrok is unavailable or fails.
    """
    global _ngrok_tunnel

    try:
        from pyngrok import ngrok, conf as ngrok_conf, exception as ngrok_exc
    except ImportError:
        run_logger.warning("pyngrok not installed — skipping tunnel (PSI won't work for localhost)")
        return None

    auth_token = os.environ.get("NGROK_AUTHTOKEN") or os.environ.get("NGROK_AUTH_TOKEN")
    if not auth_token:
        run_logger.warning(
            "NGROK_AUTHTOKEN env var not set — skipping tunnel. "
            "Set it to enable PSI/CrUX field data collection."
        )
        return None

    try:
        ngrok.set_auth_token(auth_token)

        tunnel = ngrok.connect(port, proto="http")
        _ngrok_tunnel = tunnel

        public_url = tunnel.public_url
        # Prefer https
        if public_url.startswith("http://"):
            public_url = public_url.replace("http://", "https://", 1)

        run_logger.info(f"ngrok tunnel started: {public_url} -> localhost:{port}")
        # Brief pause so the tunnel is fully reachable before PSI hits it
        time.sleep(2)
        return public_url

    except Exception as e:
        run_logger.warning(f"Failed to start ngrok tunnel: {e}")
        _ngrok_tunnel = None
        return None


def stop_ngrok_tunnel(run_logger: logging.Logger) -> None:
    """Disconnect the active ngrok tunnel for this process."""
    global _ngrok_tunnel
    if _ngrok_tunnel is not None:
        try:
            from pyngrok import ngrok
            ngrok.disconnect(_ngrok_tunnel.public_url)
            run_logger.info("ngrok tunnel disconnected")
        except Exception as e:
            run_logger.warning(f"Error disconnecting ngrok tunnel: {e}")
        _ngrok_tunnel = None


def start_cloudflare_tunnel(port: int, run_logger: logging.Logger) -> Optional[str]:
    """Start a Cloudflare quick tunnel (no account required).

    Returns the public https://*.trycloudflare.com URL, or None on failure.
    """
    import re
    import queue
    import threading

    bin_path = shutil.which("cloudflared")
    if not bin_path:
        run_logger.warning("cloudflared not found in PATH — skipping Cloudflare tunnel")
        return None

    try:
        proc = subprocess.Popen(
            [bin_path, "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        result_q: queue.Queue = queue.Queue()

        def _reader() -> None:
            try:
                for line in proc.stderr:
                    run_logger.debug(f"cloudflared: {line.strip()}")
                    m = re.search(r"(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)", line)
                    if m:
                        result_q.put(m.group(1))
                        return
            except Exception:
                pass
            result_q.put(None)

        threading.Thread(target=_reader, daemon=True).start()

        try:
            url = result_q.get(timeout=30)
        except queue.Empty:
            url = None

        if url:
            run_logger.info(f"Cloudflare tunnel started: {url} -> localhost:{port}")
            time.sleep(3)  # let Cloudflare propagate
            return url

        run_logger.warning("Cloudflare tunnel URL not found within timeout")
        try:
            proc.terminate()
        except Exception:
            pass
        return None

    except Exception as e:
        run_logger.warning(f"Failed to start Cloudflare tunnel: {e}")
        return None


def start_bore_tunnel(port: int, run_logger: logging.Logger) -> Optional[str]:
    """Start a bore tunnel (https://github.com/ekzhang/bore).

    bore writes its log to stdout; RUST_LOG=info is required.
    Returns the public http://bore.pub:<port> URL, or None on failure.
    Note: bore URLs use plain HTTP on a random high port — they work fine
    for automated PSI calls but may be blocked by firewalls in browsers.
    """
    import re
    import queue
    import threading

    global _bore_tunnel

    bin_path = shutil.which("bore")
    if not bin_path:
        # Also check ~/.cargo/bin explicitly (cargo installs there but it may not
        # be on PATH in non-interactive shells)
        cargo_bin = Path.home() / ".cargo" / "bin" / "bore"
        if cargo_bin.exists():
            bin_path = str(cargo_bin)
        else:
            run_logger.warning(
                "bore not found in PATH — skipping bore tunnel. "
                "Install with: cargo install bore-cli"
            )
            return None

    try:
        env = {**os.environ, "RUST_LOG": "info"}
        proc = subprocess.Popen(
            [bin_path, "local", str(port), "--to", "bore.pub"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        _bore_tunnel = proc

        result_q: queue.Queue = queue.Queue()

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    run_logger.debug(f"bore: {line.strip()}")
                    m = re.search(r"bore\.pub:(\d+)", line)
                    if m:
                        result_q.put(f"http://bore.pub:{m.group(1)}")
                        return
            except Exception:
                pass
            result_q.put(None)

        threading.Thread(target=_reader, daemon=True).start()

        try:
            url = result_q.get(timeout=30)
        except queue.Empty:
            url = None

        if url:
            run_logger.info(f"bore tunnel started: {url} -> localhost:{port}")
            return url

        run_logger.warning("bore tunnel URL not found within timeout")
        try:
            proc.terminate()
        except Exception:
            pass
        _bore_tunnel = None
        return None

    except Exception as e:
        run_logger.warning(f"Failed to start bore tunnel: {e}")
        _bore_tunnel = None
        return None


def stop_bore_tunnel(run_logger: logging.Logger) -> None:
    """Terminate the active bore tunnel process."""
    global _bore_tunnel
    if _bore_tunnel is not None:
        try:
            _bore_tunnel.terminate()
            _bore_tunnel.wait(timeout=5)
            run_logger.info("bore tunnel stopped")
        except Exception as e:
            run_logger.warning(f"Error stopping bore tunnel: {e}")
            try:
                _bore_tunnel.kill()
            except Exception:
                pass
        _bore_tunnel = None


def start_tunnel(
    port: int,
    run_logger: logging.Logger,
    provider: str = "auto",
) -> Optional[str]:
    """Start a public tunnel for *port* using the specified provider.

    provider choices:
      ``"ngrok"``       – pyngrok (requires NGROK_AUTHTOKEN env var)
      ``"cloudflare"``  – cloudflared quick tunnel (no account needed, HTTPS)
      ``"bore"``        – bore.pub (no account needed, HTTP on random high port)
      ``"auto"``        – try ngrok first, then cloudflare, then bore
    """
    provider = provider.lower()

    if provider == "ngrok":
        return start_ngrok_tunnel(port, run_logger)
    if provider == "cloudflare":
        return start_cloudflare_tunnel(port, run_logger)
    if provider == "bore":
        return start_bore_tunnel(port, run_logger)
    if provider == "auto":
        # Try ngrok first (if token available), then cloudflare, then bore
        url = start_ngrok_tunnel(port, run_logger)
        if url:
            return url
        run_logger.info("ngrok unavailable — trying Cloudflare tunnel …")
        url = start_cloudflare_tunnel(port, run_logger)
        if url:
            return url
        run_logger.info("Cloudflare unavailable — trying bore tunnel …")
        return start_bore_tunnel(port, run_logger)

    raise ValueError(
        f"Unknown tunnel provider '{provider}'. "
        "Choose 'ngrok', 'cloudflare', 'bore', or 'auto'."
    )

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

# Aliases for common alternate spellings from dataset CSV
FRAMEWORK_COMMANDS["Vue"] = FRAMEWORK_COMMANDS["Vue.js"]
FRAMEWORK_COMMANDS["Next"] = FRAMEWORK_COMMANDS["Next.js"]

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
                stderr_text = result.stderr.decode("utf-8", errors="ignore")
                stdout_text = result.stdout.decode("utf-8", errors="ignore")
                combined = stderr_text + stdout_text
                # npm exits non-zero for EBADENGINE/deprecated warnings even when
                # packages actually installed. Treat as success if there are no
                # real "npm error" / "ERR!" lines.
                real_errors = [
                    l for l in combined.splitlines()
                    if re.search(r"npm error|npm ERR!|\bERR!\b", l, re.IGNORECASE)
                ]
                if not real_errors:
                    run_logger.warning(
                        "npm exited %d but only warnings detected — continuing",
                        result.returncode,
                    )
                else:
                    error_msg = "\n".join(real_errors[:10]) or combined[:500]
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
        
        # Find available port — respect per-worker port_start to avoid clashes
        port_start = current_state.get("port_start", DEFAULT_PORT)
        port = find_available_port(port_start)
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
        local_url = server_result["deployed_url"]
        current_state["local_url"] = local_url
        current_state["server_pid"] = server_result.get("pid")
        
        # Start a public tunnel so Google PSI can reach the local server.
        # Provider is read from state config; defaults to "auto" which tries
        # ngrok → cloudflare → bore in order.
        tunnel_provider = current_state.get("tunnel_provider", "auto")
        tunnel_url = start_tunnel(port, run_logger, provider=tunnel_provider)
        if tunnel_url:
            current_state["deployed_url"] = tunnel_url
            current_state["url"] = tunnel_url
            current_state["tunnel_url"] = tunnel_url
            # Keep legacy key for any callers that still read ngrok_url
            current_state["ngrok_url"] = tunnel_url
        else:
            current_state["deployed_url"] = local_url
            current_state["url"] = local_url

        run_logger.info("=" * 60)
        run_logger.info("SERVER DEPLOYED SUCCESSFULLY")
        run_logger.info(f"URL: {current_state['deployed_url']}")
        if tunnel_url:
            run_logger.info(f"Tunnel: {tunnel_url} -> {local_url}")
        run_logger.info(f"PID: {server_result.get('pid')}")
        run_logger.info("=" * 60)
        
        logger.info("Server deployed at: %s (framework: %s)", current_state["deployed_url"], framework)
        return current_state
    
    return await run_with_timing("framework_deploy", state, _impl)
