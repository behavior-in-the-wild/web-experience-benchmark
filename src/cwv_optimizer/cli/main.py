#!/usr/bin/env python3
"""CLI entrypoint for CWV Optimizer."""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

from datasets import load_dataset

import ast
import csv
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cwv_optimizer import __version__
from cwv_optimizer.config import get_settings
from cwv_optimizer.core.logger import setup_logging, get_logger
from cwv_optimizer.langgraph_app.executor import (
    run_full_pipeline,
    run_framework_pipeline,
    run_optimization_workflow,
    run_suggestions_pipeline,
    run_workflow_stream,
)

app = typer.Typer(
    name="cwv-optimizer",
    help="Core Web Vitals Optimization Pipeline",
    add_completion=False,
)
console = Console()
logger = get_logger(__name__)

# Fixed HuggingFace dataset for CWV benchmarking
HF_DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
_hf_dataset_cache = None


def _get_hf_dataset():
    """Load and cache the HuggingFace dataset."""
    global _hf_dataset_cache
    if _hf_dataset_cache is None:
        console.print(f"[dim]Loading dataset: {HF_DATASET_NAME}...[/dim]")
        _hf_dataset_cache = load_dataset(HF_DATASET_NAME, split="train")
        console.print(f"[green]✓ Loaded {len(_hf_dataset_cache)} entries[/green]")
    return _hf_dataset_cache


def _load_hf_entry(index: int = 0) -> Optional[dict]:
    """Load a single entry from the HuggingFace dataset."""
    dataset = _get_hf_dataset()
    if index < 0 or index >= len(dataset):
        console.print(f"[red]Index {index} out of range (0-{len(dataset)-1})[/red]")
        return None
    return dataset[index]


def _load_all_hf_entries() -> list:
    """Load all entries from the HuggingFace dataset."""
    dataset = _get_hf_dataset()
    return [dataset[i] for i in range(len(dataset))]

