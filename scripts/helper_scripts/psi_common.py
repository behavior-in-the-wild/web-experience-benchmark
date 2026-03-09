#!/usr/bin/env python3
"""
psi_common.py – Shared utilities for CWV PSI measurement scripts.

Provides:
  - Framework detection and server deployment
  - Cloudflare tunnel management
  - PSI API calls (Google public + Adobe internal (optional))
  - Timing helpers

Used by:
  - bulk_psi_run_tunneling.py  (batch PSI on 300 sites)
  - psi_speed_insights.py      (single-site CLI)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PORT = 8000
INSTALL_TIMEOUT = 180       # seconds for npm/bundle installs
SERVER_HEALTH_TIMEOUT = 30  # seconds to wait for server to respond
TUNNEL_WAIT_TIMEOUT = 30    # seconds to wait for cloudflare URL
TUNNEL_PROPAGATION_DELAY = 5  # seconds after tunnel URL appears before using it
PSI_REQUEST_TIMEOUT = 120   # seconds

# Canonical PSI endpoint — same as cwv-agent/src/tools/psi.js
# Key: GOOGLE_PAGESPEED_INSIGHTS_API_KEY  (same env var name as cwv-agent)
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_API_KEY = os.getenv("GOOGLE_PAGESPEED_INSIGHTS_API_KEY", "")

# Legacy aliases (kept for backward compat)
PSI_PUBLIC_URL = PSI_URL
PSI_ADOBE_URL = os.getenv("PSI_API_URL", "https://psi.experiencecloud.live")

# ---------------------------------------------------------------------------
# Framework → deploy commands map
# Copied verbatim from stack_deploy_test.py (canonical source).
# ---------------------------------------------------------------------------

FRAMEWORK_COMMANDS: Dict[str, List[Dict]] = {
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
        {"check": "server.js", "commands": ["npm install", "PORT={port} node server.js"]},
        {"check": "app.js",    "commands": ["npm install", "PORT={port} node app.js"]},
        {"check": "index.js",  "commands": ["npm install", "PORT={port} node index.js"]},
        # Backend subdir patterns
        {"check": "backend/package.json", "commands": ["cd backend && npm install", "cd backend && PORT={port} npm run start"]},
        {"check": "backend/server.js",    "commands": ["cd backend && npm install", "cd backend && PORT={port} node server.js"]},
    ],

    "Next.js": [
        # Next source app (root)
        {"check": "package.json", "commands": ["npm install", "npm run build", "npm run start -- -p {port}"]},
        # Next monorepo patterns
        {"check": "website/package.json", "commands": ["cd website && npm install", "cd website && npm run build", "cd website && npm run start -- -p {port}"]},
        {"check": "web/package.json",     "commands": ["cd web && npm install", "cd web && npm run build", "cd web && npm run start -- -p {port}"]},
        # Next static export output (commonly out/)
        {"check": "out/index.html", "commands": ["python -m http.server {port} --directory out"]},
        # Next committed build output only
        {"check": "_next/static", "commands": ["python -m http.server {port}"]},
        {"check": "index.html",   "commands": ["python -m http.server {port}"]},
    ],

    "React": [
        # CRA (react-scripts) – standard
        {"check": "package.json", "commands": ["npm install", "PORT={port} npm start"]},
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
        {"check": "package.json", "commands": ["npm install", "PORT={port} npm run serve"]},
        # Vite (Vue) – explicit port + host
        {"check": "vite.config.ts", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        {"check": "vite.config.js", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        # Built Vue outputs (most common on github.io)
        {"check": "dist/index.html",  "commands": ["python -m http.server {port} --directory dist"]},
        # Fallback static
        {"check": "index.html", "commands": ["python -m http.server {port}"]},
    ],

    "Pelican": [
        {
            "check": "pelicanconf.py",
            "commands": [
                "pip install -r requirements.txt",
                "pelican content",
                "python -m http.server {port} --directory output",
            ],
        },
        {
            "check": "publishconf.py",
            "commands": [
                "pip install -r requirements.txt",
                "pelican content -s publishconf.py",
                "python -m http.server {port} --directory output",
            ],
        },
        {"check": "output/index.html", "commands": ["python -m http.server {port} --directory output"]},
        {"check": "index.html",        "commands": ["python -m http.server {port}"]},
    ],

    "Quarto": [
        {
            "check": "_quarto.yml",
            "commands": ["quarto render", "python -m http.server {port} --directory _site"],
        },
        {
            "check": "_quarto.yaml",
            "commands": ["quarto render", "python -m http.server {port} --directory _site"],
        },
        {"check": "_site/index.html", "commands": ["python -m http.server {port} --directory _site"]},
        {"check": "docs/index.html",  "commands": ["python -m http.server {port} --directory docs"]},
        {"check": "index.html",       "commands": ["python -m http.server {port}"]},
    ],

    "Flask": [
        {
            "check": "app.py",
            "commands": [
                "pip install -r requirements.txt",
                "FLASK_APP=app.py FLASK_ENV=development FLASK_RUN_PORT={port} flask run --host=0.0.0.0",
            ],
        },
        {
            "check": "wsgi.py",
            "commands": [
                "pip install -r requirements.txt",
                "FLASK_APP=wsgi.py FLASK_ENV=development FLASK_RUN_PORT={port} flask run --host=0.0.0.0",
            ],
        },
        {"check": "static/index.html", "commands": ["python -m http.server {port} --directory static"]},
    ],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def get_logger(name: str, log_file: Optional[Path] = None) -> logging.Logger:
    """Return a logger writing to console (INFO) and optionally a file (DEBUG)."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger

