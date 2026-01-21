#!/usr/bin/env python3
"""
Repo Landscape Analyzer

Clones repositories and uses Aider AI to generate comprehensive
framework, dependency, and deployment metadata in structured JSON format.

Features:
- Dataset processing with checkpointing
- Temporary repo cloning (cleanup after analysis)
- AI-powered framework detection
- Structured JSON output for automation
- Resume capability on interruption
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
AZURE_DEPLOYMENT = os.getenv("AZURE_DEPLOYMENT", "gpt-5")


class RepoAnalyzer:
    """Analyzes repositories for framework and dependency information"""
    
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path(tempfile.mkdtemp(prefix="repo_analysis_"))
        self.processed_repos = set()
        self.results = []
        self._lock = Lock()  # Thread lock for thread-safe operations
        self.load_checkpoint()
        
        logger.info(f"📂 Output directory: {self.output_dir}")
        logger.info(f"📂 Temp clone directory: {self.temp_dir}")
    
    def load_checkpoint(self):
        """Load checkpoint if exists"""
        checkpoint_file = self.output_dir / "checkpoint.json"
        if checkpoint_file.exists():
            try:
                with open(checkpoint_file, 'r') as f:
                    data = json.load(f)
                    self.processed_repos = set(data.get("processed_repos", []))
                    self.results = data.get("results", [])
                logger.info(f"✅ Loaded checkpoint: {len(self.processed_repos)} repos already processed")
            except Exception as e:
                logger.warning(f"⚠️ Could not load checkpoint: {e}")
    
    def save_checkpoint(self):
        """Save current progress"""
        checkpoint_file = self.output_dir / "checkpoint.json"
        data = {
            "processed_repos": list(self.processed_repos),
            "results": self.results,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_processed": len(self.processed_repos)
        }
        with open(checkpoint_file, 'w') as f:
            json.dump(data, f, indent=2)
        logger.debug(f"💾 Checkpoint saved: {len(self.processed_repos)} repos")
    
    def clone_repository(self, repo_name: str) -> Optional[Path]:
        """Clone a repository to temp directory"""
        clone_path = self.temp_dir / repo_name.replace("/", "_")
        
        if clone_path.exists():
            shutil.rmtree(clone_path)
        
        github_url = f"https://github.com/{repo_name}.git"
        logger.info(f"📥 Cloning {github_url}...")
        
        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", github_url, str(clone_path)],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                logger.error(f"❌ Clone failed: {result.stderr[:200]}")
                return None
            
            logger.info(f"✅ Cloned to {clone_path}")
            return clone_path
            
        except subprocess.TimeoutExpired:
            logger.error(f"❌ Clone timeout")
            return None
        except Exception as e:
            logger.error(f"❌ Clone error: {e}")
            return None
    
    def get_directory_tree(self, repo_path: Path, max_depth: int = 3, max_items: int = 100) -> str:
        """Generate a directory tree structure for deeper repo understanding"""
        tree_lines = []
        item_count = 0
        
        def walk_dir(path: Path, prefix: str = "", depth: int = 0):
            nonlocal item_count
            if depth > max_depth or item_count >= max_items:
                return
            
            try:
                # Sort directories first, then files
                entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                # Filter out common non-essential directories
                skip_dirs = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.idea', 
                            '.vscode', 'dist', 'build', '.cache', 'coverage', '.next'}
                entries = [e for e in entries if e.name not in skip_dirs or depth == 0]
                
                for i, entry in enumerate(entries):
                    if item_count >= max_items:
                        tree_lines.append(f"{prefix}... (truncated)")
                        return
                    
                    is_last = i == len(entries) - 1
                    connector = "└── " if is_last else "├── "
                    tree_lines.append(f"{prefix}{connector}{entry.name}")
                    item_count += 1
                    
                    if entry.is_dir():
                        extension = "    " if is_last else "│   "
                        walk_dir(entry, prefix + extension, depth + 1)
            except PermissionError:
                pass
        
        walk_dir(repo_path)
        return "\n".join(tree_lines) if tree_lines else "(empty or inaccessible)"
    
    def sample_source_files(self, repo_path: Path) -> Dict[str, str]:
        """Sample key source files from subdirectories for deeper analysis"""
        samples = {}
        
        # Priority source files to look for in subdirectories
        source_patterns = [
            # React/Next.js
            "src/App.js", "src/App.jsx", "src/App.tsx",
            "src/index.js", "src/index.jsx", "src/index.tsx",
            "pages/index.js", "pages/index.jsx", "pages/index.tsx",
            "pages/_app.js", "pages/_app.tsx",
            "app/page.js", "app/page.tsx", "app/layout.js", "app/layout.tsx",
            # Vue/Nuxt
            "src/main.js", "src/main.ts", "src/App.vue",
            "nuxt.config.js", "nuxt.config.ts",
            # Svelte
            "src/routes/+page.svelte", "src/App.svelte",
            # Angular
            "src/app/app.component.ts", "angular.json",
            # Python
            "app/__init__.py", "src/__init__.py",
            "wsgi.py", "asgi.py", "settings.py",
            # Config files in subdirs
            "src/config.js", "config/default.js",
            # Build configs
            "next.config.mjs", "vite.config.ts", "vite.config.js",
        ]
        
        for pattern in source_patterns:
            file_path = repo_path / pattern
            if file_path.exists():
                content = self.read_file_safely(file_path, max_lines=50)
                if content and not content.startswith("[Error"):
                    samples[pattern] = content
        
        # Also try to find main entry point files dynamically
        for subdir in ["src", "app", "lib", "pages"]:
            subdir_path = repo_path / subdir
            if subdir_path.exists() and subdir_path.is_dir():
                try:
                    for ext in [".js", ".jsx", ".ts", ".tsx", ".vue", ".py"]:
                        for f in list(subdir_path.glob(f"*{ext}"))[:3]:  # Max 3 files per extension
                            rel_path = str(f.relative_to(repo_path))
                            if rel_path not in samples and len(samples) < 15:  # Max 15 samples
                                content = self.read_file_safely(f, max_lines=30)
                                if content and not content.startswith("[Error"):
                                    samples[rel_path] = content
                except Exception:
                    pass
        
        return samples
    
    def analyze_structure(self, repo_path: Path) -> Dict[str, Any]:
        """Preliminary file-based analysis of repository structure"""
        analysis = {
            "config_files": [],
            "package_managers": [],
            "build_tools": [],
            "static_site_generators": [],
            "frameworks_detected": [],
            "directories": [],
            "file_counts": {},
            "has_build_output": False
        }
        
        # Key files to look for
        config_files = [
            "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "requirements.txt", "setup.py", "pyproject.toml", "Pipfile", "poetry.lock",
            "Gemfile", "Gemfile.lock", "_config.yml", "config.toml", "hugo.toml",
            "tsconfig.json", "jsconfig.json",
            "webpack.config.js", "vite.config.js", "vite.config.ts", 
            "rollup.config.js", "next.config.js", "next.config.ts", "next.config.mjs",
            "nuxt.config.js", "nuxt.config.ts", "gatsby-config.js", "svelte.config.js",
            "astro.config.mjs", "docusaurus.config.js",
            ".eleventy.js", "eleventy.config.js",
            "netlify.toml", "vercel.json",
            "README.md", "README.txt",
            "index.html", "index.htm",
            "app.py", "main.py", "manage.py",
            "server.js", "app.js", "index.js",
            ".env.example", ".env.template",
            "Makefile", "angular.json", "vue.config.js"
        ]
        
        for file in config_files:
            if (repo_path / file).exists():
                analysis["config_files"].append(file)
        
        # Detect package managers
        if "package.json" in analysis["config_files"]:
            analysis["package_managers"].append("npm")
        if "yarn.lock" in analysis["config_files"]:
            analysis["package_managers"].append("yarn")
        if "pnpm-lock.yaml" in analysis["config_files"]:
            analysis["package_managers"].append("pnpm")
        if "Gemfile" in analysis["config_files"]:
            analysis["package_managers"].append("bundler")
        if "requirements.txt" in analysis["config_files"] or "pyproject.toml" in analysis["config_files"]:
            analysis["package_managers"].append("pip")
        if "poetry.lock" in analysis["config_files"]:
            analysis["package_managers"].append("poetry")
        if "Pipfile" in analysis["config_files"]:
            analysis["package_managers"].append("pipenv")
        
        # Detect build tools
        build_tools = ["webpack.config.js", "vite.config.js", "vite.config.ts", "rollup.config.js", "Makefile"]
        analysis["build_tools"] = [f for f in build_tools if f in analysis["config_files"]]
        
        # Detect static site generators from config files
        if "_config.yml" in analysis["config_files"]:
            analysis["static_site_generators"].append("Jekyll")
        if "config.toml" in analysis["config_files"] or "hugo.toml" in analysis["config_files"]:
            analysis["static_site_generators"].append("Hugo")
        if "gatsby-config.js" in analysis["config_files"]:
            analysis["static_site_generators"].append("Gatsby")
            analysis["frameworks_detected"].append("Gatsby")
        if ".eleventy.js" in analysis["config_files"] or "eleventy.config.js" in analysis["config_files"]:
            analysis["static_site_generators"].append("Eleventy")
        if "astro.config.mjs" in analysis["config_files"]:
            analysis["static_site_generators"].append("Astro")
            analysis["frameworks_detected"].append("Astro")
        if "docusaurus.config.js" in analysis["config_files"]:
            analysis["static_site_generators"].append("Docusaurus")
        
        # Detect frameworks from config files
        if any(f in analysis["config_files"] for f in ["next.config.js", "next.config.ts", "next.config.mjs"]):
            analysis["frameworks_detected"].append("Next.js")
        if any(f in analysis["config_files"] for f in ["nuxt.config.js", "nuxt.config.ts"]):
            analysis["frameworks_detected"].append("Nuxt.js")
        if "angular.json" in analysis["config_files"]:
            analysis["frameworks_detected"].append("Angular")
        if "svelte.config.js" in analysis["config_files"]:
            analysis["frameworks_detected"].append("SvelteKit")
        if "vue.config.js" in analysis["config_files"]:
            analysis["frameworks_detected"].append("Vue CLI")
        
        # Check important directories
        important_dirs = ["src", "source", "app", "lib", "public", "static", 
                         "assets", "components", "pages", "_posts", "_site", "dist", "build",
                         "layouts", "templates", "views", "routes"]
        for dir_name in important_dirs:
            if (repo_path / dir_name).exists():
                analysis["directories"].append(dir_name)
        
        # Check for build output (indicates this might be a built static site)
        build_output_dirs = ["dist", "build", "_site", "out", ".next", "public"]
        for d in build_output_dirs:
            dir_path = repo_path / d
            if dir_path.exists() and dir_path.is_dir():
                # Check if it looks like a build output (has index.html inside)
                if (dir_path / "index.html").exists():
                    analysis["has_build_output"] = True
                    break
        
        # Detect frameworks from package.json dependencies using helper
        if "package.json" in analysis["config_files"]:
            pkg_details = self.extract_package_json_details(repo_path)
            deps = {**pkg_details["dependencies"], **pkg_details["devDependencies"]}
            analysis["frameworks_detected"].extend(self.detect_frameworks_from_deps(deps))
        
        # Deduplicate frameworks
        analysis["frameworks_detected"] = list(set(analysis["frameworks_detected"]))
        
        # Count file types
        try:
            extensions = ['.html', '.htm', '.js', '.jsx', '.ts', '.tsx', 
                         '.py', '.css', '.scss', '.vue', '.svelte', '.md', '.json', '.rb']
            for ext in extensions:
                count = len(list(repo_path.glob(f"**/*{ext}")))
                if count > 0:
                    analysis["file_counts"][ext] = count
        except Exception as e:
            logger.debug(f"Error counting files: {e}")
        
        return analysis
    
    def read_file_safely(self, file_path: Path, max_lines: int = 300) -> Optional[str]:
        """Read file content safely with size limit"""
        try:
            if not file_path.exists():
                return None
            if file_path.stat().st_size > 500_000:
                return "[File too large]"
            
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                if len(lines) > max_lines:
                    return ''.join(lines[:max_lines]) + f"\n... [truncated, {len(lines)} total lines]"
                return ''.join(lines)
        except Exception as e:
            return f"[Error: {e}]"
    
    def extract_package_json_details(self, repo_path: Path) -> Dict[str, Any]:
        """Extract detailed info from package.json"""
        details = {"scripts": {}, "dependencies": {}, "devDependencies": {}, "engines": {}}
        pkg_path = repo_path / "package.json"
        if pkg_path.exists():
            try:
                with open(pkg_path, 'r') as f:
                    data = json.load(f)
                    details["scripts"] = data.get("scripts", {})
                    details["dependencies"] = data.get("dependencies", {})
                    details["devDependencies"] = data.get("devDependencies", {})
                    details["engines"] = data.get("engines", {})
            except Exception as e:
                logger.debug(f"Error parsing package.json: {e}")
        return details
    
    def detect_frameworks_from_deps(self, deps: Dict[str, Any]) -> List[str]:
        """Detect frameworks from package.json dependencies - reusable helper"""
        frameworks = []
        framework_map = {
            "next": "Next.js",
            "gatsby": "Gatsby",
            "nuxt": "Nuxt.js",
            "nuxt3": "Nuxt.js",
            "@angular/core": "Angular",
            "@sveltejs/kit": "SvelteKit",
            "svelte": "Svelte",
            "astro": "Astro",
            "@11ty/eleventy": "Eleventy",
            "hexo": "Hexo",
            "vue": "Vue.js",
            "react": "React",
            "express": "Express",
        }
        
        for dep, framework in framework_map.items():
            if dep in deps:
                # Don't add React if Next.js is present (Next includes React)
                if framework == "React" and "Next.js" in frameworks:
                    continue
                if framework not in frameworks:
                    frameworks.append(framework)
        
        return frameworks
    
    def extract_env_vars(self, repo_path: Path) -> List[str]:
        """Extract environment variables from .env.example"""
        env_vars = []
        for env_file in [".env.example", ".env.template"]:
            env_path = repo_path / env_file
            if env_path.exists():
                try:
                    with open(env_path, 'r') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#') and '=' in line:
                                var_name = line.split('=')[0].strip()
                                if var_name and var_name not in env_vars:
                                    env_vars.append(var_name)
                except Exception:
                    pass
        return env_vars
    
    def run_aider_analysis(self, repo_path: Path, repo_name: str, preliminary: Dict) -> Optional[Dict]:
        """Use Aider to create analysis.json"""
        logger.info(f"🤖 Running Aider analysis for {repo_name}...")
        
        # Read important files
        package_json = self.read_file_safely(repo_path / "package.json")
        requirements = self.read_file_safely(repo_path / "requirements.txt")
        readme = self.read_file_safely(repo_path / "README.md", max_lines=200)
        gemfile = self.read_file_safely(repo_path / "Gemfile")
        config_yml = self.read_file_safely(repo_path / "_config.yml")
        makefile = self.read_file_safely(repo_path / "Makefile", max_lines=100)
        
        # NEW: Read additional config files for deeper analysis
        next_config = self.read_file_safely(repo_path / "next.config.js") or self.read_file_safely(repo_path / "next.config.mjs")
        vite_config = self.read_file_safely(repo_path / "vite.config.js") or self.read_file_safely(repo_path / "vite.config.ts")
        tsconfig = self.read_file_safely(repo_path / "tsconfig.json")
        
        pkg_details = self.extract_package_json_details(repo_path)
        env_vars = self.extract_env_vars(repo_path)
        
        # NEW: Get directory tree for deeper understanding
        dir_tree = self.get_directory_tree(repo_path, max_depth=3, max_items=80)
        
        # NEW: Sample source files from subdirectories
        source_samples = self.sample_source_files(repo_path)
        source_samples_str = ""
        for file_path, content in source_samples.items():
            source_samples_str += f"\n### {file_path}\n```\n{content}\n```\n"
        
        # NEW: Get frameworks detected from preliminary analysis
        frameworks_detected = preliminary.get('frameworks_detected', [])
        has_build_output = preliminary.get('has_build_output', False)
        
        prompt = f"""# Deep Repository Analysis Task

