#!/usr/bin/env python3
"""
Deployment Script Generator

Takes a repository landscape JSON, clones the repo, uses Aider AI to generate
a deployment.sh file, tests it, and iteratively retries on failure until success.

Usage:
    # Single file mode
    python scripts/deploy_generator.py path/to/landscape.json [--max-retries 5] [--output-dir ./output]
    
    # Batch mode
    python scripts/deploy_generator.py --batch path/to/json_dir/ [--limit 10] [--threads 4]
"""

import argparse
import json
import logging
import os
import signal
import shutil
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
AZURE_DEPLOYMENT = os.getenv("AZURE_DEPLOYMENT", "gpt-4o")
AIDER_TIMEOUT = 300  # 5 minutes for Aider to generate
DEPLOY_TEST_TIMEOUT = 120  # 2 minutes for deployment test
STARTUP_WAIT = 15  # seconds to wait for server startup
DEFAULT_PORT = 8080  # Default port if not specified

# Server startup indicators
SERVER_STARTUP_PATTERNS = [
    "server running",
    "listening on",
    "started server",
    "development server",
    "ready on",
    "localhost:",
    "127.0.0.1:",
    "http://",
    "serving at",
    "available on",
]


class DeploymentGenerator:
    """Generates deployment.sh files using Aider AI with iterative testing."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.repos_dir = self.output_dir / "repos"
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()  # Thread lock for batch processing
        logger.info(f"📁 Repos directory: {self.repos_dir}")

    def load_landscape_json(self, json_path: Path) -> Dict:
        """Load and parse the landscape JSON file."""
        logger.info(f"📖 Loading landscape JSON: {json_path}")
        with open(json_path, 'r') as f:
            data = json.load(f)
        logger.info(f"✅ Loaded analysis for: {data.get('repo_name', 'unknown')}")
        return data

    def clone_repository(self, repo_name: str) -> Optional[Path]:
        """Clone a repository to repos directory."""
        clone_path = self.repos_dir / repo_name.replace("/", "_")

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
            logger.error("❌ Clone timeout")
            return None
        except Exception as e:
            logger.error(f"❌ Clone error: {e}")
            return None

    def get_port_from_analysis(self, analysis: Dict) -> int:
        """Extract port from analysis, with fallback to default."""
        try:
            port = analysis.get('development_server', {}).get('port')
            if port and isinstance(port, int):
                return port
        except Exception:
            pass
        return DEFAULT_PORT

    def check_server_health(self, port: int, timeout: int = 10) -> bool:
        """Verify server is responding to HTTP requests."""
        url = f"http://127.0.0.1:{port}"
        logger.info(f"🔍 Checking server health at {url}...")
        
        for attempt in range(timeout):
            try:
                response = urllib.request.urlopen(url, timeout=2)
                status = response.getcode()
                if status in (200, 301, 302, 304):
                    logger.info(f"✅ Server responding with status {status}")
                    return True
            except urllib.error.HTTPError as e:
                # Server is running but returned an error - still counts as "up"
                if e.code in (400, 401, 403, 404, 500):
                    logger.info(f"✅ Server responding (HTTP {e.code})")
                    return True
            except Exception:
                pass
            time.sleep(1)
        
        logger.warning(f"⚠️ Server not responding after {timeout}s")
        return False

    def kill_process_tree(self, process: subprocess.Popen):
        """Kill process and all its children robustly."""
        try:
            # Try to kill the process group
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        
        try:
            process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        except Exception:
            pass

    def build_aider_prompt(self, json_data: Dict, previous_error: str = None) -> str:
        """Build the Aider prompt for generating deployment.sh."""
        analysis = json_data.get("analysis", {})
        repo_name = json_data.get("repo_name", "unknown")
        
        # Extract key info with defaults
        framework = analysis.get('primary_framework', {}).get('name', 'Unknown')
        pkg_manager = analysis.get('package_manager', {}).get('primary', 'Unknown')
        install_cmd = analysis.get('scripts', {}).get('install', 'Unknown')
        dev_cmd = analysis.get('scripts', {}).get('dev', 'Unknown')
        port = self.get_port_from_analysis(analysis)
        system_deps = analysis.get('dependencies', {}).get('system', [])

        prompt = f"""# Generate Deployment Script