# ---------------------------------------------------------------------------
# Port utilities
# ---------------------------------------------------------------------------

def find_available_port(start: int = DEFAULT_PORT) -> int:
    """Find the first free TCP port at or after `start`."""
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start

# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------

def detect_framework(repo_path: Path) -> str:
    """Heuristically detect the site framework from repo contents.

    Covers all keys present in FRAMEWORK_COMMANDS.
    """
    # Quarto
    if (repo_path / "_quarto.yml").exists() or (repo_path / "_quarto.yaml").exists():
        return "Quarto"
    # Pelican
    if (repo_path / "pelicanconf.py").exists() or (repo_path / "publishconf.py").exists():
        return "Pelican"
    # Hugo
    if any((repo_path / f).exists() for f in ("hugo.toml", "hugo.yaml", "hugo.yml", "config.toml", "config.yaml", "config.yml")):
        return "Hugo"
    # Jekyll
    if (repo_path / "_config.yml").exists() or (repo_path / "Gemfile").exists():
        return "Jekyll"
    # Flask
    if (repo_path / "app.py").exists() or (repo_path / "wsgi.py").exists():
        req = repo_path / "requirements.txt"
        if req.exists() and "flask" in req.read_text(errors="ignore").lower():
            return "Flask"

    pkg = repo_path / "package.json"
    if pkg.exists():
        content = pkg.read_text(errors="ignore").lower()
        if "hexo" in content:
            return "Hexo"
        if '"next"' in content or "next" in content:
            return "Next.js"
        if "react-scripts" in content:
            return "React"
        if '"react"' in content:
            # Check for Vite config
            if (repo_path / "vite.config.ts").exists() or (repo_path / "vite.config.js").exists():
                return "React"
            return "React"
        if '"vue"' in content:
            return "Vue.js"
        # Express: has server.js / app.js / index.js
        if (repo_path / "server.js").exists() or (repo_path / "app.js").exists():
            return "Express"
        if "express" in content:
            return "Express"

    if (repo_path / "vite.config.ts").exists() or (repo_path / "vite.config.js").exists():
        return "React"  # Vite default

    return "Static HTML"

# ---------------------------------------------------------------------------
# Server deployment
# ---------------------------------------------------------------------------

