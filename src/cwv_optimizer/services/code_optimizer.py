"""Code optimization service using Claude CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from cwv_optimizer.core.logger import get_logger
from cwv_optimizer.core.utils import save_json_file

logger = get_logger(__name__)

# Aider imports and setup
try:
    from aider.models import Model
    from aider.io import InputOutput
    from aider.repomap import find_src_files
    from aider.coders.context_coder import ContextCoder
    io = InputOutput(yes=True)
    AIDER_AVAILABLE = True
except ImportError:
    AIDER_AVAILABLE = False
    logger.warning("Aider not available. Install with: pip install aider-chat")


def _run_claude_code(
    workspace_dir: str,
    prompt: str,
    agent: str = "claude",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply code changes using Claude CLI or Codex.
    
    Uses the `claude` CLI or `codex exec` which respects respective environment variables.

    Args:
        workspace_dir: Path to workspace directory
        prompt: Change prompt with optimization details
        agent: "claude" or "codex"

    Returns:
        Result dictionary with status
    """
    logger.info("Running %s for code optimization...", agent)
    logger.info(f"Workspace: {workspace_dir}")
    logger.info(f"Prompt length: {len(prompt)} chars")
    with open(Path(workspace_dir) / "prompts.log", "a") as f:
        f.write(f"=== OPTIMIZATION PROMPT ({agent}) ===\n")
        f.write(prompt)
        f.write("\n====================================\n\n")

    try:
        if agent == "claude":
            # Build Claude CLI command
            command = [
                "claude",
                "--print",
                prompt,
                "--dangerously-skip-permissions",
            ]
        elif agent == "codex":
            # Build Codex exec command
            command = [
                "codex",
                "exec",
                prompt,
                "--full-auto",
            ]
        elif agent == "opencode":
            # Build OpenCode run command
            command = [
                "opencode",
                "run",
                prompt,
            ]
            if model:
                command.extend(["--model", model])
        else:
            return {"status": "error", "error": f"Unknown agent: {agent}"}

        # Setup environment
        env = os.environ.copy()
        if agent == "opencode":
            from cwv_optimizer.config import get_settings
            settings = get_settings()

            # Matches template_opencodegpt51codex.sh logic: non-interactive permissions
            # and explicit provider configuration
            opencode_cfg = {
                "permission": {"question": "deny"},
                "provider": {
                    "azure": {
                        "options": {
                            "maxTokens": 50000,
                            "reasoning": {"effort": "medium"}
                        }
                    },
                    "openai": {
                        "options": {
                            "maxTokens": 50000,
                            "reasoning": {"effort": "medium"}
                        }
                    }
                }
            }
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(opencode_cfg)

            # Ensure Azure credentials are in env if using azure model
            if model and model.startswith("azure/"):
                if settings.azure_openai_api_key:
                    env["AZURE_OPENAI_API_KEY"] = settings.azure_openai_api_key
                
                if settings.azure_openai_endpoint:
                    env["AZURE_OPENAI_ENDPOINT"] = settings.azure_openai_endpoint
                    # Derive resource name: https://myresource.openai.azure.com -> myresource
                    endpoint = settings.azure_openai_endpoint
                    if "://" in endpoint:
                        resource = endpoint.split("://")[1].split(".")[0]
                        env["AZURE_RESOURCE_NAME"] = resource
                
                # Some versions of opencode use AZURE_API_KEY / AZURE_API_BASE
                if "AZURE_OPENAI_API_KEY" in env:
                    env["AZURE_API_KEY"] = env["AZURE_OPENAI_API_KEY"]
                if "AZURE_OPENAI_ENDPOINT" in env:
                    env["AZURE_API_BASE"] = env["AZURE_OPENAI_ENDPOINT"]

        # Run with environment variables inherited
        result = subprocess.run(
            command,
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=600,  # 10 minute timeout
            env=env,
        )

        logger.info(f"{agent} return code: {result.returncode}")
        if result.stdout:
            logger.info(f"{agent} output preview: {result.stdout[:500]}...")
        if result.stderr:
            logger.warning(f"{agent} stderr: {result.stderr[:500]}")

        return {
            "status": "success" if result.returncode == 0 else "error",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "error": result.stderr if result.returncode != 0 else None,
        }

    except subprocess.TimeoutExpired:
        logger.error("%s command timed out after 10 minutes", agent)
        return {"status": "error", "error": f"{agent} timed out"}
    except FileNotFoundError:
        logger.error("%s CLI not found", agent)
        if agent == "claude":
            error_msg = "Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        elif agent == "opencode":
            error_msg = "OpenCode CLI not found. Install with: curl -fsSL https://opencode.ai/install | bash"
        else:
            error_msg = f"{agent} CLI not found. Please install it."
        return {"status": "error", "error": error_msg}
    except Exception as e:
        logger.error("%s optimization failed: %s", agent, e, exc_info=True)
        return {"status": "error", "error": str(e)}