## Repository: {repo_name}

## Landscape Analysis (from pre-analysis)

```json
{json.dumps(analysis, indent=2)}
```

---

## YOUR TASK: Create deployment.sh

Read the codebase files carefully (package.json, README.md, requirements.txt, Gemfile, _config.yml, docker-compose.yml, etc.) to understand the project structure.

Then create a `deployment.sh` file in the repository root that:

1. **Installs all dependencies** (system, runtime, dev)
2. **Builds the project** (if needed)
3. **Starts the development server** locally

### Requirements:
- Must be executable bash script (start with `#!/bin/bash`)
- Use `set -e` to exit on errors
- Print clear status messages for each step
- Handle missing dependencies gracefully
- The server should run in foreground (not daemonized)

### Key Information from Analysis:
- **Framework**: {framework}
- **Package Manager**: {pkg_manager}
- **Install Command**: {install_cmd}
- **Dev Command**: {dev_cmd}
- **Port**: {port}
- **System Dependencies**: {system_deps}

### Template Structure:
```bash
#!/bin/bash
set -e

log() {{ echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" >&2; }}

log "🚀 Starting deployment for {repo_name}..."

# Step 1: Check/Install system dependencies
log "📦 Checking system dependencies..."
# [Add commands here]

# Step 2: Install project dependencies
log "📥 Installing project dependencies..."
# [Add commands here]

# Step 3: Build (if needed)
log "🔨 Building project..."
# [Add commands here]

# Step 4: Start development server
log "🌐 Starting development server on port {port}..."
# [Add server start command here]
```

IMPORTANT: Read the project files to understand exact commands needed. Do not guess - use what's in the actual codebase.

Create the `deployment.sh` file now.
"""

        if previous_error:
            prompt += f"""

---

## ⚠️ PREVIOUS ATTEMPT FAILED

The previous deployment.sh failed with this error:

```
{previous_error}
```

Please analyze the error and fix the deployment.sh script. Common issues:
- Missing system dependencies (brew install, apt-get, etc.)
- Wrong Node/Ruby/Python version
- Missing environment setup
- Incorrect working directory
- Wrong command syntax

