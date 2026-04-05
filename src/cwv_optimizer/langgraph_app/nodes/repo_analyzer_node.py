"""Node for analyzing repository structure and generating analysis JSON.

This node uses Aider AI to analyze the repository and create a comprehensive
analysis.json file with framework, dependency, and deployment metadata.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)

# Azure deployment for Aider
AZURE_DEPLOYMENT = os.getenv("AZURE_DEPLOYMENT", "gpt-5")
AIDER_TIMEOUT = 300  # 5 minutes


def get_run_logger(log_file: str | None, repo_name: str) -> logging.Logger:
    """Get or create a run logger for file logging."""
    if not log_file:
        return logger

    run_logger = logging.getLogger(f"cwv_run.{repo_name}")
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


def analyze_structure(repo_path: Path) -> Dict[str, Any]:
    """Preliminary file-based analysis of repository structure."""
    analysis = {
        "config_files": [],
        "package_managers": [],
        "build_tools": [],
        "static_site_generators": [],
        "directories": [],
        "file_counts": {}
    }

    # Key files to look for
    config_files = [
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "requirements.txt", "setup.py", "pyproject.toml", "Pipfile", "poetry.lock",
        "Gemfile", "Gemfile.lock", "_config.yml", "config.toml", "hugo.toml",
        "tsconfig.json", "jsconfig.json",
        "webpack.config.js", "vite.config.js", "vite.config.ts",
        "rollup.config.js", "next.config.js", "next.config.ts",
        "nuxt.config.js", "gatsby-config.js", "svelte.config.js",
        "astro.config.mjs", "docusaurus.config.js",
        ".eleventy.js", "eleventy.config.js",
        "netlify.toml", "vercel.json",
        "README.md", "README.txt",
        "index.html", "index.htm",
        "app.py", "main.py", "manage.py",
        "server.js", "app.js", "index.js",
        ".env.example", ".env.template",
        "Makefile"
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

    # Detect static site generators
    if "_config.yml" in analysis["config_files"]:
        analysis["static_site_generators"].append("Jekyll")
    if "config.toml" in analysis["config_files"] or "hugo.toml" in analysis["config_files"]:
        analysis["static_site_generators"].append("Hugo")
    if "gatsby-config.js" in analysis["config_files"]:
        analysis["static_site_generators"].append("Gatsby")
    if ".eleventy.js" in analysis["config_files"] or "eleventy.config.js" in analysis["config_files"]:
        analysis["static_site_generators"].append("Eleventy")
    if "astro.config.mjs" in analysis["config_files"]:
        analysis["static_site_generators"].append("Astro")
    if "docusaurus.config.js" in analysis["config_files"]:
        analysis["static_site_generators"].append("Docusaurus")

    # Check important directories
    important_dirs = ["src", "source", "app", "lib", "public", "static",
                     "assets", "components", "pages", "_posts", "_site", "dist", "build"]
    for dir_name in important_dirs:
        if (repo_path / dir_name).exists():
            analysis["directories"].append(dir_name)

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


def read_file_safely(file_path: Path, max_lines: int = 300) -> Optional[str]:
    """Read file content safely with size limit."""
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


def extract_package_json_details(repo_path: Path) -> Dict[str, Any]:
    """Extract detailed info from package.json."""
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


def extract_env_vars(repo_path: Path) -> List[str]:
    """Extract environment variables from .env.example."""
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


def run_aider_analysis(
    repo_path: Path,
    repo_name: str,
    preliminary: Dict,
    run_logger: logging.Logger
) -> Optional[Dict]:
    """Use Aider to create analysis.json."""
    run_logger.info(f"🤖 Running Aider analysis for {repo_name}...")

    # Read important files
    package_json = read_file_safely(repo_path / "package.json")
    requirements = read_file_safely(repo_path / "requirements.txt")
    readme = read_file_safely(repo_path / "README.md", max_lines=200)
    gemfile = read_file_safely(repo_path / "Gemfile")
    config_yml = read_file_safely(repo_path / "_config.yml")
    makefile = read_file_safely(repo_path / "Makefile", max_lines=100)
    env_example = read_file_safely(repo_path / ".env.example", max_lines=50)

    pkg_details = extract_package_json_details(repo_path)
    env_vars = extract_env_vars(repo_path)

    prompt = f"""# Repository Analysis Task

Analyze this repository and create **analysis.json** in the repository root.

## Repository: {repo_name}

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

## package.json Engines
```json
{json.dumps(pkg_details['engines'], indent=2)}
```

