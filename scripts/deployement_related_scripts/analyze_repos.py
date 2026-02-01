#!/usr/bin/env python3
"""
Comprehensive Repository Analysis Script for cwv-bench-v0 dataset with PARALLEL PROCESSING.

Features:
- **PARALLEL WORKERS**: Process multiple repos concurrently
- Deep code analysis (complexity, dependencies, build artifacts)
- Robust browser metrics (resources, timing, console logs)
- Advanced marketing stack detection
- Cross-platform deployment support
- Error recovery and retry logic
- Progress tracking and resumability
- Worker-safe file I/O with locks
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
import threading
import argparse

from datasets import load_dataset
from playwright.async_api import async_playwright, Browser, Page, Request, Response
from tqdm.asyncio import tqdm
import logging
import sys

class TqdmLoggingHandler(logging.Handler):
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)

    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)

# Configure logging
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | [Worker %(thread)d] %(message)s",
    handlers=[
        logging.FileHandler("analysis_parallel.log"),
        TqdmLoggingHandler()
    ]
)
logger = logging.getLogger(__name__)

# Constants
DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
OUTPUT_FILE = "repo_analysis_detailed_parallel.jsonl"
PROGRESS_FILE = "analysis_progress_parallel.json"
TEMP_DIR = Path("temp_analysis_workspace")
MAX_RETRIES = 2
TIMEOUT_DEPLOY = 180
TIMEOUT_BROWSER = 45
NUM_WORKERS = 4  # Number of parallel workers

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


class ProgressTracker:
    """Thread-safe progress tracking"""
    
    def __init__(self, progress_file: str):
        self.progress_file = progress_file
        self.processed = set()
        self.lock = threading.Lock()
        self.load()
    
    def load(self):
        """Load progress from file"""
        if not Path(self.progress_file).exists():
            return
        
        try:
            with open(self.progress_file, "r") as f:
                self.processed = set(json.load(f))
            logger.info(f"Loaded {len(self.processed)} processed repos from progress file")
        except Exception as e:
            logger.error(f"Error loading progress: {e}")
    
    def save(self):
        """Save progress to file"""
        with self.lock:
            try:
                with open(self.progress_file, "w") as f:
                    json.dump(list(self.processed), f)
            except Exception as e:
                logger.error(f"Error saving progress: {e}")
    
    def mark_processed(self, repo_id: str):
        """Mark a repo as processed"""
        with self.lock:
            self.processed.add(repo_id)
    
    def is_processed(self, repo_id: str) -> bool:
        """Check if repo is already processed"""
        with self.lock:
            return repo_id in self.processed
    
    def get_count(self) -> int:
        """Get count of processed repos"""
        with self.lock:
            return len(self.processed)


class ResultWriter:
    """Thread-safe result writer"""
    
    def __init__(self, output_file: str):
        self.output_file = output_file
        self.lock = threading.Lock()
        self.file_handle = open(output_file, "a")
    
    def write(self, result: Dict):
        """Write result to file"""
        with self.lock:
            self.file_handle.write(json.dumps(result) + "\n")
            self.file_handle.flush()
    
    def close(self):
        """Close file handle"""
        self.file_handle.close()


def find_available_port(start_port: int, end_port: int) -> int:
    """Find an available port within range"""
    for port in range(start_port, end_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free ports available in range {start_port}-{end_port}")


async def check_server_health(port: int, timeout: int = 30) -> bool:
    """Check if server is responding on the given port (Async)"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.5)
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