You are analyzing a GitHub repository to determine its ACTUAL framework and technology stack.
**DO NOT default to "Static HTML" just because you see an index.html file.**

Many repositories contain:
- Built/compiled output (dist/, build/, _site/) which are NOT the source
- Pre-built GitHub Pages sites that are actually built from frameworks like Jekyll, Hugo, Hexo, Gatsby
- React/Vue/Angular apps that have an index.html entry point but are NOT static HTML

## Repository: {repo_name}

## ⚠️ IMPORTANT: Pre-detected Frameworks
Our preliminary analysis detected these frameworks: **{', '.join(frameworks_detected) if frameworks_detected else 'None detected yet - YOU must investigate deeper'}**
Has build output directory: **{has_build_output}**

## Directory Structure (EXPLORE THIS CAREFULLY!)
```
{dir_tree}
```

## Preliminary Analysis
**Config Files Found:** {', '.join(preliminary['config_files']) or 'None'}
**Package Managers:** {', '.join(preliminary['package_managers']) or 'None'}
**Build Tools:** {', '.join(preliminary['build_tools']) or 'None'}
**Static Site Generators:** {', '.join(preliminary['static_site_generators']) or 'None'}
**Directories:** {', '.join(preliminary['directories']) or 'None'}
**File Counts:** {json.dumps(preliminary['file_counts'])}

