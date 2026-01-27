#!/usr/bin/env python3
"""
Comprehensive Repository Analysis Script for cwv-bench-v0 dataset.

Features:
- Deep code analysis (complexity, dependencies, build artifacts)
- Robust browser metrics (resources, timing, console logs)
- Advanced marketing stack detection
- Cross-platform deployment support
- Error recovery and retry logic
- Parallel processing
- Progress tracking and resumability
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple
from urllib.parse import urlparse
import hashlib

from datasets import load_dataset
from playwright.async_api import async_playwright, Browser, Page, Request, Response

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("analysis.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constants
DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
OUTPUT_FILE = "repo_analysis_detailed.jsonl"
PROGRESS_FILE = "analysis_progress.json"
TEMP_DIR = Path("temp_analysis_workspace")
MAX_RETRIES = 2
TIMEOUT_DEPLOY = 180
TIMEOUT_BROWSER = 45
LARGE_FILE_THRESHOLDS = {
    "image": 200 * 1024,      # 200KB
    "script": 300 * 1024,     # 300KB
    "style": 150 * 1024,      # 150KB
    "font": 100 * 1024,       # 100KB
    "other": 500 * 1024       # 500KB
}

# Code file extensions by category
CODE_EXTENSIONS = {
    "javascript": {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"},
    "python": {".py", ".pyw"},
    "markup": {".html", ".htm", ".xml", ".svg"},
    "style": {".css", ".scss", ".sass", ".less"},
    "config": {".json", ".yaml", ".yml", ".toml", ".ini", ".conf"},
    "markdown": {".md", ".mdx", ".markdown"},
    "ruby": {".rb", ".erb"},
    "php": {".php", ".phtml"},
    "go": {".go"},
    "rust": {".rs"},
    "java": {".java"},
    "c/c++": {".c", ".cpp", ".cc", ".h", ".hpp"},
}

# Build artifacts to detect
BUILD_ARTIFACTS = {
    ".min.js", ".min.css", ".bundle.js", ".chunk.js",
    ".map", ".wasm", ".br", ".gz"
}

# Marketing/Analytics patterns
MARKETING_PATTERNS = {
    "google_analytics": [
        r"google-analytics\.com",
        r"googletagmanager\.com",
        r"[\"']UA-\d+-\d+[\"']",
        r"gtag\(",
        r"ga\(",
        r"_gaq",
    ],
    "facebook_pixel": [
        r"facebook\.net",
        r"fbevents\.js",
        r"fbq\(",
        r"_fbp",
    ],
    "hotjar": [
        r"hotjar\.com",
        r"_hjid",
        r"hj\(",
    ],
    "segment": [
        r"segment\.(com|io)",
        r"analytics\.js",
        r"analytics\.identify",
    ],
    "mixpanel": [
        r"mixpanel\.com",
        r"mixpanel\.init",
    ],
    "intercom": [
        r"intercom\.io",
        r"Intercom\(",
    ],
    "hubspot": [
        r"hubspot\.com",
        r"_hsq",
    ],
    "linkedin": [
        r"linkedin\.com/analytics",
        r"_linkedin_partner_id",
    ],
    "twitter": [
        r"twitter\.com/i/adsct",
        r"twq\(",
    ],
    "clarity": [
        r"clarity\.ms",
        r"clarity\(",
    ],
    "amplitude": [
        r"amplitude\.com",
        r"amplitude\.getInstance",
    ],
}

# Framework Commands (cross-platform compatible)
FRAMEWORK_COMMANDS = {
    "Hexo": [
        {"check": "package.json", "commands": ["npm install", "npx hexo server -p {port}"]},
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "Jekyll": [
        {"check": "Gemfile", "commands": ["bundle install", "bundle exec jekyll serve --port {port} --host 0.0.0.0"]},
        {"check": "_config.yml", "commands": ["jekyll serve --port {port} --host 0.0.0.0"]},
    ],
    "Static HTML": [
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "Hugo": [
        {"check": "hugo.toml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "hugo.yaml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "hugo.yml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "config.toml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "config.yaml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "config.yml", "commands": ["hugo server -p {port} --bind 0.0.0.0"]},
        {"check": "public/index.html", "commands": ["python3 -m http.server {port} --directory public"]},
        {"check": "docs/index.html", "commands": ["python3 -m http.server {port} --directory docs"]},
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "Express": [
        {"check": "package.json", "commands": ["npm install", "PORT={port} npm start"]},
        {"check": "server.js", "commands": ["npm install", "PORT={port} node server.js"]},
        {"check": "app.js", "commands": ["npm install", "PORT={port} node app.js"]},
        {"check": "index.js", "commands": ["npm install", "PORT={port} node index.js"]},
        {"check": "backend/package.json", "commands": ["cd backend && npm install", "cd backend && PORT={port} npm start"]},
        {"check": "backend/server.js", "commands": ["cd backend && npm install", "cd backend && PORT={port} node server.js"]},
    ],
    "Next.js": [
        {"check": "package.json", "commands": ["npm install", "npm run build", "npm run start -- -p {port}"]},
        {"check": "website/package.json", "commands": ["cd website && npm install && npm run build", "cd website && npm run start -- -p {port}"]},
        {"check": "web/package.json", "commands": ["cd web && npm install && npm run build", "cd web && npm run start -- -p {port}"]},
        {"check": "out/index.html", "commands": ["python3 -m http.server {port} --directory out"]},
        {"check": "_next/static", "commands": ["python3 -m http.server {port}"]},
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "React": [
        {"check": "package.json", "commands": ["npm install", "PORT={port} npm start"]},
        {"check": "vite.config.ts", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        {"check": "vite.config.js", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        {"check": "dist/index.html", "commands": ["python3 -m http.server {port} --directory dist"]},
        {"check": "build/index.html", "commands": ["python3 -m http.server {port} --directory build"]},
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "Vue.js": [
        {"check": "package.json", "commands": ["npm install", "PORT={port} npm run serve"]},
        {"check": "vite.config.ts", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        {"check": "vite.config.js", "commands": ["npm install", "npm run dev -- --host 0.0.0.0 --port {port}"]},
        {"check": "dist/index.html", "commands": ["python3 -m http.server {port} --directory dist"]},
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "Pelican": [
        {
            "check": "pelicanconf.py",
            "commands": [
                "pip install --break-system-packages pelican markdown",
                "pelican content",
                "python3 -m http.server {port} --directory output",
            ],
        },
        {
            "check": "publishconf.py",
            "commands": [
                "pip install --break-system-packages pelican markdown",
                "pelican content -s publishconf.py",
                "python3 -m http.server {port} --directory output",
            ],
        },
        {"check": "output/index.html", "commands": ["python3 -m http.server {port} --directory output"]},
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "Quarto": [
        {"check": "_quarto.yml", "commands": ["quarto render", "python3 -m http.server {port} --directory _site"]},
        {"check": "_quarto.yaml", "commands": ["quarto render", "python3 -m http.server {port} --directory _site"]},
        {"check": "_site/index.html", "commands": ["python3 -m http.server {port} --directory _site"]},
        {"check": "docs/index.html", "commands": ["python3 -m http.server {port} --directory docs"]},
        {"check": "index.html", "commands": ["python3 -m http.server {port}"]},
    ],
    "Flask": [
        {
            "check": "app.py",
            "commands": [
                "pip install --break-system-packages flask",
                "FLASK_APP=app.py FLASK_ENV=development flask run --host=0.0.0.0 --port={port}",
            ],
        },
        {
            "check": "wsgi.py",
            "commands": [
                "pip install --break-system-packages flask",
                "FLASK_APP=wsgi.py FLASK_ENV=development flask run --host=0.0.0.0 --port={port}",
            ],
        },
        {"check": "static/index.html", "commands": ["python3 -m http.server {port} --directory static"]},
    ],
}


@dataclass
class CodeStats:
    """Detailed code statistics"""
    total_files: int = 0
    total_lines: int = 0
    total_size: int = 0
    files_by_language: Dict[str, int] = None
    lines_by_language: Dict[str, int] = None
    size_by_language: Dict[str, int] = None
    build_artifacts: List[str] = None
    dependencies: Dict[str, List[str]] = None
    file_details: List[Dict] = None
    
    def __post_init__(self):
        if self.files_by_language is None:
            self.files_by_language = {}
        if self.lines_by_language is None:
            self.lines_by_language = {}
        if self.size_by_language is None:
            self.size_by_language = {}
        if self.build_artifacts is None:
            self.build_artifacts = []
        if self.dependencies is None:
            self.dependencies = {}
        if self.file_details is None:
            self.file_details = []


@dataclass
class BrowserMetrics:
    """Browser analysis metrics"""
    num_images: int = 0
    num_scripts: int = 0
    num_stylesheets: int = 0
    num_fonts: int = 0
    large_resources: List[Dict] = None
    third_party_domains: List[str] = None
    marketing_stacks: Dict[str, List[str]] = None
    console_errors: List[str] = None
    console_warnings: List[str] = None
    resource_timing: Dict = None
    total_requests: int = 0
    total_transfer_size: int = 0
    failed_requests: int = 0
    redirect_count: int = 0
    
    def __post_init__(self):
        if self.large_resources is None:
            self.large_resources = []
        if self.third_party_domains is None:
            self.third_party_domains = []
        if self.marketing_stacks is None:
            self.marketing_stacks = {}
        if self.console_errors is None:
            self.console_errors = []
        if self.console_warnings is None:
            self.console_warnings = []
        if self.resource_timing is None:
            self.resource_timing = {}


def find_available_port(start_port: int = 8000) -> int:
    """Find an available port starting from start_port"""
    port = start_port
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("No free ports available")


def check_server_health(port: int, timeout: int = 30) -> bool:
    """Check if server is responding on the given port"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()
            if result == 0:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def get_file_language(file_path: Path) -> Optional[str]:
    """Determine programming language from file extension"""
    ext = file_path.suffix.lower()
    for lang, extensions in CODE_EXTENSIONS.items():
        if ext in extensions:
            return lang
    return None