def url_filter(file_path: str) -> bool:
    """Filter to choose HTML, non-minified JS/CSS, or etc.clientlibs assets."""
    if file_path.endswith(".html"):
        return True
    if file_path.endswith(".js"):
        if ("clientlibs" in file_path) or (not file_path.endswith(".min.js")):
            return True
    if file_path.endswith(".css"):
        if ("clientlibs" in file_path) or (not file_path.endswith(".min.css")):
            return True
    return False


def format_model_name(model_name, for_code_apply=False):
    """Simple model name formatting."""
    return model_name


def get_performance_context_prompt(src_files_relative, suggestion):
    return f"Based on the performance suggestion: {suggestion.get('title', '')} {suggestion.get('description', '')}, which files from {src_files_relative} should be edited? List the most relevant files in backticks."


def create_performance_aware_prompt(suggestion, edit_files):
    return f"Apply the following performance optimization:\n\nTitle: {suggestion.get('title', '')}\n\nDescription: {suggestion.get('description', '')}\n\nImplementation: {suggestion.get('implementation', '')}\n\nMake the necessary changes to improve performance. Focus on files: {edit_files}"


def get_performance_focused_context(output_dir: str, model: Model, suggestion: Dict) -> List[str]:
    """Select top-priority files for aider based on the suggestion's focus."""
    if not AIDER_AVAILABLE:
        return []
    src_files = [f for f in find_src_files(output_dir) if url_filter(f)]
    context_coder = ContextCoder(
        main_model=model, io=io, fnames=src_files, detect_urls=False
    )
    skiplen = len(output_dir) + (0 if output_dir.endswith("/") else 1)
    src_files_relative = [f[skiplen:] for f in src_files]
    perf_prompt = get_performance_context_prompt(src_files_relative, suggestion)
    response = context_coder.run(perf_prompt)
    potential_files = re.findall(r"`(.*?)`", response)
    valid_files = [pf for pf in potential_files if pf in src_files_relative]
    if not valid_files:
        critical = [
            f
            for f in src_files_relative
            if any(tok in f.lower() for tok in ["index", "scripts", "styles", "main"])
        ]
        valid_files = critical[:3] if critical else src_files_relative[:3]
    return valid_files


def apply_code_changes(
    output_dir: str,
    suggestion: Dict,
    model_name: str,
    suggestion_id: str,
    use_architect: bool = True,
    branch_logs_dir: Optional[str] = None,
):
    """Apply a single suggestion using aider."""
    if not AIDER_AVAILABLE:
        logger.error("Aider not available")
        return
    page_dump_dir = Path(output_dir).parent
    
    # Use provided branch_logs_dir or fallback to old structure
    if branch_logs_dir:
        logs_dir = Path(branch_logs_dir)
    else:
        logs_dir = page_dump_dir / "branch_logs"
    logs_dir.mkdir(exist_ok=True)
    log_file_path = logs_dir / f"{suggestion_id}.txt"
    formatted_model = format_model_name(model_name, for_code_apply=True)
    model = Model(formatted_model)
    edit_files = get_performance_focused_context(output_dir, model, suggestion)
    if not edit_files:
        logger.info("No target files found for suggestion, skipping.")
        return
    prompt = create_performance_aware_prompt(suggestion, edit_files)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(prompt)
        temp_file = f.name
    readonly_patterns = [
        "styles.css",
        "lazy-styles.css",
    ]
    skiplen = len(output_dir) + (0 if output_dir.endswith("/") else 1)
    rel_files = [f[skiplen:] for f in find_src_files(output_dir) if url_filter(f)]
    readonly_files = [
        f for f in rel_files if any(f.endswith(p) for p in readonly_patterns)
    ]
    readonly_flags = " ".join(f"--read {f}" for f in readonly_files)
    
    # Detect the default branch dynamically
    default_branch = _get_default_branch(output_dir)
    
    # Allow overriding the editor model via environment for environments
    # where a specific editor model (e.g., Azure) is not available.
    editor_model = os.environ.get('AIDER_EDITOR_MODEL') or os.environ.get('AIDER_EDITOR')
    if not editor_model:
        # Default to the same model used for the main run if available
        editor_model = formatted_model

    if use_architect:
        command = (
            f"git checkout -b {suggestion_id} && "
            f"aider {' '.join(edit_files)} {readonly_flags} "
            f"--model {formatted_model} --architect --editor-model {editor_model} "
            f"--message-file '{temp_file}' --no-show-model-warnings --no-auto-commits --no-gitignore --llm-history-file '{log_file_path}' --yes"
        )
        with open(Path(output_dir) / "prompts.log", "a") as f:
            f.write("=== ARCHITECT PROMPT ===\n")
            f.write(prompt)
            f.write("\n========================\n\n")
        command_finish = (
            f"git add . && (git reset -- .aider* 2>/dev/null || true) && "
            f"git commit -m 'Apply performance suggestion' && "
            f"git checkout -f {default_branch}"
        )
    else:
        command = (
            f"git checkout -b {suggestion_id} && "
            f"aider {' '.join(edit_files)} {readonly_flags} "
            f"--model {formatted_model} --editor-model {editor_model} "
            f"--message-file '{temp_file}' --no-show-model-warnings --no-auto-commits --no-gitignore --llm-history-file '{log_file_path}' --yes"
        )
        with open(Path(output_dir) / "prompts.log", "a") as f:
            f.write("=== AIDER OPTIMIZATION PROMPT ===\n")
            f.write(prompt)
            f.write("\n=================================\n\n")
        command_finish = (
            f"git add . && (git reset -- .aider* 2>/dev/null || true) && "
            f"git commit -m 'Apply performance suggestion' && "
            f"git checkout -f {default_branch}"
        )
    try:
        logger.info("🏗️  Running aider to apply suggestion…")
        subprocess.run(command, shell=True, check=True, cwd=output_dir)
        subprocess.run(command_finish, shell=True, check=True, cwd=output_dir)
    except subprocess.CalledProcessError as e:
        logger.error("aider failed: %s", e)
    finally:
        os.unlink(temp_file)