def get_deploy_commands(framework: str, repo_path: Path, port: int) -> List[str]:
    """Return the ordered list of commands to install deps and serve `repo_path`."""
    configs = FRAMEWORK_COMMANDS.get(framework, FRAMEWORK_COMMANDS["Static HTML"])
    for cfg in configs:
        check = cfg.get("check")
        if check and not (repo_path / check).exists():
            continue
        return [cmd.replace("{port}", str(port)) for cmd in cfg["commands"]]
    return [f"python3 -m http.server {port}"]


def run_install_commands(
    commands: List[str],
    repo_path: Path,
    logger: logging.Logger,
) -> Tuple[bool, str]:
    """Run all commands except the last (the serve command). Returns (ok, error_msg)."""
    for cmd in commands[:-1]:
        logger.info(f"  $ {cmd}")
        try:
            res = subprocess.run(
                cmd, shell=True, cwd=str(repo_path),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=INSTALL_TIMEOUT,
            )
            if res.returncode != 0:
                err = res.stderr.decode("utf-8", errors="ignore")[:500]
                return False, f"Command failed [{cmd}]: {err}"
        except subprocess.TimeoutExpired:
            return False, f"Timeout after {INSTALL_TIMEOUT}s: {cmd}"
        except Exception as exc:
            return False, f"Error running [{cmd}]: {exc}"
    return True, ""


