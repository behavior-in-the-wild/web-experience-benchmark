"""Node for cloning a GitHub repository."""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from cwv_optimizer.config import get_settings
from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.langgraph_app.nodes.base import run_with_timing

logger = get_logger(__name__)


def setup_run_directory(repo_name: str) -> Dict[str, Path]:
    """Create the run directory structure for a repository.
    
    Structure:
        dumps/{repo_name}_{timestamp}/
            codebase/           # cloned repository
            logs/               # all logs
                run.log         # main execution log
                server.log      # server output
                branches/       # aider logs per suggestion
            results/            # test results
                cwv_summary.json
                visual_regression.json
                application_results.json
            screenshots/        # visual regression images
    
    Returns:
        Dict with paths to all directories and files
    """
    settings = get_settings()
    
    # Create timestamped run directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = settings.dumps_dir / f"{repo_name}_{timestamp}"
    
    # Create subdirectories
    codebase_dir = run_dir / "codebase"
    logs_dir = run_dir / "logs"
    branch_logs_dir = logs_dir / "branches"
    results_dir = run_dir / "results"
    screenshots_dir = run_dir / "screenshots"
    
    run_dir.mkdir(parents=True, exist_ok=True)
    codebase_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    branch_logs_dir.mkdir(exist_ok=True)
    results_dir.mkdir(exist_ok=True)
    screenshots_dir.mkdir(exist_ok=True)
    
    # Create log file in logs directory
    log_file = logs_dir / "run.log"
    log_file.touch()
    
    return {
        "run_dir": run_dir,
        "codebase_dir": codebase_dir,
        "logs_dir": logs_dir,
        "branch_logs_dir": branch_logs_dir,
        "results_dir": results_dir,
        "screenshots_dir": screenshots_dir,
        "log_file": log_file,
    }


def setup_file_logger(log_file: Path, repo_name: str) -> logging.Logger:
    """Set up a file logger that captures all output for this run.
    
    This logger will capture:
    - All pipeline node logs
    - Claude Code command output
    - Deployment script output
    - Error traces
    """
    # Create a dedicated logger for this run
    run_logger = logging.getLogger(f"cwv_run.{repo_name}")
    run_logger.setLevel(logging.DEBUG)
    
    # Remove existing handlers
    run_logger.handlers.clear()
    
    # Create file handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    
    # Create formatter with timestamp
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    
    run_logger.addHandler(file_handler)
    
    # Also add a handler for the root logger to capture everything
    root_logger = logging.getLogger()
    root_file_handler = logging.FileHandler(log_file)
    root_file_handler.setLevel(logging.DEBUG)
    root_file_handler.setFormatter(formatter)
    root_logger.addHandler(root_file_handler)
    
    return run_logger