def _get_default_branch(workspace_dir: str) -> str:
    """Detect the default branch of the repository (main or master)."""
    try:
        # Try to get the default branch from remote HEAD
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # Output is like "refs/remotes/origin/main"
            return result.stdout.strip().split("/")[-1]
    except Exception:
        pass
    
    # Fallback: check if main or master exists
    for branch in ["main", "master"]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=workspace_dir,
            capture_output=True,
        )
        if result.returncode == 0:
            return branch
    
    # Last resort: get current branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    
    return "main"  # Ultimate fallback


def _create_branch(workspace_dir: str, branch_name: str) -> bool:
    """Create and checkout a new git branch."""
    try:
        # Detect the default branch
        default_branch = _get_default_branch(workspace_dir)
        logger.debug("Using default branch: %s", default_branch)
        
        # Ensure we're on the default branch first
        subprocess.run(
            ["git", "checkout", default_branch],
            cwd=workspace_dir,
            capture_output=True,
            check=True,
        )

        # Create new branch
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=workspace_dir,
            capture_output=True,
            check=True,
        )

        return True
    except subprocess.CalledProcessError as e:
        logger.error("Failed to create branch %s: %s", branch_name, e)
        return False


def _commit_changes(workspace_dir: str, message: str) -> bool:
    """Stage and commit all changes."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace_dir,
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace_dir,
            capture_output=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )

        return True
    except subprocess.CalledProcessError:
        return False


async def apply_code_optimizations(
    suggestions_path: str,
    workspace_dir: str,
    apply_count: int = 1,
    agent: str = "claude",
    model: str = "azure/gpt-5",
    use_architect: bool = True,
    branch_logs_dir: Optional[str] = None,
    results_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply performance suggestions to create optimization branches.

    Args:
        suggestions_path: Path to suggestions JSON file
        workspace_dir: Path to workspace directory
        apply_count: Number of attempts per suggestion
        branch_logs_dir: Directory for branch logs
        results_dir: Directory for results (clean structure)

    Returns:
        Result dictionary with status and output paths
    """
    logger.info("Applying optimizations from: %s", suggestions_path)

    try:
        results: List[Dict] = []
        workspace_path = Path(workspace_dir)
        dump_dir = workspace_path.parent

        with open(suggestions_path, "r") as f:
            suggestions_data = json.load(f)

        suggestions = suggestions_data.get("suggestions", [])

        # Ensure prompts.log is never tracked by git so it won't appear in patches
        gitignore_path = workspace_path / ".gitignore"
        gitignore_entry = "prompts.log\n"
        existing = gitignore_path.read_text(errors="ignore") if gitignore_path.exists() else ""
        if "prompts.log" not in existing:
            with open(gitignore_path, "a") as _gi:
                _gi.write(gitignore_entry)
            logger.debug("Added prompts.log to .gitignore in %s", workspace_dir)

        # Pseudo-file names that signal infrastructure/environment changes rather than
        # actual source-file edits.  The agent cannot make these changes, so skip them.
        _INFRA_PSEUDO_FILES = {
            "server_infrastructure",
            "server_config",
            "infrastructure",
            "deployment",
            "hosting",
            "cdn_config",
            "environment",
        }

        def _is_infrastructure_only(suggestion: dict) -> bool:
            """Return True if every codeChange targets a pseudo/infra file name."""
            changes = suggestion.get("codeChanges", [])
            if not changes:
                return False
            return all(
                (c.get("file") or "").lower().replace("-", "_").replace(" ", "_")
                in _INFRA_PSEUDO_FILES
                for c in changes
            )

        for idx, suggestion in enumerate(suggestions, 1):
            if _is_infrastructure_only(suggestion):
                logger.info(
                    "Skipping suggestion %d ('%s') — infrastructure-only, no source files to edit",
                    idx,
                    suggestion.get("title", ""),
                )
                results.append({
                    "suggestion_index": idx,
                    "status": "skipped",
                    "reason": "infrastructure-only suggestion",
                    "suggestion": suggestion,
                })
                continue
            for run_num in range(1, apply_count + 1):
                branch_name = f"suggestion_{idx}_run{run_num}_{uuid.uuid4().hex[:8]}"

                logger.info(
                    "Applying suggestion %d/%d (run %d/%d) -> %s",
                    idx, len(suggestions), run_num, apply_count, branch_name,
                )

                if agent == "aider":
                    if not AIDER_AVAILABLE:
                        results.append({
                            "suggestion_index": idx,
                            "run": run_num,
                            "status": "error",
                            "error": "Aider not available",
                            "suggestion": suggestion,
                        })
                        continue
                    apply_code_changes(
                        output_dir=workspace_dir,
                        suggestion=suggestion,
                        model_name=model,
                        suggestion_id=branch_name,
                        use_architect=use_architect,
                        branch_logs_dir=branch_logs_dir,
                    )
                    results.append({
                        "suggestion_index": idx,
                        "run": run_num,
                        "branch": branch_name,
                        "status": "success",
                        "suggestion": suggestion,
                    })
                else:
                    if not _create_branch(workspace_dir, branch_name):
                        results.append({
                            "suggestion_index": idx,
                            "run": run_num,
                            "status": "error",
                            "error": "Failed to create branch",
                            "suggestion": suggestion,
                        })
                        continue

                    # Build prompt from suggestion
                    title = suggestion.get("title", "Optimization")
                    description = suggestion.get("description", "")
                    solution = suggestion.get("solution", "")
                    implementation = suggestion.get("implementation", suggestion.get("code", ""))
                    code_changes = suggestion.get("codeChanges", [])

                    prompt = f"Apply the following performance optimization:\n\n"
                    prompt += f"Title: {title}\n"
                    prompt += f"Description: {description}\n"
                    if solution:
                        prompt += f"Solution: {solution}\n"
                    
                    if implementation:
                        prompt += f"\nImplementation:\n{implementation}\n"
                    elif code_changes:
                        prompt += f"\nProposed Code Changes:\n{json.dumps(code_changes, indent=2)}\n"

                    prompt += "\nMake the necessary changes to improve performance.\n"

                    result = _run_claude_code(
                        workspace_dir=workspace_dir,
                        prompt=prompt,
                        agent=agent,
                        model=model,
                    )

                    if result.get("status") == "success":
                        _commit_changes(
                            workspace_dir,
                            f"Apply optimization: {suggestion.get('title', 'suggestion')}",
                        )

                    results.append({
                        "suggestion_index": idx,
                        "run": run_num,
                        "branch": branch_name,
                        "status": result.get("status"),
                        "suggestion": suggestion,
                    })

        # Save results to results_dir if provided, otherwise to dump_dir
        if results_dir:
            output_path = Path(results_dir) / "application_results.json"
        else:
            output_path = dump_dir / "application_results.json"
        
        save_json_file(
            {
                "timestamp": datetime.now().isoformat(),
                "suggestions_path": suggestions_path,
                "agent": agent,
                "model": model,
                "results": results,
            },
            output_path,
        )

        return {
            "status": "success",
            "output_paths": {
                "application_results_path": str(output_path),
            },
            "summary": {
                "total": len(suggestions),
                "applied": sum(1 for r in results if r.get("status") == "success"),
            },
        }

    except Exception as e:
        logger.error("Code optimization failed: %s", e, exc_info=True)
        return {"status": "error", "error": str(e)}