async def deploy_and_analyze(repo_path: Path, framework: str, browser: Browser, worker_id: int) -> Optional[BrowserMetrics]:
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
    
    # Assign port range based on worker_id to avoid collisions
    # Worker 0: 8000-8999, Worker 1: 9000-9999, etc.
    start_port = 8000 + (worker_id * 1000)
    end_port = start_port + 1000
    try:
        port = find_available_port(start_port, end_port)
    except RuntimeError as e:
        logger.error(str(e))
        return None

    server_process = None
    
    try:
        # Run setup/build commands (all except last)
        for cmd in deployment_cmd[:-1]:
            cmd = cmd.replace("{port}", str(port))
            logger.info(f"Running setup: {cmd}")
            
            try:
                process = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=str(repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT_DEPLOY)
                
                if process.returncode != 0:
                    logger.warning(f"Setup command failed (continuing anyway): {stderr.decode()[:200]}")
            except asyncio.TimeoutError:
                logger.warning(f"Setup command timed out (continuing anyway)")
                if process:
                    try:
                        process.kill()
                    except:
                        pass
            except Exception as e:
                logger.warning(f"Setup command error: {e}")
        
        # Start server (last command)
        final_cmd = deployment_cmd[-1].replace("{port}", str(port))
        logger.info(f"Starting server: {final_cmd}")
        
        # Set environment variables for port
        env = os.environ.copy()
        env["PORT"] = str(port)
        
        # Start server process
        server_process = await asyncio.create_subprocess_shell(
            final_cmd,
            cwd=str(repo_path),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=os.setsid if os.name != "nt" else None,
        )
        
        # Wait for server to be ready
        if not await check_server_health(port, timeout=20):
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
                await server_process.wait()
            except Exception as e:
                logger.debug(f"Error killing server: {e}")


async def clone_repository(repo_url: str, target_path: Path, timeout: int = 180) -> bool:
    """Clone git repository with timeout (Async)"""
    try:
        logger.info(f"Cloning {repo_url}...")
        process = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", repo_url, str(target_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode == 0:
                return True
            else:
                logger.error(f"Clone failed: {stderr.decode()}")
                return False
        except asyncio.TimeoutError:
            process.kill()
            logger.error(f"Clone timeout for {repo_url}")
            return False
            
    except Exception as e:
        logger.error(f"Clone error: {e}")
        return False


async def process_repo(row: Dict[str, Any], browser: Browser, worker_id: int, temp_dir: Path, retry_count: int = 0) -> Optional[Dict]:
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
    repo_path = temp_dir / f"worker{worker_id}_{repo_name}_{repo_hash}"
    
    result = {
        "repo_url": repo_url,
        "repo_id": repo_id,
        "framework": framework,
        "worker_id": worker_id,
        "status": "failed",
        "error": None,
        "retry_count": retry_count,
        "code_stats": None,
        "browser_metrics": None,
    }
    
    try:
        # Clean up if exists
        if repo_path.exists():
            await asyncio.to_thread(shutil.rmtree, repo_path, ignore_errors=True)
        
        # 1. Clone
        if not await clone_repository(repo_url, repo_path):
            result["error"] = "Clone failed"
            return result
        
        # 2. Analyze code (CPU bound, run in thread)
        logger.info(f"[Worker {worker_id}] Analyzing code for {repo_name}...")
        loop = asyncio.get_running_loop()
        code_stats = await loop.run_in_executor(None, analyze_code_files, repo_path)
        result["code_stats"] = asdict(code_stats)
        
        # 3. Deploy and analyze with browser
        logger.info(f"[Worker {worker_id}] Deploying {repo_name}...")
        browser_metrics = await deploy_and_analyze(repo_path, framework, browser, worker_id)
        
        if browser_metrics:
            result["browser_metrics"] = asdict(browser_metrics)
            result["status"] = "success"
        else:
            result["error"] = "Deployment/browser analysis failed"
            result["status"] = "partial"  # We got code stats at least
        
    except Exception as e:
        logger.error(f"[Worker {worker_id}] Error processing {repo_name}: {e}")
        result["error"] = str(e)
        
        # Retry logic
        if retry_count < MAX_RETRIES:
            logger.info(f"[Worker {worker_id}] Retrying {repo_name} (attempt {retry_count + 1}/{MAX_RETRIES})")
            await asyncio.sleep(2)  # Brief delay before retry
            return await process_repo(row, browser, worker_id, temp_dir, retry_count + 1)
    
    finally:
        # Cleanup
        if repo_path.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, repo_path)
            except Exception as e:
                logger.debug(f"Cleanup error for {repo_path}: {e}")
    
    return result