## package.json Scripts
```json
{json.dumps(pkg_details['scripts'], indent=2)}
```

## package.json Dependencies (CRITICAL - check for frameworks!)
```json
{json.dumps(pkg_details['dependencies'], indent=2)}
```

## package.json devDependencies
```json
{json.dumps(pkg_details['devDependencies'], indent=2)}
```

## Environment Variables (from .env.example)
{', '.join(env_vars) if env_vars else 'None found'}

## Root Config Files

### package.json
```json
{package_json or 'Not found'}
```

### requirements.txt
```
{requirements or 'Not found'}
```

### Gemfile
```ruby
{gemfile or 'Not found'}
```

### _config.yml (Jekyll config)
```yaml
{config_yml or 'Not found'}
```

### next.config.js / next.config.mjs
```javascript
{next_config or 'Not found'}
```

### vite.config.js / vite.config.ts
```javascript
{vite_config or 'Not found'}
```

### tsconfig.json
```json
{tsconfig or 'Not found'}
```

### Makefile
```makefile
{makefile or 'Not found'}
```

### README.md
```markdown
{readme or 'Not found'}
```

## Source Files from Subdirectories (CRITICAL - examine these!)
{source_samples_str if source_samples_str else '(No source files found in src/, app/, pages/, lib/)'}