async def _clone_github_repo(
    github_url: str,
    codebase_dir: Path,
    run_logger: logging.Logger,
    revision_id: str | None = None,
) -> Dict[str, Any]:
    """Clone a GitHub repository into the codebase directory.

    Args:
        github_url: The GitHub repository URL
        codebase_dir: Directory to clone into
        run_logger: Logger for this run
        revision_id: Optional commit hash to checkout

    Returns:
        Dict containing status, clone path, and metadata
    """
    try:
        run_logger.info("=" * 60)
        run_logger.info("CLONE REPOSITORY")
        run_logger.info("=" * 60)
        run_logger.info(f"GitHub URL: {github_url}")
        run_logger.info(f"Target directory: {codebase_dir}")
        run_logger.info(f"Revision: {revision_id or 'HEAD'}")
        
        # Clone the repository
        logger.info("Cloning repository: %s -> %s", github_url, codebase_dir)
        run_logger.info(f"Running: git clone {github_url} {codebase_dir}")

        clone_result = subprocess.run(
            ["git", "clone", github_url, str(codebase_dir)],
            capture_output=True,
            text=True,
        )
        
        run_logger.info(f"STDOUT: {clone_result.stdout}")
        if clone_result.stderr:
            run_logger.info(f"STDERR: {clone_result.stderr}")

        if clone_result.returncode != 0:
            error_msg = f"Failed to clone repository: {clone_result.stderr}"
            logger.error("Git clone failed: %s", clone_result.stderr)
            run_logger.error(error_msg)
            return {"status": "error", "error": error_msg}

        # Checkout specific revision if provided
        if revision_id:
            logger.info("Checking out revision: %s", revision_id)
            run_logger.info(f"Running: git checkout {revision_id}")
            
            checkout_result = subprocess.run(
                ["git", "checkout", revision_id],
                cwd=str(codebase_dir),
                capture_output=True,
                text=True,
            )
            
            run_logger.info(f"STDOUT: {checkout_result.stdout}")
            if checkout_result.stderr:
                run_logger.info(f"STDERR: {checkout_result.stderr}")

            if checkout_result.returncode != 0:
                run_logger.warning(
                    f"Failed to checkout revision {revision_id}: {checkout_result.stderr}"
                )
                logger.warning(
                    "Failed to checkout revision %s: %s",
                    revision_id,
                    checkout_result.stderr,
                )

        run_logger.info("Clone completed successfully")
        run_logger.info("-" * 60)
        
        return {
            "status": "success",
            "clone_path": str(codebase_dir),
        }

    except Exception as e:
        error_msg = f"Error cloning repository: {e}"
        logger.error(error_msg, exc_info=True)
        run_logger.error(error_msg, exc_info=True)
        return {"status": "error", "error": str(e)}


async def clone_repo_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """LangGraph node that clones a GitHub repository.
    
    Creates the run directory structure:
        dumps/{repo_name}_{timestamp}/
            codebase/   # cloned repository
            results/    # test results
            logs/       # execution logs
    """

    async def _impl(current_state: Dict[str, Any]) -> Dict[str, Any]:
        github_url = current_state.get("github_url")
        if not github_url:
            raise RuntimeError("github_url is required in state")

        # Extract repo name from URL
        repo_name = github_url.rstrip("/").split("/")[-1].replace(".git", "")
        revision_id = current_state.get("revision_id")

        # Set up run directory structure
        paths = setup_run_directory(repo_name)
        run_dir = paths["run_dir"]
        codebase_dir = paths["codebase_dir"]
        logs_dir = paths["logs_dir"]
        branch_logs_dir = paths["branch_logs_dir"]
        results_dir = paths["results_dir"]
        screenshots_dir = paths["screenshots_dir"]
        log_file = paths["log_file"]
        
        # Set up file logger
        run_logger = setup_file_logger(log_file, repo_name)
        
        run_logger.info("=" * 60)
        run_logger.info("CWV OPTIMIZATION RUN STARTED")
        run_logger.info("=" * 60)
        run_logger.info(f"Repository: {github_url}")
        run_logger.info(f"Run directory: {run_dir}")
        run_logger.info(f"Timestamp: {datetime.now().isoformat()}")
        run_logger.info("=" * 60)

        # Clone the repository
        result = await _clone_github_repo(
            github_url=github_url,
            codebase_dir=codebase_dir,
            run_logger=run_logger,
            revision_id=revision_id,
        )

        if result.get("status") != "success":
            error = result.get("error", "Unknown error during clone")
            current_state.setdefault("errors", []).append(error)
            raise RuntimeError(error)

        # Update state with all paths
        current_state["workspace_dir"] = str(codebase_dir)
        current_state["run_dir"] = str(run_dir)
        current_state["logs_dir"] = str(logs_dir)
        current_state["branch_logs_dir"] = str(branch_logs_dir)
        current_state["results_dir"] = str(results_dir)
        current_state["screenshots_dir"] = str(screenshots_dir)
        current_state["log_file"] = str(log_file)
        current_state["repo_name"] = repo_name

        logger.info("Repository cloned to: %s", codebase_dir)
        return current_state

    return await run_with_timing("clone_repo", state, _impl)