def _load_jsonl_dataset(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        console.print(f"[red]Dataset not found: {path}[/red]")
        return []
    with p.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_csv_dataset(path: str) -> list[dict]:
    """Load a CSV dataset and normalize entries to match pipeline expectations."""
    p = Path(path)
    if not p.exists():
        console.print(f"[red]Dataset not found: {path}[/red]")
        return []
    csv.field_size_limit(10 ** 7)
    entries = []
    with p.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entry = dict(row)
            # Normalize keys to lowercase for compatibility
            entry["repo_id"] = entry.get("REPO_ID", entry.get("repo_id", ""))
            entry["framework"] = entry.get("FRAMEWORK", entry.get("framework", ""))
            # Construct repo_url from REPO_ID
            if entry["repo_id"] and "repo_url" not in entry:
                entry["repo_url"] = f"https://github.com/{entry['repo_id']}"
            # Extract repo_name from repo_id
            if entry["repo_id"]:
                entry["repo_name"] = entry["repo_id"].split("/")[-1]
            # Parse IS_LIVE from string to dict
            is_live_raw = entry.get("IS_LIVE", entry.get("is_live", ""))
            if isinstance(is_live_raw, str) and is_live_raw.strip():
                try:
                    entry["is_live"] = ast.literal_eval(is_live_raw)
                except (ValueError, SyntaxError):
                    entry["is_live"] = None
            # Extract checked_url from IS_LIVE dict
            is_live = entry.get("is_live")
            if isinstance(is_live, dict) and is_live.get("CHECKED_URL"):
                entry["checked_url"] = is_live["CHECKED_URL"]
            entries.append(entry)
    return entries


def _load_dataset(path: str) -> list[dict]:
    """Auto-detect dataset format (CSV or JSONL) and load entries."""
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return _load_csv_dataset(path)
    else:
        return _load_jsonl_dataset(path)


def _load_dataset_entry(path: str, index: int) -> Optional[dict]:
    """Load a single entry from a dataset file (CSV or JSONL)."""
    entries = _load_dataset(path)
    if index < 0 or index >= len(entries):
        console.print(f"[red]Index {index} out of range (0-{len(entries)-1})[/red]")
        return None
    return entries[index]


def _is_repo_already_processed(repo_name: str, settings) -> bool:
    """
    Check if a repo was already processed by looking for existing runs in dumps/.
    Returns True if a folder starting with the repo_name exists.
    """
    dumps_dir = settings.dumps_dir
    if not dumps_dir.exists():
        return False
    
    # Normalize repo name (same logic as clone_repo.py)
    normalized_name = repo_name.replace('.', '').replace('/', '')
    
    # Look for any folder that starts with the normalized repo name
    for folder in dumps_dir.iterdir():
        if folder.is_dir() and folder.name.startswith(normalized_name):
            return True
    return False


@app.command()
def full(
    github_url: Optional[str] = typer.Option(
        None, "--github-url", "-g", help="GitHub repository URL to clone"
    ),
    hf_index: int = typer.Option(
        0, "--hf-index", "-i", help="Index of entry in HuggingFace dataset"
    ),
    use_hf: bool = typer.Option(
        False, "--use-hf", help="Use HuggingFace dataset (behavior-in-the-wild/cwv-bench-v0)"
    ),
    revision: Optional[str] = typer.Option(
        None, "--revision", "-r", help="Git revision/commit to checkout"
    ),
    device: str = typer.Option("mobile", "--device", help="Device type (mobile/desktop)"),
    agent_model: str = typer.Option("azure/gpt-5", "--model", "-m", help="LLM model for code changes"),
    cwv_model: str = typer.Option("gpt-4.1", "--cwv-model", help="LLM model for CWV analysis (cwv-agent)"),
    coding_agent_provider: str = typer.Option("aider", "--coding-agent-provider", help="Coding agent provider (claude/codex/aider/opencode)"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run headlessly"),
    num_runs: int = typer.Option(3, "--num-runs", "-n", help="Number of test runs"),
    checkpoint: bool = typer.Option(False, "--checkpoint", "-c", help="Enable checkpointing"),
    stream: bool = typer.Option(False, "--stream", "-s", help="Stream output"),
    tunnel_provider: str = typer.Option(
        "auto", "--tunnel-provider",
        help="Tunnel provider for PSI: auto, ngrok, cloudflare, bore (default: auto)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Run full pipeline: clone -> deploy -> analyze -> optimize -> test."""
    setup_logging(level="DEBUG" if verbose else "INFO")

    if not github_url and not use_hf:
        console.print("[red]Either --github-url or --use-hf is required[/red]")
        raise typer.Exit(1)

    # Build config
    settings = get_settings()
    config = {
        "device": device,
        "model": agent_model,
        "cwv_model": cwv_model,
        "agent": coding_agent_provider,
        "headless": headless,
        "num_runs": num_runs,
        "use_architect": True,
        "apply_count": 1,
        "apply_mode": "individual",
        "tunnel_provider": tunnel_provider,
        "temperature": settings.temperature,
        "content_similarity_threshold": settings.content_similarity_threshold,
        "enable_content_similarity": settings.enable_content_similarity,
        "run_visual_regression_tests": settings.run_visual_regression_tests,
        "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix,
        "errors": [],
        "step_timings": {},
    }

    if github_url:
        config["github_url"] = github_url
        if revision:
            config["revision_id"] = revision
    elif use_hf:
        entry = _load_hf_entry(hf_index)
        if not entry:
            raise typer.Exit(1)
        # Construct github_url from REPO_ID (uppercase) or repo_id (lowercase fallback)
        repo_id = entry.get("REPO_ID") or entry.get("repo_id")
        if entry.get("repo_url"):
            config["github_url"] = entry["repo_url"]
        elif repo_id:
            config["github_url"] = f"https://github.com/{repo_id}"
        else:
            console.print("[red]Dataset entry missing 'repo_url' or 'REPO_ID'[/red]")
            raise typer.Exit(1)
        config["repo_name"] = entry.get("repo_name") or repo_id.split("/")[-1] if repo_id else ""
        # Set checked_url for CrUX/PSI field data
        if entry.get("checked_url"):
            config["checked_url"] = entry["checked_url"]
            console.print(f"[dim]Field URL for CrUX/PSI: {entry['checked_url']}[/dim]")
        
        # Check IS_LIVE metadata if available
        is_live = entry.get("IS_LIVE") or entry.get("is_live")
        if isinstance(is_live, dict):
            if is_live.get("LIVE"):
                if is_live.get("CHECKED_URL"):
                    config["checked_url"] = is_live["CHECKED_URL"]
                    console.print(f"[dim]Field URL for CrUX/PSI (from IS_LIVE): {config['checked_url']}[/dim]")
                if is_live.get("REPO_URL"):
                    # Optionally override github_url if preferred, but usually REPO_ID construct is fine
                    # config["github_url"] = is_live["REPO_URL"]
                    pass
        elif hasattr(is_live, "get") and is_live.get("LIVE"): # Handle if it's an object but not dict-like? (Unlikely with HF dataset)
             pass

    # Display config
    console.print(Panel.fit(
        f"[bold]Full Pipeline[/bold]\n"
        f"GitHub URL: {config.get('github_url')}\n"
        f"Device: {device}\n"
        f"Agent Model: {agent_model}\n"
        f"CWV Model: {cwv_model}\n"
        f"Agent: {coding_agent_provider}\n"
        f"Checkpointing: {'ON' if checkpoint else 'OFF'}",
        title="CWV Optimizer",
    ))

    try:
        if stream:
            result = asyncio.run(run_workflow_stream(
                config, full_pipeline=True, use_checkpointing=checkpoint
            ))
        else:
            result = asyncio.run(run_full_pipeline(
                config, use_checkpointing=checkpoint
            ))

        _display_results(result)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Pipeline failed: {e}[/red]")
        raise typer.Exit(1)


VALID_FRAMEWORKS = [
    "Hexo", "Jekyll", "Static HTML", "Static Html",
    "Hugo", "Vue", "React", "Next", "Flask", "Pelican", "Express", "Quarto"
]


def _run_single_framework_entry(
    entry: dict,
    index: int,
    total: int,
    framework_type: str,
    device: str,
    agent_model: str,
    cwv_model: str,
    coding_agent_provider: str,
    headless: bool,
    num_runs: int,
    checkpoint: bool,
    stream: bool,
    settings,
    tunnel_provider: str = "auto",
) -> dict:
    """Run framework pipeline on a single dataset entry."""
    # Construct github_url from repo_id if repo_url is not present
    if entry.get("repo_url"):
        github_url = entry["repo_url"]
    elif entry.get("repo_id") or entry.get("REPO_ID"):
        repo_id = entry.get("repo_id") or entry.get("REPO_ID")
        github_url = f"https://github.com/{repo_id}"
    else:
        return {"status": "error", "error": "Missing repo_url or repo_id"}
    
    # Use framework from dataset if available, otherwise use CLI arg
    framework = entry.get("framework") or entry.get("FRAMEWORK", framework_type)
    repo_name = entry.get("repo_name") or (entry.get("repo_id") or entry.get("REPO_ID", "")).split("/")[-1]
    
    config = {
        "github_url": github_url,
        "framework": framework,
        "repo_name": repo_name,
        "device": device,
        "model": agent_model,
        "cwv_model": cwv_model,
        "agent": coding_agent_provider,
        "headless": headless,
        "num_runs": num_runs,
        "use_architect": True,
        "apply_count": 1,
        "apply_mode": "individual",
        "tunnel_provider": tunnel_provider,
        "temperature": settings.temperature,
        "content_similarity_threshold": settings.content_similarity_threshold,
        "enable_content_similarity": settings.enable_content_similarity,
        "run_visual_regression_tests": settings.run_visual_regression_tests,
        "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix,
        "errors": [],
        "step_timings": {},
    }
    
    # Set checked_url for CrUX/PSI field data
    if entry.get("checked_url"):
        config["checked_url"] = entry["checked_url"]
        
    # Check IS_LIVE metadata if available
    is_live = entry.get("IS_LIVE") or entry.get("is_live")
    if isinstance(is_live, dict) and is_live.get("LIVE"):
        if is_live.get("CHECKED_URL"):
            config["checked_url"] = is_live["CHECKED_URL"]
    
    console.print(f"\n[bold cyan]{'='*60}[/bold cyan]")
    console.print(f"[bold]Processing [{index+1}/{total}]: {repo_name}[/bold]")
    console.print(f"[dim]Framework: {framework} | URL: {github_url}[/dim]")
    console.print(f"[bold cyan]{'='*60}[/bold cyan]")
    
    try:
        if stream:
            result = asyncio.run(run_workflow_stream(
                config, framework_pipeline=True, use_checkpointing=checkpoint
            ))
        else:
            result = asyncio.run(run_framework_pipeline(
                config, use_checkpointing=checkpoint
            ))
        console.print(f"[green]✓ Completed: {repo_name}[/green]")
        return {"status": "success", "repo": repo_name, "result": result}
    except Exception as e:
        console.print(f"[red]✗ Failed: {repo_name} - {e}[/red]")
        return {"status": "error", "repo": repo_name, "error": str(e)}


@app.command()
def framework(
    github_url: Optional[str] = typer.Option(
        None, "--github-url", "-g", help="GitHub repository URL to clone"
    ),
    framework_type: str = typer.Option(
        "Static HTML", "--framework", "-f", help="Framework type: Hexo, Jekyll, or 'Static HTML'"
    ),
    dataset: Optional[str] = typer.Option(
        None, "--dataset", "-d", help="Path to local dataset (JSONL or CSV)"
    ),
    hf_index: int = typer.Option(
        0, "--hf-index", "-i", help="Index of entry (HF or JSONL)"
    ),
    use_hf: bool = typer.Option(
        False, "--use-hf", help="Use HuggingFace dataset"
    ),
    all_entries: bool = typer.Option(
        False, "--all", "-a", help="Process ALL entries in the dataset"
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Limit number of entries"
    ),
    device: str = typer.Option("mobile", "--device", help="Device type (mobile/desktop)"),
    agent_model: str = typer.Option("azure/gpt-5", "--model", "-m", help="LLM model for code changes (aider/claude)"),
    cwv_model: str = typer.Option("gpt-4.1", "--cwv-model", help="LLM model for CWV analysis (cwv-agent)"),
    coding_agent_provider: str = typer.Option("aider", "--coding-agent-provider", help="Coding agent provider (claude/codex/aider/opencode)"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run headlessly"),
    num_runs: int = typer.Option(3, "--num-runs", "-n", help="Number of test runs"),
    checkpoint: bool = typer.Option(False, "--checkpoint", "-c", help="Enable checkpointing"),
    stream: bool = typer.Option(False, "--stream", "-s", help="Stream output"),
    tunnel_provider: str = typer.Option(
        "auto", "--tunnel-provider",
        help="Tunnel provider for PSI: auto, ngrok, cloudflare, bore (default: auto)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Run framework pipeline: clone -> deploy (deterministic) -> analyze -> optimize.

    Uses pre-detected framework info instead of AI for deployment.
    Supported frameworks: Hexo, Jekyll, Static HTML, Hugo, Vue, React, Next, Flask, Pelican, Express, Quarto
    
    Use --use-hf to load from HuggingFace dataset (behavior-in-the-wild/cwv-bench-v0).
    Use --all to process ALL entries in the dataset.
    """
    setup_logging(level="DEBUG" if verbose else "INFO")

    if not github_url and not use_hf and not dataset:
        console.print("[red]One of --github-url, --use-hf, or --dataset is required[/red]")
        raise typer.Exit(1)

    if framework_type not in VALID_FRAMEWORKS:
        console.print(f"[red]Invalid framework: {framework_type}. Must be one of: {VALID_FRAMEWORKS}[/red]")
        raise typer.Exit(1)

    settings = get_settings()

    # ============================================
    # Process ALL entries in HF dataset
    # ============================================
    if use_hf and all_entries:
        entries = _load_all_hf_entries()
        if not entries:
            console.print("[red]No entries found in HuggingFace dataset[/red]")
            raise typer.Exit(1)
        
        total = len(entries)
        console.print(Panel.fit(
            f"[bold]Framework Pipeline - Batch Mode[/bold]\n"
            f"Dataset: {HF_DATASET_NAME}\n"
            f"Total entries: {total}\n"
            f"Framework: {framework_type}\n"
            f"Device: {device}",
            title="CWV Optimizer",
        ))
        
        results = []
        success_count = 0
        error_count = 0
        
        for idx, entry in enumerate(entries):
            repo_id = entry.get("REPO_ID") or entry.get("repo_id", "")
            repo_name = entry.get("repo_name") or repo_id.split("/")[-1] if repo_id else ""
            
            # Skip if already processed
            if _is_repo_already_processed(repo_name, settings):
                console.print(f"[dim]Skipping [{idx+1}/{total}]: {repo_name} (already processed)[/dim]")
                continue
            
            try:
                result = _run_single_framework_entry(
                    entry=entry,
                    index=idx,
                    total=total,
                    framework_type=framework_type,
                    device=device,
                    agent_model=agent_model,
                    cwv_model=cwv_model,
                    coding_agent_provider=coding_agent_provider,
                    headless=headless,
                    num_runs=num_runs,
                    checkpoint=checkpoint,
                    stream=stream,
                    settings=settings,
                    tunnel_provider=tunnel_provider,
                )
                results.append(result)
                if result.get("status") == "success":
                    success_count += 1
                else:
                    error_count += 1
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted by user[/yellow]")
                break
        
        # Summary
        console.print(f"\n[bold]{'='*60}[/bold]")
        console.print(f"[bold green]Batch Complete![/bold green]")
        console.print(f"  Total: {total}")
        console.print(f"  [green]Success: {success_count}[/green]")
        console.print(f"  [red]Failed: {error_count}[/red]")
        console.print(f"[bold]{'='*60}[/bold]")
        return

    # ============================================
    # Process ALL / LIMITED entries from JSONL
    # ============================================
    if dataset and all_entries:
        entries = _load_dataset(dataset)
        if not entries:
            raise typer.Exit(1)

        if limit is not None:
            entries = entries[:limit]

        total = len(entries)

        console.print(Panel.fit(
            f"[bold]Framework Pipeline - Dataset[/bold]\n"
            f"Dataset: {dataset}\n"
            f"Total entries: {total}\n"
            f"Framework: {framework_type}\n"
            f"Device: {device}",
            title="CWV Optimizer",
        ))

        for idx, entry in enumerate(entries):
            repo_id = entry.get("REPO_ID") or entry.get("repo_id", "")
            repo_name = entry.get("repo_name") or repo_id.split("/")[-1] if repo_id else ""

            if _is_repo_already_processed(repo_name, settings):
                console.print(f"[dim]Skipping [{idx+1}/{total}]: {repo_name} (already processed)[/dim]")
                continue

            _run_single_framework_entry(
                entry=entry,
                index=idx,
                total=total,
                framework_type=framework_type,
                device=device,
                agent_model=agent_model,
                cwv_model=cwv_model,
                coding_agent_provider=coding_agent_provider,
                headless=headless,
                num_runs=num_runs,
                checkpoint=checkpoint,
                stream=stream,
                settings=settings,
                tunnel_provider=tunnel_provider,
            )

        return

    # ============================================
    # Process SINGLE entry (original behavior)
    # ============================================
    config = {
        "device": device,
        "model": agent_model,
        "cwv_model": cwv_model,
        "agent": coding_agent_provider,
        "headless": headless,
        "num_runs": num_runs,
        "use_architect": True,
        "apply_count": 1,
        "apply_mode": "individual",
        "tunnel_provider": tunnel_provider,
        "temperature": settings.temperature,
        "content_similarity_threshold": settings.content_similarity_threshold,
        "enable_content_similarity": settings.enable_content_similarity,
        "run_visual_regression_tests": settings.run_visual_regression_tests,
        "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix,
        "errors": [],
        "step_timings": {},
    }

    if github_url:
        config["github_url"] = github_url
        config["framework"] = framework_type
    elif use_hf:
        entry = _load_hf_entry(hf_index)
        if not entry:
            raise typer.Exit(1)
        # Construct github_url from REPO_ID (uppercase) or repo_id (lowercase fallback)
        repo_id = entry.get("REPO_ID") or entry.get("repo_id")
        if entry.get("repo_url"):
            config["github_url"] = entry["repo_url"]
        elif repo_id:
            config["github_url"] = f"https://github.com/{repo_id}"
        else:
            console.print("[red]Dataset entry missing 'repo_url' or 'REPO_ID'[/red]")
            raise typer.Exit(1)
        # Use framework from dataset if available (uppercase or lowercase), otherwise use CLI arg
        config["framework"] = entry.get("FRAMEWORK") or entry.get("framework", framework_type)
        config["repo_name"] = entry.get("repo_name") or repo_id.split("/")[-1] if repo_id else ""
        # Set checked_url for CrUX/PSI field data
        # Set checked_url for CrUX/PSI field data
        if entry.get("checked_url"):
            config["checked_url"] = entry["checked_url"]
            console.print(f"[dim]Field URL for CrUX/PSI: {entry['checked_url']}[/dim]")
        
        # Check IS_LIVE metadata if available
        is_live = entry.get("IS_LIVE") or entry.get("is_live")
        if isinstance(is_live, dict):
            if is_live.get("LIVE"):
                if is_live.get("CHECKED_URL"):
                    config["checked_url"] = is_live["CHECKED_URL"]
                    console.print(f"[dim]Field URL for CrUX/PSI (from IS_LIVE): {config['checked_url']}[/dim]")
    elif dataset:
        entry = _load_dataset_entry(dataset, hf_index)
        if not entry:
            raise typer.Exit(1)

        config["github_url"] = entry.get("repo_url") or f"https://github.com/{entry['repo_id']}"
        config["framework"] = entry.get("framework", framework_type)
        config["repo_name"] = entry.get("repo_name") or entry.get("repo_id", "").split("/")[-1]


    # Display config
    console.print(Panel.fit(
        f"[bold]Framework Pipeline[/bold]\n"
        f"GitHub URL: {config.get('github_url')}\n"
        f"Framework: {config.get('framework')}\n"
        f"Device: {device}\n"
        f"Agent Model: {agent_model}\n"
        f"CWV Model: {cwv_model}\n"
        f"Agent: {coding_agent_provider}\n"
        f"Checkpointing: {'ON' if checkpoint else 'OFF'}",
        title="CWV Optimizer",
    ))

    try:
        if stream:
            result = asyncio.run(run_workflow_stream(
                config, framework_pipeline=True, use_checkpointing=checkpoint
            ))
        else:
            result = asyncio.run(run_framework_pipeline(
                config, use_checkpointing=checkpoint
            ))

        _display_results(result)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Framework pipeline failed: {e}[/red]")
        raise typer.Exit(1)



def _suggest_worker(args: tuple) -> dict:
    """Module-level worker for ProcessPoolExecutor (must be picklable)."""
    import asyncio as _asyncio
    import shutil as _shutil
    from cwv_optimizer.config import get_settings as _get_settings
    from cwv_optimizer.langgraph_app.executor import run_suggestions_pipeline as _run

    idx, entry, fw_type, dev, model, hl, ckpt, worker_slot, output_dir, tunnel_provider = args

    _settings = _get_settings()
    cfg = _build_suggest_config(entry, fw_type, dev, model, hl, _settings, tunnel_provider)
    cfg["port_start"] = 8000 + worker_slot * 200

    repo_id = entry.get("repo_id") or entry.get("REPO_ID", "")
    repo_name = entry.get("repo_name") or repo_id.split("/")[-1]
    try:
        result = _asyncio.run(_run(cfg, use_checkpointing=ckpt))
        suggestions_src = result.get("parsed_suggestions_path", "")

        # Copy suggestions into the shared batch output directory
        out_path = ""
        if suggestions_src and output_dir:
            dest_dir = Path(output_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(dest_dir / f"{repo_name}_cwv_suggestions_{dev}.json")
            _shutil.copy2(suggestions_src, out_path)

        return {
            "status": "success",
            "repo": repo_name,
            "suggestions_path": suggestions_src,
            "output_path": out_path,
        }
    except Exception as exc:
        return {"status": "error", "repo": repo_name, "error": str(exc)}


def _build_suggest_config(
    entry: dict,
    framework_type: str,
    device: str,
    cwv_model: str,
    headless: bool,
    settings,
    tunnel_provider: str = "auto",
) -> dict:
    """Build a suggestions pipeline config from a dataset entry."""
    repo_id = entry.get("repo_id") or entry.get("REPO_ID", "")
    config = {
        "github_url": entry.get("repo_url") or f"https://github.com/{repo_id}",
        "framework": entry.get("framework") or entry.get("FRAMEWORK", framework_type),
        "repo_name": entry.get("repo_name") or repo_id.split("/")[-1],
        "device": device,
        "cwv_model": cwv_model,
        "headless": headless,
        "tunnel_provider": tunnel_provider,
        "temperature": settings.temperature,
        "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix,
        "errors": [],
        "step_timings": {},
    }
    if entry.get("checked_url"):
        config["checked_url"] = entry["checked_url"]
    is_live = entry.get("IS_LIVE") or entry.get("is_live")
    if isinstance(is_live, dict) and is_live.get("LIVE") and is_live.get("CHECKED_URL"):
        config["checked_url"] = is_live["CHECKED_URL"]
    commit_id = entry.get("COMMIT_ID") or entry.get("commit_id")
    if commit_id:
        config["revision_id"] = commit_id
    return config


@app.command()
def suggest(
    github_url: Optional[str] = typer.Option(
        None, "--github-url", "-g", help="GitHub repository URL to clone"
    ),
    framework_type: str = typer.Option(
        "Static HTML", "--framework", "-f", help="Framework type: Hexo, Jekyll, Hugo, 'Static HTML', Vue, React, Next, etc."
    ),
    dataset: Optional[str] = typer.Option(
        None, "--dataset", "-d", help="Path to local dataset (JSONL or CSV)"
    ),
    hf_index: int = typer.Option(
        0, "--hf-index", "-i", help="Row index within --dataset (single-entry mode)"
    ),
    all_entries: bool = typer.Option(
        False, "--all", "-a", help="Process ALL entries in the dataset"
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", help="Cap the number of entries processed (batch mode)"
    ),
    workers: int = typer.Option(
        1, "--workers", "-w", help="Number of parallel workers for batch mode (each gets its own port range)"
    ),
    device: str = typer.Option("mobile", "--device", help="Device type (mobile/desktop)"),
    cwv_model: str = typer.Option("gpt-4.1", "--cwv-model", help="LLM model for CWV analysis (cwv-agent)"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run headlessly"),
    checkpoint: bool = typer.Option(False, "--checkpoint", "-c", help="Enable checkpointing"),
    batch_ts: Optional[str] = typer.Option(
        None, "--batch-ts", help="Timestamp string (YYYYMMDD_HHMMSS) to use for the output suggestions dir; generated if omitted"
    ),
    tunnel_provider: str = typer.Option(
        "auto",
        "--tunnel-provider",
        help=(
            "Tunnel provider for exposing the local server to Google PSI. "
            "Choices: auto (try ngrok→cloudflare→bore), ngrok, cloudflare, bore. "
            "Default: auto"
        ),
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Clone, deploy, and generate CWV suggestions JSON — without applying any code changes.

    Runs the pipeline up through CWV analysis and writes the structured suggestions
    file to the run's results directory.  The path is printed at the end so it can
    be piped directly into the 'optimize' command via --parsed-suggestions.

    Use --all (with --dataset) to process every row in the CSV/JSONL in sequence,
    skipping repos that already have a dumps/ folder.

    Supported frameworks: Hexo, Jekyll, Static HTML, Hugo, Vue, React, Next, Flask, Pelican, Express, Quarto
    """
    setup_logging(level="DEBUG" if verbose else "INFO")

    if not github_url and not dataset:
        console.print("[red]Either --github-url or --dataset is required[/red]")
        raise typer.Exit(1)

    if framework_type not in VALID_FRAMEWORKS:
        console.print(f"[red]Invalid framework: {framework_type}. Must be one of: {VALID_FRAMEWORKS}[/red]")
        raise typer.Exit(1)

    settings = get_settings()

    # =========================================================
    # Batch mode — sequential or parallel
    # =========================================================
    if dataset and all_entries:
        entries = _load_dataset(dataset)
        if not entries:
            raise typer.Exit(1)
        if limit is not None:
            entries = entries[:limit]

        # Filter already-processed repos up front
        pending = []
        skip_count = 0
        for entry in entries:
            repo_id = entry.get("repo_id") or entry.get("REPO_ID", "")
            repo_name = entry.get("repo_name") or repo_id.split("/")[-1]
            if _is_repo_already_processed(repo_name, settings):
                console.print(f"[dim]Skipping: {repo_name} (already processed)[/dim]")
                skip_count += 1
            else:
                pending.append(entry)

        total = len(pending)
        effective_workers = min(workers, total) if total > 0 else 1

        # Create a single output directory for all suggestions in this batch run
        from datetime import datetime as _dt
        if not batch_ts:
            batch_ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        batch_output_dir = settings.project_root / "harness" / "out" / "suggestions" / batch_ts
        batch_output_dir.mkdir(parents=True, exist_ok=True)

        console.print(Panel.fit(
            f"[bold]Suggestions Pipeline — Batch Mode[/bold]\n"
            f"Dataset:   {dataset}\n"
            f"Pending:   {total}  (skipped {skip_count} already done)\n"
            f"Workers:   {effective_workers}\n"
            f"Device:    {device}\n"
            f"CWV Model: {cwv_model}\n"
            f"Output:    {batch_output_dir}\n"
            f"(No code changes will be applied)",
            title="CWV Optimizer — Suggest",
        ))

        if total == 0:
            console.print("[green]Nothing to do — all entries already processed.[/green]")
            return

        import concurrent.futures

        # Build task list — distribute entries round-robin across worker slots.
        # Each worker slot gets a non-overlapping 200-port window (8000, 8200, …).
        tasks = [
            (idx, entry, framework_type, device, cwv_model, headless, checkpoint,
             idx % effective_workers, str(batch_output_dir), tunnel_provider)
            for idx, entry in enumerate(pending)
        ]

        success_count = 0
        error_count = 0

        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=effective_workers) as pool:
                futures = {pool.submit(_suggest_worker, task): task for task in tasks}
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res["status"] == "success":
                        out = res.get("output_path") or res.get("suggestions_path", "")
                        console.print(f"[green]✓ {res['repo']}[/green] → {out}")
                        success_count += 1
                    else:
                        console.print(f"[red]✗ {res['repo']}: {res.get('error', '')}[/red]")
                        error_count += 1
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted by user[/yellow]")

        console.print(f"\n[bold]{'='*60}[/bold]")
        console.print(f"[bold green]Batch Complete![/bold green]")
        console.print(f"  Total:   {len(entries)}")
        console.print(f"  [green]Success: {success_count}[/green]")
        console.print(f"  [dim]Skipped: {skip_count}[/dim]")
        console.print(f"  [red]Failed:  {error_count}[/red]")
        console.print(f"  [bold]Suggestions dir:[/bold] {batch_output_dir}")
        console.print(f"[bold]{'='*60}[/bold]")
        return

    # =========================================================
    # Single-entry mode
    # =========================================================
    config = {
        "device": device,
        "cwv_model": cwv_model,
        "headless": headless,
        "tunnel_provider": tunnel_provider,
        "temperature": settings.temperature,
        "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix,
        "errors": [],
        "step_timings": {},
    }

    if github_url:
        config["github_url"] = github_url
        config["framework"] = framework_type
    elif dataset:
        entry = _load_dataset_entry(dataset, hf_index)
        if not entry:
            raise typer.Exit(1)
        config = _build_suggest_config(entry, framework_type, device, cwv_model, headless, settings, tunnel_provider)

    console.print(Panel.fit(
        f"[bold]Suggestions Pipeline[/bold]\n"
        f"GitHub URL: {config.get('github_url')}\n"
        f"Framework: {config.get('framework')}\n"
        f"Device: {device}\n"
        f"CWV Model: {cwv_model}\n"
        f"(No code changes will be applied)",
        title="CWV Optimizer — Suggest",
    ))

    try:
        result = asyncio.run(run_suggestions_pipeline(config, use_checkpointing=checkpoint))

        suggestions_path = result.get("parsed_suggestions_path")
        report_path = result.get("cwv_report_path")

        console.print("\n")
        console.print(Panel.fit("[bold green]Suggestions Generated![/bold green]"))
        if suggestions_path:
            console.print(f"[bold]Suggestions JSON:[/bold] {suggestions_path}")
        if report_path:
            console.print(f"[bold]CWV Report:[/bold]      {report_path}")
        if result.get("workspace_dir"):
            console.print(f"[bold]Workspace:[/bold]        {result['workspace_dir']}")
        if result.get("deployed_url") or result.get("url"):
            console.print(f"[bold]Deployed URL:[/bold]     {result.get('deployed_url') or result.get('url')}")

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Suggestions pipeline failed: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def optimize(
    parsed_suggestions: str = typer.Option(
        ..., "--parsed-suggestions", "-p", help="Path to suggestions JSON"
    ),
    url: str = typer.Option(..., "--url", "-u", help="URL of deployed website"),
    workspace_dir: str = typer.Option(
        ..., "--workspace-dir", "-w", help="Path to workspace directory"
    ),
    device: str = typer.Option("mobile", "--device", help="Device type"),
    model: str = typer.Option("azure/gpt-5", "--model", "-m", help="LLM model"),
    coding_agent_provider: str = typer.Option("claude", "--coding-agent-provider", help="Coding agent provider (claude/codex/aider/opencode)"),
    headless: bool = typer.Option(True, "--headless/--no-headless", help="Run headlessly"),
    num_runs: int = typer.Option(3, "--num-runs", "-n", help="Number of test runs"),
    checkpoint: bool = typer.Option(False, "--checkpoint", "-c", help="Enable checkpointing"),
    stream: bool = typer.Option(False, "--stream", "-s", help="Stream output"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Run optimization on existing workspace with parsed suggestions."""
    setup_logging(level="DEBUG" if verbose else "INFO")

    settings = get_settings()
    config = {
        "url": url,
        "workspace_dir": workspace_dir,
        "parsed_suggestions_path": parsed_suggestions,
        "device": device,
        "model": model,
        "agent": coding_agent_provider,
        "headless": headless,
        "num_runs": num_runs,
        "apply_count": 1,
        "apply_mode": "individual",
        "temperature": settings.temperature,
        "content_similarity_threshold": settings.content_similarity_threshold,
        "enable_content_similarity": settings.enable_content_similarity,
        "run_visual_regression_tests": settings.run_visual_regression_tests,
        "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix,
        "errors": [],
        "step_timings": {},
    }

    console.print(Panel.fit(
        f"[bold]Optimization Pipeline[/bold]\n"
        f"URL: {url}\n"
        f"Suggestions: {parsed_suggestions}\n"
        f"Device: {device}\n"
        f"Model: {model}\n"
        f"Agent: {coding_agent_provider}",
        title="CWV Optimizer",
    ))

    try:
        if stream:
            result = asyncio.run(run_workflow_stream(
                config, full_pipeline=False, use_checkpointing=checkpoint
            ))
        else:
            result = asyncio.run(run_optimization_workflow(
                config, use_checkpointing=checkpoint
            ))

        _display_results(result)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Optimization failed: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def version():
    """Show version information."""
    console.print(f"CWV Optimizer v{__version__}")


def _display_results(result: dict):
    """Display workflow results in a nice format."""
    console.print("\n")
    console.print(Panel.fit("[bold green]Workflow Complete![/bold green]"))

    if result.get("learnings_path"):
        console.print(f"📚 Learnings: {result['learnings_path']}")
    if result.get("archive_path"):
        console.print(f"📦 Archive: {result['archive_path']}")

    if result.get("step_timings"):
        table = Table(title="Step Timings")
        table.add_column("Step", style="cyan")
        table.add_column("Duration", style="green")

        for step, duration in result["step_timings"].items():
            table.add_row(step, f"{duration:.2f}s")

        console.print(table)


def main():
    """Main entry point."""
    app()


if __name__ == "__main__":
    main()