async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    progress_tracker: ProgressTracker,
    result_writer: ResultWriter,
    pbar: tqdm,
    temp_dir: Path,
):
    """Worker coroutine to process repos from queue"""
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        logger.info(f"[Worker {worker_id}] Started")
        
        while True:
            try:
                # Get task from queue
                row, index, total = await queue.get()
                
                if row is None:  # Poison pill to stop worker
                    break
                
                repo_id = row.get("REPO_ID") or row.get("repo_id")
                
                # Skip if already processed
                if progress_tracker.is_processed(repo_id):
                    logger.info(f"[Worker {worker_id}] Skipping {index}/{total}: {repo_id} (already processed)")
                    pbar.update(1)
                    queue.task_done()
                    continue
                
                logger.info(f"[Worker {worker_id}] Processing {index}/{total}: {repo_id}")
                
                # Process the repo
                analysis = await process_repo(row, browser, worker_id, temp_dir)
                
                if analysis:
                    # Write result
                    result_writer.write(analysis)
                    
                    # Mark as processed
                    progress_tracker.mark_processed(repo_id)
                    
                    # Save progress periodically
                    if progress_tracker.get_count() % 10 == 0:
                        progress_tracker.save()
                
                pbar.update(1)
                queue.task_done()
                
                # Small delay between repos
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"[Worker {worker_id}] Worker error: {e}")
                pbar.update(1)
                queue.task_done()
        
        await browser.close()
        logger.info(f"[Worker {worker_id}] Stopped")


async def main():
    """Main execution with parallel workers"""
    
    # Setup
    # Removed global TEMP_DIR cleanup, now handled per-run
    # if TEMP_DIR.exists():
    #     shutil.rmtree(TEMP_DIR, ignore_errors=True)
    # TEMP_DIR.mkdir(exist_ok=True)
    
    # Initialize progress tracker and result writer
    # Moved after args parsing
    
    # Parse arguments
    parser = argparse.ArgumentParser(description="Analyze repositories with parallel workers")
    parser.add_argument("--start_index", type=int, default=0, help="Start index for dataset slice")
    parser.add_argument("--end_index", type=int, default=None, help="End index for dataset slice")
    args = parser.parse_args()
    
    # Generate unique paths based on slice
    suffix = f"_{args.start_index}_{args.end_index}" if args.end_index is not None else ""
    
    output_file = f"repo_analysis_detailed{suffix}.jsonl"
    progress_file = f"analysis_progress{suffix}.json"
    temp_dir = Path(f"temp_analysis_workspace{suffix}")
    
    # Setup temp dir
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(exist_ok=True)
    
    # Initialize progress tracker and result writer
    progress_tracker = ProgressTracker(progress_file)
    result_writer = ResultWriter(output_file)
    
    logger.info(f"Output: {output_file}")
    logger.info(f"Temp Dir: {temp_dir}")
    logger.info(f"Already processed: {progress_tracker.get_count()} repos")
    logger.info(f"Starting {NUM_WORKERS} parallel workers")
    
    # Load dataset
    logger.info(f"Loading dataset {DATASET_NAME}...")
    dataset = load_dataset(DATASET_NAME, split="train")
    
    # Slice dataset if requested
    total_len = len(dataset)
    start_idx = args.start_index
    end_idx = args.end_index if args.end_index is not None else total_len
    
    # Validate indices
    if start_idx < 0: start_idx = 0
    if end_idx > total_len: end_idx = total_len
    
    if start_idx != 0 or end_idx != total_len:
        logger.info(f"Processing slice: {start_idx} to {end_idx} (Total: {total_len})")
        # HF Dataset slicing is slightly different, usually use select
        dataset = dataset.select(range(start_idx, end_idx))
    
    logger.info(f"Dataset loaded: {len(dataset)} repos to process")
    
    # Create queue and add all repos
    queue = asyncio.Queue()
    
    # Initialize progress bar
    pbar = tqdm(total=len(dataset), desc="Analyzing Repos", unit="repo")
    
    for i, row in enumerate(dataset):
        # Calculate original index for logging
        original_index = start_idx + i + 1
        await queue.put((row, original_index, total_len))
    
    # Add poison pills to stop workers
    for _ in range(NUM_WORKERS):
        await queue.put((None, None, None))
    
    # Start workers
    workers = [
        asyncio.create_task(worker(i, queue, progress_tracker, result_writer, pbar, temp_dir))
        for i in range(NUM_WORKERS)
    ]
    
    # Wait for all tasks to complete
    await queue.join()
    
    # Wait for workers to finish
    await asyncio.gather(*workers)
    
    pbar.close()
    
    # Final save and cleanup
    progress_tracker.save()
    result_writer.close()
    
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    logger.info(f"Analysis complete! Results saved to {output_file}")
    logger.info(f"Total processed: {progress_tracker.get_count()} repos")


if __name__ == "__main__":
    asyncio.run(main())