def is_build_artifact(file_path: Path) -> bool:
    """Check if file is a build artifact"""
    name = file_path.name.lower()
    for pattern in BUILD_ARTIFACTS:
        if pattern in name:
            return True
    return False


def calculate_code_complexity(content: str, language: str) -> Dict:
    """Basic code complexity metrics"""
    lines = content.split('\n')
    
    complexity = {
        "nesting_depth": 0,
        "function_count": 0,
        "class_count": 0,
        "comment_lines": 0,
        "blank_lines": 0,
    }
    
    current_depth = 0
    max_depth = 0
    
    for line in lines:
        stripped = line.strip()
        
        # Blank lines
        if not stripped:
            complexity["blank_lines"] += 1
            continue
        
        # Comments (basic detection)
        if language in ["javascript", "c/c++", "java", "php", "go", "rust"]:
            if stripped.startswith("//") or stripped.startswith("/*"):
                complexity["comment_lines"] += 1
        elif language == "python":
            if stripped.startswith("#"):
                complexity["comment_lines"] += 1
        elif language in ["markup", "style"]:
            if "<!--" in stripped or "/*" in stripped:
                complexity["comment_lines"] += 1
        
        # Nesting depth (approximate)
        if "{" in line:
            current_depth += line.count("{")
        if "}" in line:
            current_depth -= line.count("}")
        max_depth = max(max_depth, current_depth)
        
        # Function/class detection (basic)
        if language == "javascript":
            if re.search(r'\bfunction\s+\w+', line) or re.search(r'\w+\s*\(.*\)\s*=>', line):
                complexity["function_count"] += 1
            if re.search(r'\bclass\s+\w+', line):
                complexity["class_count"] += 1
        elif language == "python":
            if re.match(r'^\s*def\s+\w+', line):
                complexity["function_count"] += 1
            if re.match(r'^\s*class\s+\w+', line):
                complexity["class_count"] += 1
    
    complexity["nesting_depth"] = max_depth
    return complexity