## Environment Variables (from .env.example)
{', '.join(env_vars) if env_vars else 'None found'}

## File Contents

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

### _config.yml
```yaml
{config_yml or 'Not found'}
```

### Makefile
```makefile
{makefile or 'Not found'}
```

### .env.example
```
{env_example or 'Not found'}
```

### README.md
```markdown
{readme or 'Not found'}
```

---

## YOUR TASK: Create analysis.json

Create a file called `analysis.json` in the repository root with this **EXACT** structure:

```json
{{
  "primary_framework": {{
    "name": "React | Vue | Next.js | Django | Flask | Jekyll | Hugo | Express | Static HTML | etc.",
    "version": "version or null",
    "confidence": "high | medium | low",
    "evidence": ["files/deps that indicate this framework"]
  }},
  "language": {{
    "primary": "JavaScript | Python | Ruby | TypeScript | HTML | etc.",
    "secondary": ["other languages"]
  }},
  "package_manager": {{
    "primary": "npm | yarn | pip | bundler | etc.",
    "install_command": "npm install | pip install -r requirements.txt | etc."
  }},
  "entry_point": {{
    "file": "app.py | index.js | manage.py | etc.",
    "working_directory": ". | src | app | etc."
  }},
  "development_server": {{
    "command": "npm run dev | python manage.py runserver | bundle exec jekyll serve | etc.",
    "port": 3000,
    "host": "127.0.0.1 | localhost | 0.0.0.0",
    "startup_time_seconds": 5,
    "source": "readme | package.json | inferred"
  }},
  "health_check": {{
    "url": "http://localhost:3000 | http://127.0.0.1:8000 | etc.",
    "expected_status": 200,
    "timeout_seconds": 30
  }},
  "scripts": {{
    "install": "command to install dependencies",
    "build": "command to build (or null)",
    "dev": "command to start dev server",
    "start": "command to start production server (or null)",
    "test": "command to run tests (or null)"
  }},
  "database": {{
    "required": true/false,
    "type": "PostgreSQL | MySQL | MongoDB | SQLite | Redis | none",
    "setup_command": "command to setup db (or null)",
    "migration_command": "python manage.py migrate | npx prisma migrate | etc.",
    "seed_command": "command to seed data (or null)"
  }},
  "static_site_generator": {{
    "is_ssg": true/false,
    "generator": "Jekyll | Hugo | Gatsby | etc. or null",
    "output_directory": "_site | dist | build | etc."
  }},
  "dependencies": {{
    "runtime": ["key runtime deps"],
    "dev": ["key dev deps"],
    "system": ["node >= 18", "python >= 3.10", "ruby >= 2.7", etc.]
  }},
  "environment_variables": {{
    "required": ["list of required env vars"],
    "optional": ["list of optional env vars"],
    "defaults": {{"VAR_NAME": "default_value"}}
  }},
  "pre_build_steps": [
    "step 1: description",
    "step 2: description"
  ],
  "setup_requirements": {{
    "node_version": "version or any",
    "python_version": "version or any",
    "ruby_version": "version or any",
    "system_dependencies": ["any system-level deps like imagemagick, ffmpeg, etc."]
  }},
  "deployment_notes": "any important notes for deployment",
  "automation_ready": {{
    "can_auto_install": true/false,
    "can_auto_build": true/false,
    "can_auto_serve": true/false,
    "complexity": "low | medium | high",
    "blockers": ["list of blocking issues"],
    "warnings": ["things to watch out for"]
  }},
  "confidence_score": 0.0-1.0
}}
```

## CRITICAL REQUIREMENTS