def start_server(
    serve_cmd: str,
    repo_path: Path,
    port: int,
    logger: logging.Logger,
) -> Optional[subprocess.Popen]:
    """Start the serve command in the background. Returns process or None."""
    log_file = repo_path.parent / "server.log"
    logger.info(f"  Starting: {serve_cmd}")
    try:
        proc = subprocess.Popen(
            serve_cmd, shell=True, cwd=str(repo_path),
            stdout=open(log_file, "w"), stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        logger.info(f"  Server PID: {proc.pid}")

        # Poll until healthy
        deadline = time.time() + SERVER_HEALTH_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                logger.error(f"  Server exited early (code {proc.returncode})")
                return None
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://localhost:{port}", timeout=3) as r:
                    if r.status < 400:
                        logger.info(f"  Server healthy at localhost:{port}")
                        return proc
            except Exception:
                time.sleep(1)

        if proc.poll() is None:
            logger.warning("  Server still running but slow to respond — proceeding anyway")
            return proc

        logger.error("  Server never became healthy")
        return None
    except Exception as exc:
        logger.error(f"  Failed to start server: {exc}")
        return None


def stop_server(proc: Optional[subprocess.Popen], logger: logging.Logger) -> None:
    """Kill a server process group cleanly."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Cloudflare tunnel
# ---------------------------------------------------------------------------

class CloudflaredTunnel:
    """Context manager for a Cloudflare quick tunnel.

    Usage::
        with CloudflaredTunnel(port=8000, logger=logger) as url:
            if url:
                run_psi(url, ...)
    """

    def __init__(self, port: int, logger: logging.Logger):
        self.port = port
        self.logger = logger
        self._proc: Optional[subprocess.Popen] = None
        self.url: Optional[str] = None

    def start(self, max_retries: int = 3, initial_backoff: float = 90.0) -> Optional[str]:
        bin_path = shutil.which("cloudflared")
        if not bin_path:
            self.logger.warning("cloudflared not found in PATH — skipping tunnel")
            return None

        for attempt in range(1, max_retries + 1):
            backoff = initial_backoff * (2 ** (attempt - 1))  # 90s, 180s, 360s
            try:
                t0 = time.time()
                self._proc = subprocess.Popen(
                    [bin_path, "tunnel", "--url", f"http://localhost:{self.port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                deadline = time.time() + TUNNEL_WAIT_TIMEOUT
                url = None
                while time.time() < deadline:
                    line = self._proc.stderr.readline()
                    if not line:
                        if self._proc.poll() is not None:
                            break
                        time.sleep(0.2)
                        continue
                    self.logger.debug(f"cloudflared: {line.strip()}")
                    m = re.search(r"(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)", line)
                    if m:
                        url = m.group(1)
                        self.url = url
                        self.logger.info(f"Tunnel up: {self.url} → localhost:{self.port}")
                        time.sleep(TUNNEL_PROPAGATION_DELAY)
                        return self.url

                elapsed = time.time() - t0
                self.stop()

                if url is None and elapsed < 10:
                    # Cloudflare rejected the connection almost immediately — rate-limited.
                    if attempt < max_retries:
                        self.logger.warning(
                            f"  Tunnel attempt {attempt}/{max_retries} failed in {elapsed:.1f}s "
                            f"(likely rate-limited). Sleeping {backoff:.0f}s before retry…"
                        )
                        time.sleep(backoff)
                    else:
                        self.logger.error(
                            f"  Tunnel failed after {max_retries} attempts (rate-limited). Skipping."
                        )
                else:
                    self.logger.warning("  Tunnel URL not found within timeout")
                    break  # Timeout case — not rate-limiting, no point retrying

            except Exception as exc:
                self.logger.error(f"  Tunnel error: {exc}")
                self.stop()
                break

        return None


    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def __enter__(self) -> Optional[str]:
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()

# ---------------------------------------------------------------------------
# PSI API
# ---------------------------------------------------------------------------

def _cleanup_psi(data: Dict[str, Any]) -> Dict[str, Any]:
    """Remove base-64 screenshots from PSI response — mirrors psi.js cleanup()."""
    lr = data.get("lighthouseResult", {})
    audits = lr.get("audits", {})
    audits.pop("screenshot-thumbnails", None)
    audits.pop("final-screenshot", None)
    lr.pop("fullPageScreenshot", None)
    return data


def call_psi(
    url: str,
    strategy: str = "mobile",
    api_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Call the Google PageSpeed Insights API.

    Mirrors cwv-agent/src/tools/psi.js exactly:
      - Endpoint : https://www.googleapis.com/pagespeedonline/v5/runPagespeed
      - Key      : GOOGLE_PAGESPEED_INSIGHTS_API_KEY  (or api_key kwarg)
      - Strategy : mobile | desktop
      - Categories: not specified — API returns all defaults (performance,
                    best-practices, accessibility, seo, pwa)
      - Cleanup  : screenshot-thumbnails, final-screenshot, fullPageScreenshot
                   deleted from the response (same as psi.js cleanup())

    Returns cleaned JSON dict or None on failure.
    """
    key = api_key or PSI_API_KEY
    params: List[Tuple] = [("url", url), ("strategy", strategy)]
    if key:
        params.append(("key", key))
    try:
        r = requests.get(PSI_URL, params=params, timeout=PSI_REQUEST_TIMEOUT)
        r.raise_for_status()
        return _cleanup_psi(r.json())
    except Exception:
        return None


# Backward-compat alias
call_psi_google = call_psi



def call_psi_adobe(
    url: str,
    strategy: str = "mobile",
    categories: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Call the Adobe internal PSI service (fallback / alternative)."""
    params: List[Tuple] = [("url", url), ("strategy", strategy)]
    for cat in (categories or ["performance", "best-practices", "accessibility", "seo"]):
        params.append(("category", cat))
    try:
        r = requests.get(
            PSI_ADOBE_URL, params=params,
            headers={"User-Agent": "Spacecat/1.0"},
            timeout=PSI_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return None


def extract_metrics(psi_json: Dict[str, Any]) -> Dict[str, Any]:
    """Extract key CWV metrics and category scores from a raw PSI response."""
    lr = psi_json.get("lighthouseResult", {})
    audits = lr.get("audits", {})
    cats = lr.get("categories", {})

    def num(audit_id: str) -> Optional[float]:
        return audits.get(audit_id, {}).get("numericValue")

    return {
        "scores": {
            cid: round((cdata.get("score") or 0) * 100, 1)
            for cid, cdata in cats.items()
        },
        "metrics": {
            "LCP":        num("largest-contentful-paint"),
            "CLS":        num("cumulative-layout-shift"),
            "INP":        num("interaction-to-next-paint"),
            "FCP":        num("first-contentful-paint"),
            "TTFB":       num("server-response-time"),
            "SpeedIndex": num("speed-index"),
            "TBT":        num("total-blocking-time"),
        },
    }