def parse_dependencies(repo_path: Path) -> Dict[str, List[str]]:
    """Extract dependencies from package files"""
    deps = {}
    
    # package.json (Node.js)
    package_json = repo_path / "package.json"
    if package_json.exists():
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                npm_deps = []
                if "dependencies" in data:
                    npm_deps.extend(list(data["dependencies"].keys()))
                if "devDependencies" in data:
                    npm_deps.extend(list(data["devDependencies"].keys()))
                deps["npm"] = npm_deps
        except Exception as e:
            logger.warning(f"Error parsing package.json: {e}")
    
    # requirements.txt (Python)
    requirements = repo_path / "requirements.txt"
    if requirements.exists():
        try:
            with open(requirements, "r", encoding="utf-8") as f:
                py_deps = [line.split("==")[0].split(">=")[0].strip() 
                          for line in f if line.strip() and not line.startswith("#")]
                deps["pip"] = py_deps
        except Exception as e:
            logger.warning(f"Error parsing requirements.txt: {e}")
    
    # Gemfile (Ruby)
    gemfile = repo_path / "Gemfile"
    if gemfile.exists():
        try:
            with open(gemfile, "r", encoding="utf-8") as f:
                gem_deps = re.findall(r"gem\s+['\"]([^'\"]+)['\"]", f.read())
                deps["gem"] = gem_deps
        except Exception as e:
            logger.warning(f"Error parsing Gemfile: {e}")
    
    # go.mod (Go)
    go_mod = repo_path / "go.mod"
    if go_mod.exists():
        try:
            with open(go_mod, "r", encoding="utf-8") as f:
                go_deps = re.findall(r'require\s+([^\s]+)', f.read())
                deps["go"] = go_deps
        except Exception as e:
            logger.warning(f"Error parsing go.mod: {e}")
    
    return deps