Read the error carefully and update the script to fix it.
"""

        return prompt

    def run_aider(self, repo_path: Path, prompt: str) -> Tuple[bool, Optional[str]]:
        """Run Aider to generate or fix deployment.sh. Returns (success, script_content)."""
        logger.info("🤖 Running Aider to generate deployment.sh...")

        # Write prompt to a temp file
        prompt_file = repo_path / ".aider_prompt.md"
        with open(prompt_file, 'w') as f:
            f.write(prompt)

        try:
            cmd = [
                "aider",
                "deployment.sh",
                "--model", f"azure/{AZURE_DEPLOYMENT}",
                "--message-file", str(prompt_file.resolve()),
                "--no-show-model-warnings",
                "--no-auto-commits",
                "--no-gitignore",
                "--yes"
            ]

            logger.info(f"Running: aider deployment.sh --model azure/{AZURE_DEPLOYMENT} ...")

            result = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=AIDER_TIMEOUT,
            )

            logger.debug(f"Aider return code: {result.returncode}")

            # Check if deployment.sh was created with actual content
            deploy_script = repo_path / "deployment.sh"
            if deploy_script.exists():
                script_content = deploy_script.read_text().strip()
                if len(script_content) < 50:
                    logger.warning(f"⚠️ deployment.sh too short ({len(script_content)} chars)")
                    deploy_script.unlink()
                    return False, None
                
                logger.info(f"✅ Created deployment.sh ({len(script_content)} chars)")
                os.chmod(deploy_script, 0o755)
                return True, script_content
            else:
                logger.warning("⚠️ Aider did not create deployment.sh")
                return False, None

        except subprocess.TimeoutExpired:
            logger.error("❌ Aider timeout")
            return False, None
        except Exception as e:
            logger.error(f"❌ Aider error: {e}")
            return False, None
        finally:
            if prompt_file.exists():
                prompt_file.unlink()

    def test_deployment(self, repo_path: Path, port: int) -> Tuple[bool, str]:
        """Test the deployment.sh script with HTTP health checking."""
        deploy_script = repo_path / "deployment.sh"

        if not deploy_script.exists():
            return False, "deployment.sh not found"

        logger.info(f"🧪 Testing deployment.sh (expecting server on port {port})...")

        process = None
        try:
            # Start with new process group for clean cleanup
            process = subprocess.Popen(
                ["bash", "./deployment.sh"],
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                preexec_fn=os.setsid  # Create new process group
            )

            output_lines = []
            start_time = time.time()
            server_detected = False

            while time.time() - start_time < DEPLOY_TEST_TIMEOUT:
                # Check if process ended
                retcode = process.poll()
                if retcode is not None:
                    remaining = process.stdout.read()
                    if remaining:
                        output_lines.append(remaining)
                    output = ''.join(output_lines)

                    if retcode == 0:
                        logger.info("✅ Deployment script completed successfully")
                        return True, output
                    else:
                        logger.error(f"❌ Script failed with code {retcode}")
                        return False, output

                # Read output non-blocking
                try:
                    line = process.stdout.readline()
                    if line:
                        output_lines.append(line)
                        print(f"  {line.rstrip()}")

                        # Check for server startup patterns
                        line_lower = line.lower()
                        if any(pattern in line_lower for pattern in SERVER_STARTUP_PATTERNS):
                            if not server_detected:
                                logger.info("🔄 Server startup detected, verifying...")
                                server_detected = True
                                # Wait a moment then check health
                                time.sleep(3)
                                if self.check_server_health(port, timeout=10):
                                    self.kill_process_tree(process)
                                    return True, ''.join(output_lines)
                except Exception:
                    pass

                time.sleep(0.2)  # Reduced polling frequency

            # Timeout - final health check before giving up
            output = ''.join(output_lines)
            
            if self.check_server_health(port, timeout=5):
                logger.info("✅ Server responding (detected via health check)")
                self.kill_process_tree(process)
                return True, output

            if len(output) > 100 and "error" not in output.lower():
                logger.warning("⚠️ Timeout but no obvious errors")
                self.kill_process_tree(process)
                return True, output

            logger.error("❌ Deployment test timeout")
            self.kill_process_tree(process)
            return False, output + "\n\n[TIMEOUT: Server did not start within expected time]"

        except Exception as e:
            error_msg = f"Exception during deployment test: {e}"
            logger.error(f"❌ {error_msg}")
            if process:
                self.kill_process_tree(process)
            return False, error_msg

    def save_result(self, repo_path: Path, repo_name: str, success: bool, 
                    log_content: str = None, attempt: int = None, final: bool = False):
        """Save deployment script and logs to output directory."""
        deploy_script = repo_path / "deployment.sh"
        if not deploy_script.exists():
            return

        safe_name = repo_name.replace("/", "_")
        
        # Only add suffix for intermediate attempts
        suffix = "" if final else f"_attempt_{attempt}" if attempt else ""
        
        output_file = self.output_dir / f"{safe_name}_deployment{suffix}.sh"
        shutil.copy(deploy_script, output_file)
        logger.info(f"💾 Saved: {output_file.name}")

        if log_content:
            log_file = self.output_dir / f"{safe_name}_deployment{suffix}.log"
            with open(log_file, "w") as f:
                f.write(log_content)

        # Save metadata
        meta_file = self.output_dir / f"{safe_name}_deployment{suffix}_meta.json"
        with open(meta_file, 'w') as f:
            json.dump({
                "repo_name": repo_name,
                "success": success,
                "attempt": attempt,
                "final": final,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")
            }, f, indent=2)

    def run(self, json_path: Path, max_retries: int = 5) -> bool:
        """Main orchestration with retry loop for a single repo."""
        json_data = self.load_landscape_json(json_path)
        repo_name = json_data.get("repo_name")

        if not repo_name:
            logger.error("❌ No repo_name in JSON")
            return False

        repo_path = self.clone_repository(repo_name)
        if not repo_path:
            logger.error("❌ Failed to clone repository")
            return False

        analysis = json_data.get("analysis", {})
        port = self.get_port_from_analysis(analysis)
        
        previous_error = None
        last_output = None

        for attempt in range(1, max_retries + 1):
            logger.info(f"\n{'='*60}")
            logger.info(f"🔄 Attempt {attempt}/{max_retries}")
            logger.info(f"{'='*60}\n")

            prompt = self.build_aider_prompt(json_data, previous_error)
            
            success, script_content = self.run_aider(repo_path, prompt)
            if not success:
                previous_error = "Aider failed to create deployment.sh file."
                continue

            test_success, output = self.test_deployment(repo_path, port)
            last_output = output

            # Save intermediate attempt
            self.save_result(repo_path, repo_name, test_success, output, attempt=attempt)

            if test_success:
                logger.info("\n🎉 SUCCESS! Deployment script works!")
                self.save_result(repo_path, repo_name, True, output, final=True)
                return True
            else:
                logger.warning(f"⚠️ Attempt {attempt} failed")
                previous_error = output[-2000:] if output else "Unknown error"

        logger.error(f"\n❌ FAILED after {max_retries} attempts")
        self.save_result(repo_path, repo_name, False, last_output, final=True)
        return False

    def get_processed_repos(self) -> set:
        """Get set of repo names that have already been processed (have final meta.json)."""
        processed = set()
        for meta_file in self.output_dir.glob("*_deployment_meta.json"):
            # Skip attempt files
            if "_attempt_" in meta_file.name:
                continue
            # Extract repo name from filename
            # Format: owner_repo_deployment_meta.json -> owner/repo
            name = meta_file.name.replace("_deployment_meta.json", "")
            # Convert back to repo format (first underscore is owner/repo separator)
            parts = name.split("_")
            if len(parts) >= 2:
                processed.add(f"{parts[0]}/{parts[1]}")
        return processed
    
    def find_resume_index(self, jsonl_path: Path) -> int:
        """Find the index to resume from based on already processed repos."""
        processed = self.get_processed_repos()
        logger.info(f"📊 Found {len(processed)} already-processed repos")
        
        with open(jsonl_path, 'r') as f:
            for idx, line in enumerate(f):
                try:
                    data = json.loads(line.strip())
                    repo_name = data.get("repo_name", "")
                    if repo_name and repo_name not in processed:
                        logger.info(f"📍 Resume index: {idx} (first unprocessed: {repo_name})")
                        return idx
                except json.JSONDecodeError:
                    continue
        
        logger.info("✅ All repos have been processed!")
        return -1  # All processed

    def run_from_jsonl(self, jsonl_path: Path, analysis_dir: Path, max_retries: int = 5,
                       start_index: int = 0, limit: int = 0, auto_resume: bool = False,
                       num_threads: int = 1) -> Dict[str, Any]:
        """Process repos from a JSONL file, using corresponding landscape JSONs from analysis_dir.
        
        Args:
            jsonl_path: Path to the JSONL file with repo_name entries
            analysis_dir: Directory containing {owner}_{repo}_landscape.json files
            max_retries: Maximum retry attempts per repo
            start_index: Index in JSONL to start from (0-indexed)
            limit: Maximum number of repos to process (0 = all remaining)
            auto_resume: If True, skip already-processed repos
            num_threads: Number of threads for parallel processing (default: 1)
        """
        # Get already-processed repos if auto_resume is enabled
        processed_repos = self.get_processed_repos() if auto_resume else set()
        if auto_resume:
            logger.info(f"📊 Found {len(processed_repos)} already-processed repos (will skip)")
        
        # Read all lines from JSONL, filtering out already-processed repos
        repos = []
        skipped_processed = 0
        with open(jsonl_path, 'r') as f:
            for idx, line in enumerate(f):
                if idx < start_index:
                    continue
                try:
                    data = json.loads(line.strip())
                    repo_name = data.get("repo_name")
                    if repo_name:
                        # Skip already-processed repos when auto_resume is enabled
                        if auto_resume and repo_name in processed_repos:
                            skipped_processed += 1
                            continue
                        repos.append((idx, repo_name))
                except json.JSONDecodeError:
                    logger.warning(f"⚠️ Skipping malformed line {idx}")
        
        if auto_resume and skipped_processed > 0:
            logger.info(f"⏭️ Skipped {skipped_processed} already-processed repos")
        
        if limit > 0:
            repos = repos[:limit]
        
        total = len(repos)
        logger.info(f"\n{'='*60}")
        logger.info(f"📋 JSONL Processing: {jsonl_path.name}")
        logger.info(f"   Start index: {start_index}")
        logger.info(f"   Repos to process: {total}")
        logger.info(f"{'='*60}\n")
        
        if total == 0:
            logger.info("✅ All repos already processed!")
            return {"success": [], "failed": [], "skipped": [], "total": 0}
        
        results = {"success": [], "failed": [], "skipped": [], "total": total, "start_index": start_index}
        
        def process_one(item: Tuple[int, int, str]) -> Tuple[int, str, bool, Optional[str]]:
            """Process a single repo. Returns (idx, repo_name, success, skip_reason)."""
            i, idx, repo_name = item
            safe_name = repo_name.replace("/", "_")
            json_path = analysis_dir / f"{safe_name}_landscape.json"
            
            logger.info(f"\n[{i}/{total}] Processing index {idx}: {repo_name}")
            
            if not json_path.exists():
                logger.warning(f"⚠️ Landscape JSON not found: {json_path}")
                return idx, repo_name, False, "no_landscape_json"
            
            try:
                success = self.run(json_path, max_retries)
                return idx, repo_name, success, None
            except Exception as e:
                logger.error(f"❌ Error processing {repo_name}: {e}")
                return idx, repo_name, False, str(e)
        
        # Prepare items with progress counter
        items = [(i, idx, repo_name) for i, (idx, repo_name) in enumerate(repos, 1)]
        
        if num_threads > 1:
            logger.info(f"🧵 Processing with {num_threads} threads...")
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = {executor.submit(process_one, item): item for item in items}
                for future in as_completed(futures):
                    idx, repo_name, success, skip_reason = future.result()
                    with self._lock:
                        if skip_reason == "no_landscape_json":
                            results["skipped"].append({"index": idx, "repo": repo_name, "reason": skip_reason})
                        elif skip_reason:
                            results["failed"].append({"index": idx, "repo": repo_name, "error": skip_reason})
                        elif success:
                            results["success"].append({"index": idx, "repo": repo_name})
                        else:
                            results["failed"].append({"index": idx, "repo": repo_name})
        else:
            for item in items:
                idx, repo_name, success, skip_reason = process_one(item)
                if skip_reason == "no_landscape_json":
                    results["skipped"].append({"index": idx, "repo": repo_name, "reason": skip_reason})
                elif skip_reason:
                    results["failed"].append({"index": idx, "repo": repo_name, "error": skip_reason})
                elif success:
                    results["success"].append({"index": idx, "repo": repo_name})
                else:
                    results["failed"].append({"index": idx, "repo": repo_name})
        
        # Save batch summary with index info
        summary_file = self.output_dir / "jsonl_batch_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(results, f, indent=2)
        
        logger.info(f"\n{'='*60}")
        logger.info("JSONL BATCH SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Start Index: {start_index}")
        logger.info(f"Total Processed: {total}")
        logger.info(f"Success: {len(results['success'])}")
        logger.info(f"Failed: {len(results['failed'])}")
        logger.info(f"Skipped: {len(results['skipped'])}")
        
        return results

    def run_batch(self, json_dir: Path, max_retries: int = 5, 
                  limit: int = 0, num_threads: int = 1) -> Dict[str, Any]:
        """Process multiple JSON files from a directory."""
        json_files = list(json_dir.glob("*_landscape.json"))
        
        if limit > 0:
            json_files = json_files[:limit]
        
        logger.info(f"📋 Found {len(json_files)} landscape JSON files")
        
        results = {"success": [], "failed": [], "total": len(json_files)}
        
        def process_one(json_path: Path) -> Tuple[Path, bool]:
            try:
                success = self.run(json_path, max_retries)
                return json_path, success
            except Exception as e:
                logger.error(f"❌ Error processing {json_path}: {e}")
                return json_path, False
        
        if num_threads > 1:
            logger.info(f"🧵 Processing with {num_threads} threads...")
            with ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = {executor.submit(process_one, jp): jp for jp in json_files}
                for future in as_completed(futures):
                    json_path, success = future.result()
                    with self._lock:
                        if success:
                            results["success"].append(str(json_path))
                        else:
                            results["failed"].append(str(json_path))
        else:
            for json_path in json_files:
                _, success = process_one(json_path)
                if success:
                    results["success"].append(str(json_path))
                else:
                    results["failed"].append(str(json_path))
        
        # Save batch summary
        summary_file = self.output_dir / "batch_summary.json"
        with open(summary_file, 'w') as f:
            json.dump(results, f, indent=2)
        
        logger.info(f"\n{'='*60}")
        logger.info("BATCH SUMMARY")
        logger.info(f"{'='*60}")
        logger.info(f"Total: {results['total']}")
        logger.info(f"Success: {len(results['success'])}")
        logger.info(f"Failed: {len(results['failed'])}")
        
        return results


def main():
    parser = argparse.ArgumentParser(
        description="Generate deployment.sh using Aider AI from landscape JSON"
    )
    parser.add_argument(
        "json_path",
        type=Path,
        nargs="?",
        help="Path to the landscape JSON file (single mode)"
    )
    parser.add_argument(
        "--batch",
        type=Path,
        help="Path to directory containing landscape JSON files (batch mode)"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum retry attempts (default: 5)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./deployment_output"),
        help="Output directory for generated scripts"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit number of repos to process in batch mode (0 = all)"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Number of threads for batch processing (default: 1)"
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        help="Path to JSONL file with repo_name entries (requires --analysis-dir)"
    )
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        help="Directory containing landscape JSON files (for --jsonl mode)"
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Start index in JSONL file (0-indexed, default: 0)"
    )
    parser.add_argument(
        "--auto-resume",
        action="store_true",
        help="Automatically detect resume index based on already-processed repos"
    )
    parser.add_argument(
        "--show-progress",
        action="store_true",
        help="Show current progress (processed count and resume index)"
    )

    args = parser.parse_args()

    generator = DeploymentGenerator(args.output_dir)

    # Show progress mode
    if args.show_progress:
        if not args.jsonl:
            parser.error("--show-progress requires --jsonl")
        
        processed = generator.get_processed_repos()
        resume_idx = generator.find_resume_index(args.jsonl)
        
        # Count total lines
        with open(args.jsonl, 'r') as f:
            total_lines = sum(1 for _ in f)
        
        print(f"\n{'='*60}")
        print(f"📊 Progress Report: {args.jsonl.name}")
        print(f"{'='*60}")
        print(f"Total repos in JSONL: {total_lines}")
        print(f"Processed repos: {len(processed)}")
        print(f"Remaining: {total_lines - len(processed)}")
        print(f"Resume index: {resume_idx}")
        print(f"{'='*60}\n")
        return 0

    # Validate arguments
    if args.jsonl:
        if not args.analysis_dir:
            parser.error("--jsonl requires --analysis-dir")
        if not args.jsonl.exists():
            parser.error(f"JSONL file not found: {args.jsonl}")
        if not args.analysis_dir.exists():
            parser.error(f"Analysis directory not found: {args.analysis_dir}")
    elif not args.batch and not args.json_path:
        parser.error("One of json_path, --batch, or --jsonl is required")
    
    if args.batch and args.json_path:
        parser.error("Cannot use both json_path and --batch")

    try:
        if args.jsonl:
            results = generator.run_from_jsonl(
                args.jsonl,
                args.analysis_dir,
                args.max_retries,
                args.start_index,
                args.limit,
                args.auto_resume,
                args.threads
            )
            return 0 if len(results.get("failed", [])) == 0 else 1
        elif args.batch:
            if not args.batch.exists() or not args.batch.is_dir():
                logger.error(f"❌ Batch directory not found: {args.batch}")
                return 1
            results = generator.run_batch(
                args.batch, 
                args.max_retries, 
                args.limit,
                args.threads
            )
            return 0 if len(results["failed"]) == 0 else 1
        else:
            if not args.json_path.exists():
                logger.error(f"❌ JSON file not found: {args.json_path}")
                return 1
            success = generator.run(args.json_path, args.max_retries)
            return 0 if success else 1
            
    except KeyboardInterrupt:
        logger.warning("\n⚠️ Interrupted by user")
        return 1


if __name__ == "__main__":
    exit(main())
