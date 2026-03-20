"""Node for generating deployment script and starting the local server.

This node reads the analysis.json from repo_analyzer_node, uses Aider AI
to generate a deployment.sh script, tests it, and starts the server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)

# Config
AZURE_DEPLOYMENT = os.getenv("AZURE_DEPLOYMENT", "gpt-4o")
AIDER_TIMEOUT = 300  # 5 minutes for Aider to generate
DEPLOY_TEST_TIMEOUT = 120  # 2 minutes for deployment test
MAX_RETRIES = 5


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


def build_aider_prompt(analysis: Dict, repo_name: str, previous_error: str = None) -> str:
    """Build the Aider prompt for generating deployment.sh."""
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
3. **Starts the development server** locally on port 8000

### Requirements:
- Must be executable bash script (start with `#!/bin/bash`)
- Use `set -e` to exit on errors
- Print clear status messages for each step
- Handle missing dependencies gracefully
- The server should run in foreground (not daemonized)
- Use PORT=8000 for the server

### Key Information from Analysis:
- **Framework**: {analysis.get('primary_framework', {}).get('name', 'Unknown')}
- **Package Manager**: {analysis.get('package_manager', {}).get('primary', 'Unknown')}
- **Install Command**: {analysis.get('scripts', {}).get('install', 'Unknown')}
- **Dev Command**: {analysis.get('scripts', {}).get('dev', 'Unknown')}
- **Port**: {analysis.get('development_server', {}).get('port', 'Unknown')}
- **System Dependencies**: {analysis.get('dependencies', {}).get('system', [])}

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

# Step 4: Start development server on port 8000
log "🌐 Starting development server..."
export PORT=8000
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


def run_aider(repo_path: Path, prompt: str, run_logger: logging.Logger) -> bool:
    """Run Aider to generate or fix deployment.sh."""
    run_logger.info("🤖 Running Aider to generate deployment.sh...")
    with open(repo_path / "prompts.log", "a") as f:
        f.write("=== DEPLOY GENERATOR PROMPT ===\n")
        f.write(prompt)
        f.write("\n==============================\n\n")

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

        run_logger.info(f"Running command: {' '.join(cmd[:3])} --message-file <prompt> ...")

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=AIDER_TIMEOUT,
        )

        run_logger.debug(f"Aider stdout: {result.stdout[:1000] if result.stdout else '(empty)'}")
        run_logger.debug(f"Aider stderr: {result.stderr[:500] if result.stderr else '(empty)'}")
        run_logger.info(f"Aider return code: {result.returncode}")

        # Check if deployment.sh was created with actual content
        deploy_script = repo_path / "deployment.sh"
        if deploy_script.exists():
            script_content = deploy_script.read_text().strip()
            if len(script_content) < 50:
                run_logger.warning(f"⚠️ Aider created deployment.sh but it's too short ({len(script_content)} chars)")
                deploy_script.unlink()
                return False
            run_logger.info(f"✅ Aider created deployment.sh ({len(script_content)} chars)")
            run_logger.info(f"Script preview:\n{script_content[:300]}...")
            # Make it executable
            os.chmod(deploy_script, 0o755)
            return True
        else:
            run_logger.warning("⚠️ Aider did not create deployment.sh")
            return False

    except subprocess.TimeoutExpired:
        run_logger.error("❌ Aider timeout")
        return False
    except Exception as e:
        run_logger.error(f"❌ Aider error: {e}")
        return False
    finally:
        if prompt_file.exists():
            prompt_file.unlink()


def test_deployment(repo_path: Path, run_logger: logging.Logger) -> Tuple[bool, str]:
    """Test the deployment.sh script."""
    deploy_script = repo_path / "deployment.sh"

    if not deploy_script.exists():
        return False, "deployment.sh not found"

    script_content = deploy_script.read_text().strip()
    if len(script_content) < 50:
        return False, f"deployment.sh is too short ({len(script_content)} chars)"

    run_logger.info("🧪 Testing deployment.sh...")

    try:
        process = subprocess.Popen(
            ["bash", "./deployment.sh"],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PORT": "8000"}
        )

        output_lines = []
        start_time = time.time()

        while time.time() - start_time < DEPLOY_TEST_TIMEOUT:
            retcode = process.poll()
            if retcode is not None:
                remaining = process.stdout.read()
                if remaining:
                    output_lines.append(remaining)
                output = ''.join(output_lines)

                if retcode == 0:
                    run_logger.info("✅ Deployment script completed successfully")
                    return True, output
                else:
                    run_logger.error(f"❌ Deployment script failed with code {retcode}")
                    return False, output

            try:
                line = process.stdout.readline()
                if line:
                    output_lines.append(line)
                    run_logger.info(f"  {line.rstrip()}")

                    # Check for server startup indicators
                    if any(indicator in line.lower() for indicator in [
                        "server running",
                        "listening on",
                        "started server",
                        "development server",
                        "ready on",
                        "localhost:",
                        "127.0.0.1:",
                        ":8000"
                    ]):
                        run_logger.info("✅ Server appears to have started!")
                        time.sleep(2)
                        process.terminate()
                        return True, ''.join(output_lines)
            except Exception:
                pass

            time.sleep(0.1)

        # Timeout
        process.terminate()
        output = ''.join(output_lines)

        if len(output) > 100 and "error" not in output.lower():
            run_logger.warning("⚠️ Timeout but no obvious errors - may be working")
            return True, output

        run_logger.error("❌ Deployment test timeout")
        return False, output + "\n\n[TIMEOUT: Server did not start within expected time]"

    except Exception as e:
        error_msg = f"Exception during deployment test: {e}"
        run_logger.error(f"❌ {error_msg}")
        return False, error_msg


async def start_server_background(
    workspace_dir: str,
    run_logger: logging.Logger,
) -> Dict[str, Any]:
    """Start the server in the background and verify it's responding."""
    deploy_script = Path(workspace_dir) / "deployment.sh"

    if not deploy_script.exists():
        error = "deployment.sh not found"
        run_logger.error(error)
        return {"status": "error", "error": error}

    subprocess.run(["chmod", "+x", str(deploy_script)], check=True)

    try:
        run_logger.info("=" * 60)
        run_logger.info("STARTING SERVER IN BACKGROUND")
        run_logger.info("=" * 60)
        logger.info("Starting server in background...")

        log_file = Path(workspace_dir).parent / "server.log"
        with open(log_file, "w") as f:
            process = subprocess.Popen(
                ["bash", str(deploy_script)],
                cwd=workspace_dir,
                stdout=f,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PORT": "8000"},
            )

        run_logger.info(f"Server process started with PID: {process.pid}")
        run_logger.info(f"Server log: {log_file}")

        import urllib.request
        import urllib.error

        max_wait = 30
        check_interval = 2
        elapsed = 0
        server_ready = False

        while elapsed < max_wait:
            await asyncio.sleep(check_interval)
            elapsed += check_interval

            if process.poll() is not None:
                with open(log_file, "r") as f:
                    log_content = f.read()[-2000:]
                run_logger.error(f"Server process exited with code {process.returncode}")
                run_logger.error(f"Server log:\n{log_content}")
                return {
                    "status": "error",
                    "error": f"Server exited with code {process.returncode}. Log: {log_content[-500:]}"
                }

            try:
                req = urllib.request.Request("http://127.0.0.1:8000", method="HEAD")
                urllib.request.urlopen(req, timeout=5)
                server_ready = True
                run_logger.info(f"Server is responding after {elapsed}s")
                break
            except urllib.error.URLError:
                run_logger.info(f"Waiting for server... ({elapsed}s)")
            except Exception as e:
                run_logger.info(f"Server check error: {e}")

        if server_ready:
            run_logger.info(f"Server started with PID: {process.pid}")
            run_logger.info("Server URL: http://127.0.0.1:8000")
            return {
                "status": "success",
                "pid": process.pid,
                "deployed_url": "http://127.0.0.1:8000",
            }
        else:
            if process.poll() is None:
                run_logger.warning("Server process running but not responding on port 8000")
                return {
                    "status": "success",
                    "pid": process.pid,
                    "deployed_url": "http://127.0.0.1:8000",
                    "warning": "Server may not be fully ready",
                }
            else:
                with open(log_file, "r") as f:
                    log_content = f.read()[-1000:]
                error = f"Server failed to start. Log: {log_content}"
                run_logger.error(error)
                return {"status": "error", "error": error}

    except Exception as e:
        run_logger.error(f"Error starting server: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


async def deploy_generator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that generates deployment script and starts server.

    Reads analysis.json from repo_analyzer_node, uses Aider AI to generate
    deployment.sh, tests it, and starts the development server.

    Input from state:
        - workspace_dir: Path to cloned repository
        - analysis_json_path: Path to analysis.json from repo_analyzer
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

        analysis_json_path = current_state.get("analysis_json_path")
        repo_name = current_state.get("repo_name", "unknown")
        log_file = current_state.get("log_file")
        reports_dir = current_state.get("reports_dir")

        run_logger = get_run_logger(log_file, repo_name)

        run_logger.info("=" * 60)
        run_logger.info("DEPLOY GENERATOR NODE")
        run_logger.info("=" * 60)
        run_logger.info(f"Workspace: {workspace_dir}")
        run_logger.info(f"Analysis JSON: {analysis_json_path}")

        repo_path = Path(workspace_dir)

        # Step 1: Load analysis JSON
        if analysis_json_path and Path(analysis_json_path).exists():
            with open(analysis_json_path, 'r') as f:
                analysis = json.load(f)
            run_logger.info(f"Loaded analysis: {analysis.get('primary_framework', {}).get('name', 'Unknown')}")
        else:
            # Fallback: try to find analysis.json in workspace
            fallback_path = repo_path / "analysis.json"
            if fallback_path.exists():
                with open(fallback_path, 'r') as f:
                    analysis = json.load(f)
                run_logger.info(f"Loaded analysis from workspace: {analysis.get('primary_framework', {}).get('name', 'Unknown')}")
            else:
                run_logger.warning("No analysis.json found, using empty analysis")
                analysis = {}

        # Step 2: Generate deployment script with retries
        previous_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            run_logger.info(f"\n{'='*60}")
            run_logger.info(f"🔄 Attempt {attempt}/{MAX_RETRIES}")
            run_logger.info(f"{'='*60}\n")

            prompt = build_aider_prompt(analysis, repo_name, previous_error)

            if not run_aider(repo_path, prompt, run_logger):
                run_logger.warning("⚠️ Aider failed to create deployment.sh, retrying...")
                previous_error = "Aider failed to create deployment.sh file. Please create the file."
                continue

            # Test deployment
            success, output = test_deployment(repo_path, run_logger)

            if success:
                run_logger.info("\n🎉 Deployment script works!")
                break
            else:
                run_logger.warning(f"⚠️ Attempt {attempt} failed")
                previous_error = output[-2000:] if output else "Unknown error"

        # Step 3: Copy deployment.sh to reports directory
        deploy_script = repo_path / "deployment.sh"
        if deploy_script.exists() and reports_dir:
            reports_path = Path(reports_dir) / "deployment.sh"
            shutil.copy(deploy_script, reports_path)
            run_logger.info(f"💾 Copied deployment.sh to {reports_path}")

        # Step 4: Start server in background
        run_logger.info("Starting server in background...")
        server_result = await start_server_background(workspace_dir, run_logger)

        if server_result.get("status") != "success":
            error = server_result.get("error", "Failed to start server")
            current_state.setdefault("errors", []).append(error)
            run_logger.error(f"Failed to start server: {error}")
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

        logger.info("Server deployed at: %s", server_result["deployed_url"])
        return current_state

    return await run_with_timing("deploy_generator", state, _impl)