def analyze_code_files(repo_path: Path) -> CodeStats:
    """Deep analysis of code files in repository"""
    stats = CodeStats()
    
    # Parse dependencies first
    stats.dependencies = parse_dependencies(repo_path)
    
    skip_dirs = {".git", "node_modules", "vendor", "venv", "__pycache__", 
                 ".next", "dist", "build", "out", ".cache", "target"}
    
    for root, dirs, files in os.walk(repo_path):
        # Skip common directories
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        
        for file in files:
            file_path = Path(root) / file
            rel_path = str(file_path.relative_to(repo_path))
            
            # Check if build artifact
            if is_build_artifact(file_path):
                stats.build_artifacts.append(rel_path)
                continue
            
            # Get language
            language = get_file_language(file_path)
            if not language:
                continue
            
            try:
                size = file_path.stat().st_size
                
                # Read file content
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    lines = content.count('\n') + 1
                
                # Calculate complexity
                complexity = calculate_code_complexity(content, language)
                
                # Update stats
                stats.total_files += 1
                stats.total_lines += lines
                stats.total_size += size
                
                stats.files_by_language[language] = stats.files_by_language.get(language, 0) + 1
                stats.lines_by_language[language] = stats.lines_by_language.get(language, 0) + lines
                stats.size_by_language[language] = stats.size_by_language.get(language, 0) + size
                
                # Store detailed info for significant files (>50 lines or >10KB)
                if lines > 50 or size > 10240:
                    stats.file_details.append({
                        "path": rel_path,
                        "language": language,
                        "size": size,
                        "lines": lines,
                        "complexity": complexity,
                    })
                
            except Exception as e:
                logger.debug(f"Error analyzing {rel_path}: {e}")
    
    return stats