1. **CREATE THE FILE**: You MUST create `analysis.json` in the repository root
2. **VALID JSON**: The file must be valid, parseable JSON
3. **NO PLACEHOLDERS**: Use actual values, "unknown" if truly unknown
4. **BE SPECIFIC**: List actual commands like "npm run dev", not "start server"
5. **EXTRACT FROM FILES**: Use the package.json, requirements.txt, README etc. provided above

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
            "--yes-always",
            "--no-auto-commits",
            "--no-show-model-warnings",
            "--no-stream",
            "--no-suggest-shell-commands",
            "--no-detect-urls",
            "--map-tokens", "0",
            "--message-file", prompt_file
        ]

        run_logger.info(f"Running Aider command: {' '.join(command[:3])} ...")
        with open(repo_path / "prompts.log", "w") as f:
            f.write("=== REPO ANALYZER PROMPT ===\n")
            f.write(prompt)
            f.write("\n===========================\n\n")

        # Run Aider in background and poll for the file
        analysis_file = repo_path / "analysis.json"
        process = subprocess.Popen(
            command,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Poll for analysis.json creation (max 300 seconds)
        import time
        max_wait = AIDER_TIMEOUT
        poll_interval = 2
        elapsed = 0
        analysis_data = None

        while elapsed < max_wait:
            # Check if file exists and is valid JSON
            if analysis_file.exists():
                try:
                    time.sleep(1)
                    with open(analysis_file, 'r') as f:
                        content = f.read()
                        if content.strip():
                            analysis_data = json.loads(content)
                            run_logger.info(f"✅ Aider created analysis.json successfully (after {elapsed}s)")

                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()

                            return analysis_data
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    run_logger.debug(f"Error reading file: {e}")

            if process.poll() is not None:
                if analysis_file.exists():
                    try:
                        with open(analysis_file, 'r') as f:
                            analysis_data = json.load(f)
                        run_logger.info("✅ Aider created analysis.json successfully")
                        return analysis_data
                    except json.JSONDecodeError as e:
                        run_logger.error(f"❌ Invalid JSON in analysis.json: {e}")
                        return None
                else:
                    run_logger.warning("⚠️ Aider did not create analysis.json")
                    return None

            time.sleep(poll_interval)
            elapsed += poll_interval

        run_logger.error(f"❌ Aider timeout after {max_wait}s")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

        if analysis_file.exists():
            try:
                with open(analysis_file, 'r') as f:
                    analysis_data = json.load(f)
                run_logger.info("✅ Found analysis.json after timeout")
                return analysis_data
            except Exception:
                pass

        return None

    except Exception as e:
        run_logger.error(f"❌ Aider error: {e}")
        return None
    finally:
        try:
            os.unlink(prompt_file)
        except Exception:
            pass


def fallback_heuristic(repo_path: Path, preliminary: Dict) -> Dict[str, Any]:
    """Basic heuristic fallback when Aider fails."""
    env_vars = extract_env_vars(repo_path)
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

    # Detect from package.json
    pkg_path = repo_path / "package.json"
    if pkg_path.exists():
        try:
            with open(pkg_path) as f:
                pkg = json.load(f)
            deps = pkg.get("dependencies", {})
            scripts = pkg.get("scripts", {})

            analysis["language"]["primary"] = "JavaScript"
            analysis["package_manager"] = {"primary": "npm", "install_command": "npm install"}
            analysis["dependencies"]["runtime"] = list(deps.keys())[:10]
            analysis["setup_requirements"]["node_version"] = pkg.get("engines", {}).get("node", "any")

            port = 3000
            if "next" in deps:
                analysis["primary_framework"] = {"name": "Next.js", "version": deps.get("next"), "confidence": "high", "evidence": ["next in dependencies"]}
                analysis["entry_point"] = {"file": "pages/index.js or app/page.js", "working_directory": "."}
            elif "react" in deps:
                analysis["primary_framework"] = {"name": "React", "version": deps.get("react"), "confidence": "high", "evidence": ["react in dependencies"]}
                analysis["entry_point"] = {"file": "src/index.js", "working_directory": "."}
            elif "vue" in deps:
                analysis["primary_framework"] = {"name": "Vue.js", "version": deps.get("vue"), "confidence": "high", "evidence": ["vue in dependencies"]}
                analysis["entry_point"] = {"file": "src/main.js", "working_directory": "."}
            elif "express" in deps:
                analysis["primary_framework"] = {"name": "Express", "version": deps.get("express"), "confidence": "high", "evidence": ["express in dependencies"]}
                analysis["entry_point"] = {"file": "index.js or app.js", "working_directory": "."}

            analysis["development_server"]["port"] = port
            analysis["health_check"] = {"url": f"http://127.0.0.1:{port}", "expected_status": 200, "timeout_seconds": 30}

            if "dev" in scripts:
                analysis["scripts"]["dev"] = "npm run dev"
                analysis["development_server"]["command"] = "npm run dev"
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

            analysis["confidence_score"] = 0.6
        except Exception:
            pass

    # Detect Python
    if (repo_path / "requirements.txt").exists():
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
        elif (repo_path / "app.py").exists():
            analysis["primary_framework"] = {"name": "Flask", "confidence": "medium", "evidence": ["app.py exists"]}
            analysis["entry_point"] = {"file": "app.py", "working_directory": "."}
            analysis["development_server"] = {"command": "flask run", "port": 5000, "host": "127.0.0.1", "startup_time_seconds": 3, "source": "inferred"}
            analysis["health_check"] = {"url": "http://127.0.0.1:5000", "expected_status": 200, "timeout_seconds": 30}
            analysis["scripts"]["dev"] = "flask run"

        analysis["automation_ready"]["can_auto_install"] = True
        analysis["confidence_score"] = 0.5

    # Detect Jekyll
    if (repo_path / "_config.yml").exists():
        analysis["primary_framework"] = {"name": "Jekyll", "confidence": "high", "evidence": ["_config.yml exists"]}
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

    # Detect static HTML
    if (repo_path / "index.html").exists() and analysis["primary_framework"]["name"] == "Unknown":
        analysis["primary_framework"] = {"name": "Static HTML", "confidence": "high", "evidence": ["index.html at root"]}
        analysis["language"]["primary"] = "HTML"
        analysis["entry_point"] = {"file": "index.html", "working_directory": "."}
        analysis["development_server"] = {"command": "python -m http.server 8080", "port": 8080, "host": "127.0.0.1", "startup_time_seconds": 1, "source": "inferred"}
        analysis["health_check"] = {"url": "http://127.0.0.1:8080", "expected_status": 200, "timeout_seconds": 10}
        analysis["scripts"]["dev"] = "python -m http.server 8080"
        analysis["automation_ready"] = {"can_auto_install": True, "can_auto_build": True, "can_auto_serve": True, "complexity": "low", "blockers": [], "warnings": []}
        analysis["confidence_score"] = 0.7

    return analysis


async def repo_analyzer_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that analyzes repository structure and generates analysis JSON.

    Uses Aider AI to create comprehensive analysis.json with framework,
    dependency, and deployment metadata.

    Input from state:
        - workspace_dir: Path to cloned repository
        - repo_name: Repository name
        - log_file: Path to run log file
        - reports_dir: Path to reports directory

    Output to state:
        - analysis_json_path: Path to generated analysis.json
    """

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        workspace_dir = current_state.get("workspace_dir")
        if not workspace_dir:
            raise RuntimeError("workspace_dir is required (run clone_repo first)")

        repo_name = current_state.get("repo_name", "unknown")
        log_file = current_state.get("log_file")
        reports_dir = current_state.get("reports_dir")

        run_logger = get_run_logger(log_file, repo_name)

        run_logger.info("=" * 60)
        run_logger.info("REPO ANALYZER NODE")
        run_logger.info("=" * 60)
        run_logger.info(f"Workspace: {workspace_dir}")
        run_logger.info(f"Reports dir: {reports_dir}")

        repo_path = Path(workspace_dir)

        # Step 1: Preliminary file-based analysis
        logger.info("Running preliminary structure analysis...")
        run_logger.info("Step 1: Preliminary structure analysis...")
        preliminary = analyze_structure(repo_path)
        run_logger.info(f"Found config files: {preliminary['config_files']}")
        run_logger.info(f"Detected package managers: {preliminary['package_managers']}")

        # Step 2: AI-powered analysis with Aider
        logger.info("Running Aider AI analysis...")
        run_logger.info("Step 2: Running Aider AI analysis...")
        analysis = run_aider_analysis(repo_path, repo_name, preliminary, run_logger)

        if not analysis:
            # Fallback to heuristics
            logger.warning("Aider analysis failed, falling back to heuristics")
            run_logger.warning("⚠️ Aider analysis failed, using heuristic fallback...")
            analysis = fallback_heuristic(repo_path, preliminary)

        # Step 3: Save analysis to reports directory
        analysis_json_path = repo_path / "analysis.json"
        with open(analysis_json_path, 'w') as f:
            json.dump(analysis, f, indent=2)
        run_logger.info(f"💾 Saved analysis.json to {analysis_json_path}")

        # Also copy to reports directory
        if reports_dir:
            reports_path = Path(reports_dir) / "analysis.json"
            shutil.copy(analysis_json_path, reports_path)
            run_logger.info(f"💾 Copied analysis.json to {reports_path}")

        # Update state
        current_state["analysis_json_path"] = str(analysis_json_path)

        run_logger.info("=" * 60)
        run_logger.info(f"ANALYSIS COMPLETE")
        run_logger.info(f"Framework: {analysis.get('primary_framework', {}).get('name', 'Unknown')}")
        run_logger.info(f"Confidence: {analysis.get('confidence_score', 0)}")
        run_logger.info("=" * 60)

        logger.info("Repository analysis complete: %s", analysis.get('primary_framework', {}).get('name', 'Unknown'))
        return current_state

    return await run_with_timing("repo_analyzer", state, _impl)