---

## YOUR TASK: Create analysis.json

**BEFORE choosing "Static HTML" as the framework:**
1. Check if there's a package.json with dependencies (React, Vue, Next.js, etc.)
2. Check if there's a _config.yml (Jekyll) or Gemfile
3. Check if the index.html at root is a build output from a framework
4. Look at the directory structure - does it have src/, app/, pages/, components/?
5. Check if there's a build script that compiles to the index.html

**Framework Priority (check in this order):**
1. Next.js - has "next" dependency or next.config.js
2. React (CRA/Vite) - has "react" dependency with src/App.jsx or similar
3. Vue.js - has "vue" dependency
4. Angular - has "@angular/core" dependency
5. Gatsby - has "gatsby" dependency or gatsby-config.js
6. Jekyll - has _config.yml AND Gemfile with jekyll
7. Hugo - has config.toml or hugo.toml
8. Hexo - has "hexo" dependency or _config.yml with hexo theme structure
9. Eleventy - has ".eleventy.js" or @11ty/eleventy dependency
10. Astro - has "astro" dependency or astro.config.mjs
11. Express/Node.js - has "express" dependency with server.js/app.js
12. Django - has manage.py + Django in requirements
13. Flask - has app.py + Flask in requirements
14. **Static HTML** - ONLY if none of the above AND no package.json/Gemfile/requirements.txt

Create a file called `analysis.json` in the repository root with this **EXACT** structure:

```json
{{
  "primary_framework": {{
    "name": "Next.js | React | Vue.js | Angular | Gatsby | Jekyll | Hugo | Hexo | Eleventy | Astro | Express | Django | Flask | Static HTML | etc.",
    "version": "version from package.json/Gemfile or null",
    "confidence": "high | medium | low",
    "evidence": ["specific files/deps that prove this framework - BE SPECIFIC!"]
  }},
  "language": {{
    "primary": "TypeScript | JavaScript | Python | Ruby | HTML",
    "secondary": ["other languages used"]
  }},
  "package_manager": {{
    "primary": "npm | yarn | pnpm | pip | bundler | none",
    "install_command": "npm install | yarn | pip install -r requirements.txt | bundle install | null"
  }},
  "entry_point": {{
    "file": "src/App.tsx | pages/index.js | app.py | index.html | etc.",
    "working_directory": "."
  }},
  "development_server": {{
    "command": "npm run dev | yarn dev | bundle exec jekyll serve | python manage.py runserver | python -m http.server 8080",
    "port": 3000,
    "host": "127.0.0.1",
    "startup_time_seconds": 5,
    "source": "package.json scripts | Makefile | README | inferred"
  }},
  "health_check": {{
    "url": "http://127.0.0.1:PORT",
    "expected_status": 200,
    "timeout_seconds": 30
  }},
  "scripts": {{
    "install": "npm install | bundle install | pip install -r requirements.txt | null",
    "build": "npm run build | bundle exec jekyll build | null",
    "dev": "npm run dev | bundle exec jekyll serve | null",
    "start": "npm start | null",
    "test": "npm test | null"
  }},
  "database": {{
    "required": false,
    "type": "none | PostgreSQL | MySQL | MongoDB | SQLite",
    "migration_command": null
  }},
  "static_site_generator": {{
    "is_ssg": true,
    "generator": "Jekyll | Hugo | Gatsby | Hexo | Eleventy | null",
    "output_directory": "_site | dist | build | public | out"
  }},
  "dependencies": {{
    "runtime": ["list key runtime dependencies"],
    "dev": ["list key dev dependencies"],
    "system": ["node >= 18", "ruby >= 2.7", etc.]
  }},
  "environment_variables": {{
    "required": [],
    "optional": [],
    "defaults": {{}}
  }},
  "pre_build_steps": [],
  "setup_requirements": {{
    "node_version": "version or any",
    "python_version": "version or any", 
    "ruby_version": "version or any",
    "system_dependencies": []
  }},
  "deployment_notes": "any important notes",
  "automation_ready": {{
    "can_auto_install": true,
    "can_auto_build": true,
    "can_auto_serve": true,
    "complexity": "low | medium | high",
    "blockers": [],
    "warnings": []
  }},
  "confidence_score": 0.0-1.0
}}
```

## CRITICAL REQUIREMENTS

1. **INVESTIGATE DEEPLY**: Look at directory structure, source files, and dependencies
2. **DON'T DEFAULT TO STATIC HTML**: Only use "Static HTML" if there's NO framework evidence
3. **CHECK DEPENDENCIES**: package.json dependencies are the strongest signal
4. **BE SPECIFIC IN EVIDENCE**: List exact files/deps that prove your framework choice
5. **VALID JSON**: The file must be valid, parseable JSON
6. **USE ACTUAL VALUES**: Extract real commands from package.json scripts, README, etc.