def categorize_resource_type(url: str, resource_type: Optional[str]) -> str:
    """Categorize resource by URL and type"""
    url_lower = url.lower()
    
    if resource_type in ["image", "img"]:
        return "image"
    if resource_type in ["script", "xhr", "fetch"]:
        return "script"
    if resource_type in ["stylesheet", "css"]:
        return "style"
    if resource_type == "font":
        return "font"
    
    # Fallback to URL extension
    if any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico"]):
        return "image"
    if any(ext in url_lower for ext in [".js", ".mjs"]):
        return "script"
    if ".css" in url_lower:
        return "style"
    if any(ext in url_lower for ext in [".woff", ".woff2", ".ttf", ".otf", ".eot"]):
        return "font"
    
    return "other"


async def analyze_browser_metrics(page: Page, url: str) -> BrowserMetrics:
    """Comprehensive browser-based analysis"""
    metrics = BrowserMetrics()
    
    # Track resources
    resources = []
    third_party_domains = set()
    console_messages = {"error": [], "warning": []}
    
    # Get page domain
    page_domain = urlparse(url).netloc
    
    # Network monitoring
    async def on_request(request: Request):
        try:
            req_url = request.url
            domain = urlparse(req_url).netloc
            
            # Track third-party
            if domain and domain != page_domain and "localhost" not in domain and "127.0.0.1" not in domain:
                third_party_domains.add(domain)
            
            metrics.total_requests += 1
            
            resources.append({
                "url": req_url,
                "type": request.resource_type,
                "method": request.method,
            })
            
        except Exception as e:
            logger.debug(f"Request tracking error: {e}")
    
    async def on_response(response: Response):
        try:
            req_url = response.url
            status = response.status
            
            # Track redirects
            if 300 <= status < 400:
                metrics.redirect_count += 1
            
            # Track failures
            if status >= 400:
                metrics.failed_requests += 1
            
            # Get size
            headers = await response.all_headers()
            content_length = headers.get("content-length")
            
            if content_length:
                try:
                    size = int(content_length)
                    metrics.total_transfer_size += size
                    
                    # Categorize and check if large
                    resource_type = categorize_resource_type(req_url, response.request.resource_type)
                    threshold = LARGE_FILE_THRESHOLDS.get(resource_type, LARGE_FILE_THRESHOLDS["other"])
                    
                    if size > threshold:
                        metrics.large_resources.append({
                            "url": req_url,
                            "size": size,
                            "type": resource_type,
                        })
                except ValueError:
                    pass
                    
        except Exception as e:
            logger.debug(f"Response tracking error: {e}")
    
    # Console monitoring
    def on_console(msg):
        msg_type = msg.type
        text = msg.text
        
        if msg_type == "error":
            console_messages["error"].append(text[:200])  # Limit length
        elif msg_type == "warning":
            console_messages["warning"].append(text[:200])
    
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("console", on_console)
    
    try:
        # Navigate and wait for network idle
        await page.goto(url, wait_until="networkidle", timeout=TIMEOUT_BROWSER * 1000)
        
        # Give it a bit more time for lazy-loaded content
        await asyncio.sleep(2)
        
        # Count elements
        metrics.num_images = await page.locator("img").count()
        metrics.num_scripts = await page.locator("script[src]").count()
        metrics.num_stylesheets = await page.locator("link[rel='stylesheet']").count()
        
        # Get page content for marketing detection
        content = await page.content()
        
        # Detect marketing stacks
        for stack_name, patterns in MARKETING_PATTERNS.items():
            matches = []
            for pattern in patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    matches.append(pattern)
            if matches:
                metrics.marketing_stacks[stack_name] = matches
        
        # Get performance timing
        try:
            timing = await page.evaluate("""
                () => {
                    const perf = performance.timing;
                    const nav = performance.navigation;
                    return {
                        dns: perf.domainLookupEnd - perf.domainLookupStart,
                        tcp: perf.connectEnd - perf.connectStart,
                        request: perf.responseStart - perf.requestStart,
                        response: perf.responseEnd - perf.responseStart,
                        dom: perf.domComplete - perf.domLoading,
                        load: perf.loadEventEnd - perf.navigationStart,
                        domContentLoaded: perf.domContentLoadedEventEnd - perf.navigationStart,
                        redirectCount: nav.redirectCount || 0
                    };
                }
            """)
            metrics.resource_timing = timing
        except Exception as e:
            logger.debug(f"Performance timing error: {e}")
        
    except Exception as e:
        logger.warning(f"Browser analysis error for {url}: {e}")
    
    # Finalize metrics
    metrics.third_party_domains = list(third_party_domains)
    metrics.console_errors = console_messages["error"][:10]  # Limit to 10
    metrics.console_warnings = console_messages["warning"][:10]
    
    return metrics


async def deploy_and_analyze(repo_path: Path, framework: str, browser: Browser) -> Optional[BrowserMetrics]:
    """Deploy repository and analyze with browser"""
    
    commands_config = FRAMEWORK_COMMANDS.get(framework, FRAMEWORK_COMMANDS["Static HTML"])
    deployment_cmd = None
    
    # Find matching deployment strategy
    for config in commands_config:
        check_file = config["check"]
        if (repo_path / check_file).exists():
            deployment_cmd = config["commands"]
            logger.info(f"Using deployment strategy for: {check_file}")
            break
    
    if not deployment_cmd:
        logger.warning(f"No deployment strategy found, using fallback")
        deployment_cmd = ["python3 -m http.server {port}"]
    
    port = find_available_port()
    server_process = None
    
    try:
        # Run setup/build commands (all except last)
        for cmd in deployment_cmd[:-1]:
            cmd = cmd.replace("{port}", str(port))
            logger.info(f"Running setup: {cmd}")
            
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(repo_path),
                    timeout=TIMEOUT_DEPLOY,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    logger.warning(f"Setup command failed (continuing anyway): {result.stderr[:200]}")
            except subprocess.TimeoutExpired:
                logger.warning(f"Setup command timed out (continuing anyway)")
            except Exception as e:
                logger.warning(f"Setup command error: {e}")
        
        # Start server (last command)
        final_cmd = deployment_cmd[-1].replace("{port}", str(port))
        logger.info(f"Starting server: {final_cmd}")
        
        # Set environment variables for port
        env = os.environ.copy()
        env["PORT"] = str(port)
        
        server_process = subprocess.Popen(
            final_cmd,
            shell=True,
            cwd=str(repo_path),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid if os.name != "nt" else None,
        )
        
        # Wait for server to be ready
        if not check_server_health(port, timeout=20):
            logger.error(f"Server failed to start on port {port}")
            return None
        
        logger.info(f"Server running on port {port}")
        
        # Analyze with browser
        page = await browser.new_page()
        metrics = await analyze_browser_metrics(page, f"http://127.0.0.1:{port}")
        await page.close()
        
        return metrics
        
    except Exception as e:
        logger.error(f"Deployment error: {e}")
        return None
        
    finally:
        # Kill server
        if server_process:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(server_process.pid), signal.SIGTERM)
                else:
                    server_process.terminate()
                server_process.wait(timeout=5)
            except Exception as e:
                logger.debug(f"Error killing server: {e}")


def clone_repository(repo_url: str, target_path: Path, timeout: int = 180) -> bool:
    """Clone git repository with timeout"""
    try:
        logger.info(f"Cloning {repo_url}...")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(target_path)],
            capture_output=True,
            timeout=timeout,
            text=True
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error(f"Clone timeout for {repo_url}")
        return False
    except Exception as e:
        logger.error(f"Clone error: {e}")
        return False