**NOW CREATE THE analysis.json FILE!**
"""
        
        # Write prompt to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name
        
        try:
            command = [
                "aider",
                "--model", f"azure/{AZURE_DEPLOYMENT}",
                "--yes-always",  # Auto-confirm everything
                "--no-auto-commits",
                "--no-show-model-warnings",
                "--no-stream",
                "--no-suggest-shell-commands",
                "--no-detect-urls",  # Don't scrape URLs from content
                "--map-tokens", "0",  # Disable repo-map to speed up
                "--message-file", prompt_file
            ]
            
            # Run Aider in background and poll for the file
            analysis_file = repo_path / "analysis.json"
            process = subprocess.Popen(
                command,
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Poll for analysis.json creation (max 300 seconds)
            max_wait = 300
            poll_interval = 2
            elapsed = 0
            analysis_data = None
            
            while elapsed < max_wait:
                # Check if file exists and is valid JSON
                if analysis_file.exists():
                    try:
                        # Wait a moment for file to be fully written
                        time.sleep(1)
                        with open(analysis_file, 'r') as f:
                            content = f.read()
                            if content.strip():  # Ensure file is not empty
                                analysis_data = json.loads(content)
                                logger.info(f"✅ Aider created analysis.json successfully (after {elapsed}s)")
                                

                                
                                # Terminate Aider since we got what we need
                                process.terminate()
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                
                                return analysis_data
                    except json.JSONDecodeError:
                        # File exists but not valid JSON yet, keep waiting
                        pass
                    except Exception as e:
                        logger.debug(f"Error reading file: {e}")
                
                # Check if process has ended
                if process.poll() is not None:
                    # Process finished, check one more time for the file
                    if analysis_file.exists():
                        try:
                            with open(analysis_file, 'r') as f:
                                analysis_data = json.load(f)
                            logger.info(f"✅ Aider created analysis.json successfully")
                            return analysis_data
                        except json.JSONDecodeError as e:
                            logger.error(f"❌ Invalid JSON in analysis.json: {e}")
                            invalid_file = self.output_dir / f"{repo_name.replace('/', '_')}_INVALID.json"
                            shutil.copy(analysis_file, invalid_file)
                            return None
                    else:
                        logger.warning(f"⚠️ Aider did not create analysis.json")
                        return None
                
                time.sleep(poll_interval)
                elapsed += poll_interval
            
            # Timeout reached
            logger.error(f"❌ Aider timeout after {max_wait}s")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            
            # Final check for file
            if analysis_file.exists():
                try:
                    with open(analysis_file, 'r') as f:
                        analysis_data = json.load(f)
                    logger.info(f"✅ Found analysis.json after timeout")
                    return analysis_data
                except:
                    pass
            
            return None
                
        except Exception as e:
            logger.error(f"❌ Aider error: {e}")
            return None
        finally:
            try:
                os.unlink(prompt_file)
            except:
                pass
    
    def fallback_heuristic(self, repo_path: Path, preliminary: Dict) -> Dict[str, Any]:
        """Improved heuristic fallback when Aider fails - checks more frameworks"""
        env_vars = self.extract_env_vars(repo_path)
        analysis = {
            "primary_framework": {"name": "Unknown", "confidence": "low", "evidence": []},
            "language": {"primary": "Unknown", "secondary": []},
            "package_manager": {"primary": None, "install_command": None},
            "entry_point": {"file": None, "working_directory": "."},
            "development_server": {"command": None, "port": 8080, "host": "127.0.0.1", "startup_time_seconds": 10, "source": "inferred"},
            "health_check": {"url": "http://127.0.0.1:8080", "expected_status": 200, "timeout_seconds": 30},
            "scripts": {"install": None, "build": None, "dev": None, "start": None, "test": None},
            "database": {"required": False, "type": None, "migration_command": None},
            "static_site_generator": {"is_ssg": False, "generator": None, "output_directory": None},
            "dependencies": {"runtime": [], "dev": [], "system": []},
            "environment_variables": {"required": env_vars, "optional": [], "defaults": {}},
            "pre_build_steps": [],
            "setup_requirements": {"node_version": None, "python_version": None, "ruby_version": None, "system_dependencies": []},
            "automation_ready": {"can_auto_install": False, "can_auto_build": False, "can_auto_serve": False, "complexity": "unknown", "blockers": ["Analysis failed"], "warnings": []},
            "confidence_score": 0.2
        }
        
        framework_detected = False
        
        # Priority 1: Detect from package.json (most reliable for JS projects)
        pkg_path = repo_path / "package.json"
        if pkg_path.exists():
            try:
                with open(pkg_path) as f:
                    pkg = json.load(f)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                scripts = pkg.get("scripts", {})
                
                analysis["language"]["primary"] = "JavaScript"
                analysis["package_manager"] = {"primary": "npm", "install_command": "npm install"}
                analysis["dependencies"]["runtime"] = list(pkg.get("dependencies", {}).keys())[:10]
                analysis["setup_requirements"]["node_version"] = pkg.get("engines", {}).get("node", "any")
                
                # Check for TypeScript
                if "typescript" in deps or (repo_path / "tsconfig.json").exists():
                    analysis["language"]["primary"] = "TypeScript"
                
                port = 3000
                
                # Framework detection priority order (most specific first)
                if "next" in deps:
                    analysis["primary_framework"] = {"name": "Next.js", "version": deps.get("next"), "confidence": "high", "evidence": ["next in dependencies"]}
                    analysis["entry_point"] = {"file": "pages/index.js or app/page.js", "working_directory": "."}
                    analysis["static_site_generator"] = {"is_ssg": True, "generator": "Next.js", "output_directory": "out or .next"}
                    framework_detected = True
                elif "gatsby" in deps:
                    analysis["primary_framework"] = {"name": "Gatsby", "version": deps.get("gatsby"), "confidence": "high", "evidence": ["gatsby in dependencies"]}
                    analysis["entry_point"] = {"file": "src/pages/index.js", "working_directory": "."}
                    analysis["static_site_generator"] = {"is_ssg": True, "generator": "Gatsby", "output_directory": "public"}
                    framework_detected = True
                elif "nuxt" in deps or "nuxt3" in deps:
                    analysis["primary_framework"] = {"name": "Nuxt.js", "version": deps.get("nuxt") or deps.get("nuxt3"), "confidence": "high", "evidence": ["nuxt in dependencies"]}
                    analysis["entry_point"] = {"file": "pages/index.vue", "working_directory": "."}
                    framework_detected = True
                elif "@angular/core" in deps:
                    analysis["primary_framework"] = {"name": "Angular", "version": deps.get("@angular/core"), "confidence": "high", "evidence": ["@angular/core in dependencies"]}
                    analysis["entry_point"] = {"file": "src/app/app.component.ts", "working_directory": "."}
                    port = 4200
                    framework_detected = True
                elif "svelte" in deps or "@sveltejs/kit" in deps:
                    version = deps.get("@sveltejs/kit") or deps.get("svelte")
                    name = "SvelteKit" if "@sveltejs/kit" in deps else "Svelte"
                    analysis["primary_framework"] = {"name": name, "version": version, "confidence": "high", "evidence": [f"{name.lower()} in dependencies"]}
                    analysis["entry_point"] = {"file": "src/routes/+page.svelte" if name == "SvelteKit" else "src/App.svelte", "working_directory": "."}
                    port = 5173
                    framework_detected = True
                elif "astro" in deps:
                    analysis["primary_framework"] = {"name": "Astro", "version": deps.get("astro"), "confidence": "high", "evidence": ["astro in dependencies"]}
                    analysis["entry_point"] = {"file": "src/pages/index.astro", "working_directory": "."}
                    analysis["static_site_generator"] = {"is_ssg": True, "generator": "Astro", "output_directory": "dist"}
                    port = 4321
                    framework_detected = True
                elif "@11ty/eleventy" in deps:
                    analysis["primary_framework"] = {"name": "Eleventy", "version": deps.get("@11ty/eleventy"), "confidence": "high", "evidence": ["@11ty/eleventy in dependencies"]}
                    analysis["entry_point"] = {"file": "index.html or index.md", "working_directory": "."}
                    analysis["static_site_generator"] = {"is_ssg": True, "generator": "Eleventy", "output_directory": "_site"}
                    port = 8080
                    framework_detected = True
                elif "hexo" in deps:
                    analysis["primary_framework"] = {"name": "Hexo", "version": deps.get("hexo"), "confidence": "high", "evidence": ["hexo in dependencies"]}
                    analysis["entry_point"] = {"file": "source/_posts", "working_directory": "."}
                    analysis["static_site_generator"] = {"is_ssg": True, "generator": "Hexo", "output_directory": "public"}
                    port = 4000
                    framework_detected = True
                elif "vue" in deps:
                    analysis["primary_framework"] = {"name": "Vue.js", "version": deps.get("vue"), "confidence": "high", "evidence": ["vue in dependencies"]}
                    analysis["entry_point"] = {"file": "src/main.js", "working_directory": "."}
                    framework_detected = True
                elif "react" in deps:
                    analysis["primary_framework"] = {"name": "React", "version": deps.get("react"), "confidence": "high", "evidence": ["react in dependencies"]}
                    analysis["entry_point"] = {"file": "src/index.js or src/App.jsx", "working_directory": "."}
                    framework_detected = True
                elif "express" in deps:
                    analysis["primary_framework"] = {"name": "Express", "version": deps.get("express"), "confidence": "high", "evidence": ["express in dependencies"]}
                    analysis["entry_point"] = {"file": "index.js or app.js or server.js", "working_directory": "."}
                    framework_detected = True
                
                analysis["development_server"]["port"] = port
                analysis["health_check"] = {"url": f"http://127.0.0.1:{port}", "expected_status": 200, "timeout_seconds": 30}
                
                if "dev" in scripts:
                    analysis["scripts"]["dev"] = "npm run dev"
                    analysis["development_server"]["command"] = "npm run dev"
                elif "start" in scripts:
                    analysis["scripts"]["dev"] = "npm start"
                    analysis["development_server"]["command"] = "npm start"
                if "start" in scripts:
                    analysis["scripts"]["start"] = "npm start"
                if "build" in scripts:
                    analysis["scripts"]["build"] = "npm run build"
                    analysis["automation_ready"]["can_auto_build"] = True
                if "test" in scripts:
                    analysis["scripts"]["test"] = "npm test"
                analysis["scripts"]["install"] = "npm install"
                analysis["automation_ready"]["can_auto_install"] = True
                analysis["automation_ready"]["can_auto_serve"] = True
                analysis["automation_ready"]["complexity"] = "low"
                analysis["automation_ready"]["blockers"] = []
                
                analysis["confidence_score"] = 0.7 if framework_detected else 0.5
            except Exception:
                pass
        
        # Priority 2: Detect Jekyll (config file based)
        if not framework_detected and (repo_path / "_config.yml").exists():
            # Check if it's actually Jekyll (not Hexo which also uses _config.yml)
            gemfile_path = repo_path / "Gemfile"
            is_jekyll = False
            if gemfile_path.exists():
                try:
                    with open(gemfile_path, 'r') as f:
                        content = f.read().lower()
                        if 'jekyll' in content:
                            is_jekyll = True
                except Exception:
                    pass
            else:
                # No Gemfile but has _config.yml and _posts - likely Jekyll
                if (repo_path / "_posts").exists() or (repo_path / "_layouts").exists():
                    is_jekyll = True
            
            if is_jekyll:
                analysis["primary_framework"] = {"name": "Jekyll", "confidence": "high", "evidence": ["_config.yml exists", "Gemfile with jekyll or Jekyll structure"]}
                analysis["language"]["primary"] = "Ruby"
                analysis["package_manager"] = {"primary": "bundler", "install_command": "bundle install"}
                analysis["entry_point"] = {"file": "index.html or index.md", "working_directory": "."}
                analysis["development_server"] = {"command": "bundle exec jekyll serve", "port": 4000, "host": "127.0.0.1", "startup_time_seconds": 5, "source": "inferred"}
                analysis["health_check"] = {"url": "http://127.0.0.1:4000", "expected_status": 200, "timeout_seconds": 30}
                analysis["static_site_generator"] = {"is_ssg": True, "generator": "Jekyll", "output_directory": "_site"}
                analysis["scripts"] = {"install": "bundle install", "dev": "bundle exec jekyll serve", "build": "bundle exec jekyll build", "start": None, "test": None}
                analysis["setup_requirements"]["ruby_version"] = "2.7+"
                analysis["dependencies"]["system"].append("ruby >= 2.7")
                analysis["automation_ready"] = {"can_auto_install": True, "can_auto_build": True, "can_auto_serve": True, "complexity": "low", "blockers": [], "warnings": ["Requires Ruby and Bundler"]}
                analysis["confidence_score"] = 0.7
                framework_detected = True
        
        # Priority 3: Detect Hugo
        if not framework_detected and ((repo_path / "config.toml").exists() or (repo_path / "hugo.toml").exists()):
            analysis["primary_framework"] = {"name": "Hugo", "confidence": "high", "evidence": ["config.toml or hugo.toml exists"]}
            analysis["language"]["primary"] = "Go Template"
            analysis["language"]["secondary"] = ["HTML", "Markdown"]
            analysis["package_manager"] = {"primary": "hugo", "install_command": None}
            analysis["entry_point"] = {"file": "content/", "working_directory": "."}
            analysis["development_server"] = {"command": "hugo server", "port": 1313, "host": "127.0.0.1", "startup_time_seconds": 2, "source": "inferred"}
            analysis["health_check"] = {"url": "http://127.0.0.1:1313", "expected_status": 200, "timeout_seconds": 30}
            analysis["static_site_generator"] = {"is_ssg": True, "generator": "Hugo", "output_directory": "public"}
            analysis["scripts"] = {"install": None, "dev": "hugo server", "build": "hugo", "start": None, "test": None}
            analysis["dependencies"]["system"].append("hugo")
            analysis["automation_ready"] = {"can_auto_install": True, "can_auto_build": True, "can_auto_serve": True, "complexity": "low", "blockers": [], "warnings": ["Requires Hugo installed"]}
            analysis["confidence_score"] = 0.7
            framework_detected = True
        
        # Priority 4: Detect Python frameworks
        if not framework_detected and (repo_path / "requirements.txt").exists():
            analysis["language"]["primary"] = "Python"
            analysis["package_manager"] = {"primary": "pip", "install_command": "pip install -r requirements.txt"}
            analysis["dependencies"]["system"].append("python >= 3.8")
            analysis["setup_requirements"]["python_version"] = "3.8+"
            analysis["scripts"]["install"] = "pip install -r requirements.txt"
            
            if (repo_path / "manage.py").exists():
                analysis["primary_framework"] = {"name": "Django", "confidence": "high", "evidence": ["manage.py exists"]}
                analysis["entry_point"] = {"file": "manage.py", "working_directory": "."}
                analysis["development_server"] = {"command": "python manage.py runserver", "port": 8000, "host": "127.0.0.1", "startup_time_seconds": 5, "source": "inferred"}
                analysis["health_check"] = {"url": "http://127.0.0.1:8000", "expected_status": 200, "timeout_seconds": 30}
                analysis["scripts"]["dev"] = "python manage.py runserver"
                analysis["database"] = {"required": True, "type": "SQLite (default)", "migration_command": "python manage.py migrate"}
                framework_detected = True
            elif (repo_path / "app.py").exists():
                analysis["primary_framework"] = {"name": "Flask", "confidence": "medium", "evidence": ["app.py exists"]}
                analysis["entry_point"] = {"file": "app.py", "working_directory": "."}
                analysis["development_server"] = {"command": "flask run", "port": 5000, "host": "127.0.0.1", "startup_time_seconds": 3, "source": "inferred"}
                analysis["health_check"] = {"url": "http://127.0.0.1:5000", "expected_status": 200, "timeout_seconds": 30}
                analysis["scripts"]["dev"] = "flask run"
                framework_detected = True
            
            analysis["automation_ready"]["can_auto_install"] = True
            analysis["confidence_score"] = 0.6 if framework_detected else 0.4
        
        # Priority 5: Static HTML (ONLY if no other framework detected and no package.json/Gemfile/requirements.txt)
        if not framework_detected:
            has_dependency_files = (
                (repo_path / "package.json").exists() or 
                (repo_path / "Gemfile").exists() or 
                (repo_path / "requirements.txt").exists() or
                (repo_path / "pyproject.toml").exists()
            )
            
            if (repo_path / "index.html").exists() and not has_dependency_files:
                analysis["primary_framework"] = {"name": "Static HTML", "confidence": "medium", "evidence": ["index.html at root", "no package.json/Gemfile/requirements.txt"]}
                analysis["language"]["primary"] = "HTML"
                analysis["entry_point"] = {"file": "index.html", "working_directory": "."}
                analysis["development_server"] = {"command": "python -m http.server 8080", "port": 8080, "host": "127.0.0.1", "startup_time_seconds": 1, "source": "inferred"}
                analysis["health_check"] = {"url": "http://127.0.0.1:8080", "expected_status": 200, "timeout_seconds": 10}
                analysis["scripts"]["dev"] = "python -m http.server 8080"
                analysis["automation_ready"] = {"can_auto_install": True, "can_auto_build": True, "can_auto_serve": True, "complexity": "low", "blockers": [], "warnings": []}
                analysis["confidence_score"] = 0.6
            elif (repo_path / "index.html").exists() and has_dependency_files:
                # Has index.html but also has dependency files - this might be build output
                analysis["primary_framework"] = {"name": "Unknown (has build files)", "confidence": "low", "evidence": ["index.html exists but also has dependency files - may be built output"]}
                analysis["confidence_score"] = 0.3
        
        return analysis
    
    def analyze_repository(self, repo_name: str, repo_url: str) -> Dict[str, Any]:
        """Full analysis workflow for a single repository"""
        result = {
            "repo_name": repo_name,
            "repo_url": repo_url,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "status": "unknown",
            "analysis": None
        }
        
        # Clone
        clone_path = self.clone_repository(repo_name)
        if not clone_path:
            result["status"] = "clone_failed"
            result["error"] = "Failed to clone repository"
            return result
        
        try:
            # Preliminary analysis
            preliminary = self.analyze_structure(clone_path)
            
            # Try Aider first
            analysis = self.run_aider_analysis(clone_path, repo_name, preliminary)
            
            if analysis:
                result["analysis"] = analysis
                result["status"] = "success"
            else:
                # Fallback to heuristics
                logger.warning(f"⚠️ Falling back to heuristics for {repo_name}")
                analysis = self.fallback_heuristic(clone_path, preliminary)
                result["analysis"] = analysis
                result["status"] = "partial"
            
            # Save individual result
            output_file = self.output_dir / f"{repo_name.replace('/', '_')}_landscape.json"
            with open(output_file, 'w') as f:
                json.dump(result, f, indent=2)
            logger.info(f"💾 Saved to {output_file}")
            
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            logger.error(f"❌ Error: {e}")
        
        finally:
            # Cleanup cloned repo
            if clone_path and clone_path.exists():
                try:
                    shutil.rmtree(clone_path)
                    logger.debug(f"🧹 Cleaned up repo")
                except Exception as e:
                    logger.warning(f"⚠️ Cleanup error: {e}")
        
        return result
    
    def _process_single_repo(self, repo_name: str, repo_url: str, results_file: Path, index: int) -> Dict[str, Any]:
        """Process a single repository (used by both single and multi-threaded modes)"""
        logger.info(f"\n{'='*60}")
        logger.info(f"📊 [{index}] Analyzing: {repo_name}")
        logger.info(f"{'='*60}")
        
        result = self.analyze_repository(repo_name, repo_url)
        
        # Thread-safe updates
        with self._lock:
            self.results.append(result)
            self.processed_repos.add(repo_name)
            
            # Incremental save
            with open(results_file, "a") as rf:
                rf.write(json.dumps(result) + "\n")
        
        return result
    
    def process_dataset(self, dataset_path: str, limit: int = 0, checkpoint_interval: int = 5, 
                        enable_multi_thread: bool = False, num_threads: int = 4):
        """Process JSONL dataset with checkpointing and optional multi-threading"""
        results_file = self.output_dir / "all_results.jsonl"
        
        # Read all entries to process
        entries_to_process = []
        with open(dataset_path, "r") as f:
            count = 0
            for line in f:
                if not line.strip():
                    continue
                
                if limit and count >= limit:
                    break
                
                entry = json.loads(line)
                repo_name = entry.get("repo_name")
                repo_url = entry.get("repo_url")
                
                if not repo_name:
                    continue
                
                # Derive repo_url from repo_name if not provided
                if not repo_url:
                    repo_url = f"https://github.com/{repo_name}"
                
                # Skip if already processed
                if repo_name in self.processed_repos:
                    logger.info(f"⏭️ Skipping {repo_name} (already processed)")
                    count += 1
                    continue
                
                entries_to_process.append((repo_name, repo_url, count + 1))
                count += 1
        
        if not entries_to_process:
            logger.info("No new repositories to process.")
            self.generate_summary()
            return
        
        if enable_multi_thread:
            logger.info(f"� Multi-threaded mode enabled with {num_threads} threads")
            logger.info(f"📋 Processing {len(entries_to_process)} repositories...")
            
            processed_count = 0
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                # Submit all tasks
                future_to_repo = {
                    executor.submit(
                        self._process_single_repo, 
                        repo_name, 
                        repo_url, 
                        results_file, 
                        index
                    ): repo_name
                    for repo_name, repo_url, index in entries_to_process
                }
                
                # Process completed tasks
                for future in as_completed(future_to_repo):
                    repo_name = future_to_repo[future]
                    try:
                        result = future.result()
                        processed_count += 1
                        
                        # Checkpoint at intervals
                        if processed_count % checkpoint_interval == 0:
                            with self._lock:
                                self.save_checkpoint()
                            logger.info(f"💾 Checkpoint: {len(self.processed_repos)} processed")
                    except Exception as e:
                        logger.error(f"❌ Failed to process {repo_name}: {e}")
        else:
            # Single-threaded mode (original behavior)
            for repo_name, repo_url, index in entries_to_process:
                self._process_single_repo(repo_name, repo_url, results_file, index)
                
                # Checkpoint at intervals
                if index % checkpoint_interval == 0:
                    self.save_checkpoint()
                    logger.info(f"💾 Checkpoint: {len(self.processed_repos)} processed")
        
        # Final checkpoint and summary
        self.save_checkpoint()
        self.generate_summary()
    
    def generate_summary(self):
        """Generate summary report"""
        summary = {
            "total_processed": len(self.results),
            "successful": sum(1 for r in self.results if r.get("status") == "success"),
            "partial": sum(1 for r in self.results if r.get("status") == "partial"),
            "failed": sum(1 for r in self.results if r.get("status") in ["clone_failed", "error"]),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "framework_distribution": {},
            "language_distribution": {}
        }
        
        # Aggregate stats
        for result in self.results:
            if result.get("analysis"):
                analysis = result["analysis"]
                # Framework
                fw = analysis.get("primary_framework", {}).get("name", "Unknown")
                summary["framework_distribution"][fw] = summary["framework_distribution"].get(fw, 0) + 1
                # Language
                lang = analysis.get("language", {}).get("primary", "Unknown")
                summary["language_distribution"][lang] = summary["language_distribution"].get(lang, 0) + 1
        
        # Save summary
        with open(self.output_dir / "summary.json", 'w') as f:
            json.dump(summary, f, indent=2)
        
        # Print summary
        logger.info(f"\n{'='*60}")
        logger.info("ANALYSIS SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Total: {summary['total_processed']}")
        logger.info(f"Success: {summary['successful']} | Partial: {summary['partial']} | Failed: {summary['failed']}")
        if summary['framework_distribution']:
            logger.info("\nTop Frameworks:")
            for fw, count in sorted(summary['framework_distribution'].items(), key=lambda x: x[1], reverse=True)[:5]:
                logger.info(f"  {fw}: {count}")
        logger.info(f"{'='*60}")
    
    def cleanup(self):
        """Clean up temp directory"""
        try:
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)
                logger.info(f"🧹 Cleaned up temp directory")
        except Exception as e:
            logger.warning(f"⚠️ Could not cleanup temp: {e}")


def main():
    parser = argparse.ArgumentParser(description="Analyze repositories for deployment")
    parser.add_argument("--github-url", help="Single GitHub URL to analyze")
    parser.add_argument("--dataset", help="JSONL dataset path")
    parser.add_argument("--output-dir", default="repo_analysis", help="Output directory")
    parser.add_argument("--limit", type=int, default=0, help="Limit repos to process")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--enable_multi_thread", action="store_true", help="Enable multi-threaded processing")
    parser.add_argument("--no_thread", type=int, default=4, help="Number of threads (only applicable when --enable_multi_thread is enabled)")
    
    args = parser.parse_args()
    
    output_path = Path(args.output_dir).resolve()
    analyzer = RepoAnalyzer(output_path)
    
    if args.resume and analyzer.processed_repos:
        logger.info(f"♻️ Resuming: {len(analyzer.processed_repos)} already processed")
    
    try:
        if args.github_url:
            # Single repo mode
            url = args.github_url.rstrip('/')
            parts = url.replace('.git', '').split('/')
            repo_name = f"{parts[-2]}/{parts[-1]}"
            result = analyzer.analyze_repository(repo_name, args.github_url)
            logger.info(f"Result: {result['status']}")
        elif args.dataset:
            # Batch mode
            analyzer.process_dataset(
                args.dataset, 
                args.limit, 
                enable_multi_thread=args.enable_multi_thread,
                num_threads=args.no_thread
            )
        else:
            parser.error("Specify --github-url or --dataset")
    
    except KeyboardInterrupt:
        logger.warning("\n⚠️ Interrupted")
        analyzer.save_checkpoint()
        logger.info("💾 Checkpoint saved. Use --resume to continue.")
    finally:
        analyzer.cleanup()


if __name__ == "__main__":
    main()