async def process_repo(row: Dict[str, Any], browser: Browser, retry_count: int = 0) -> Optional[Dict]:
    """Process a single repository with retry logic"""
    
    repo_id = row.get("REPO_ID") or row.get("repo_id")
    if not repo_id:
        logger.error("No REPO_ID found in row")
        return None
    
    # Ensure it's a full GitHub URL
    if "github.com" not in repo_id:
        repo_url = f"https://github.com/{repo_id}"
    else:
        repo_url = repo_id
    
    framework = row.get("FRAMEWORK", "Unknown")
    repo_name = repo_url.split("/")[-1].replace(".git", "")
    repo_hash = hashlib.md5(repo_url.encode()).hexdigest()[:8]
    repo_path = TEMP_DIR / f"{repo_name}_{repo_hash}"
    
    result = {
        "repo_url": repo_url,
        "repo_id": repo_id,
        "framework": framework,
        "status": "failed",
        "error": None,
        "retry_count": retry_count,
        "code_stats": None,
        "browser_metrics": None,
    }
    
    try:
        # Clean up if exists
        if repo_path.exists():
            shutil.rmtree(repo_path, ignore_errors=True)
        
        # 1. Clone
        if not clone_repository(repo_url, repo_path):
            result["error"] = "Clone failed"
            return result
        
        # 2. Analyze code
        logger.info(f"Analyzing code for {repo_name}...")
        code_stats = analyze_code_files(repo_path)
        result["code_stats"] = asdict(code_stats)
        
        # 3. Deploy and analyze with browser
        logger.info(f"Deploying {repo_name}...")
        browser_metrics = await deploy_and_analyze(repo_path, framework, browser)
        
        if browser_metrics:
            result["browser_metrics"] = asdict(browser_metrics)
            result["status"] = "success"
        else:
            result["error"] = "Deployment/browser analysis failed"
            result["status"] = "partial"  # We got code stats at least
        
    except Exception as e:
        logger.error(f"Error processing {repo_name}: {e}")
        result["error"] = str(e)
        
        # Retry logic
        if retry_count < MAX_RETRIES:
            logger.info(f"Retrying {repo_name} (attempt {retry_count + 1}/{MAX_RETRIES})")
            await asyncio.sleep(2)  # Brief delay before retry
            return await process_repo(row, browser, retry_count + 1)
    
    finally:
        # Cleanup
        if repo_path.exists():
            try:
                shutil.rmtree(repo_path)
            except Exception as e:
                logger.debug(f"Cleanup error for {repo_path}: {e}")
    
    return result


def load_progress() -> Set[str]:
    """Load already processed repo IDs"""
    if not Path(PROGRESS_FILE).exists():
        return set()
    
    try:
        with open(PROGRESS_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_progress(processed: Set[str]):
    """Save progress"""
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump(list(processed), f)
    except Exception as e:
        logger.error(f"Error saving progress: {e}")


async def main():
    """Main execution"""
    
    # Setup
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    TEMP_DIR.mkdir(exist_ok=True)
    
    # Load progress
    processed = load_progress()
    logger.info(f"Already processed: {len(processed)} repos")
    
    # Load dataset
    logger.info(f"Loading dataset {DATASET_NAME}...")
    dataset = load_dataset(DATASET_NAME, split="train")
    logger.info(f"Dataset loaded: {len(dataset)} repos")
    
    # Start browser
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        # Process repos
        with open(OUTPUT_FILE, "a") as f:
            for i, row in enumerate(dataset):
                repo_id = row.get("REPO_ID") or row.get("repo_id")
                
                # Skip if already processed
                if repo_id in processed:
                    logger.info(f"Skipping {i+1}/{len(dataset)}: {repo_id} (already processed)")
                    continue
                
                logger.info(f"Processing {i+1}/{len(dataset)}: {repo_id}")
                
                analysis = await process_repo(row, browser)
                
                if analysis:
                    f.write(json.dumps(analysis) + "\n")
                    f.flush()
                    
                    # Mark as processed
                    processed.add(repo_id)
                    
                    # Save progress every 10 repos
                    if len(processed) % 10 == 0:
                        save_progress(processed)
                
                # Small delay between repos
                await asyncio.sleep(1)
        
        await browser.close()
    
    # Final save
    save_progress(processed)
    
    # Cleanup
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    
    logger.info(f"Analysis complete! Results saved to {OUTPUT_FILE}")
    logger.info(f"Total processed: {len(processed)} repos")


if __name__ == "__main__":
    asyncio.run(main())