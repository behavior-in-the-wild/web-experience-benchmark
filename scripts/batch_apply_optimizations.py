#!/usr/bin/env python3
"""
Script to batch apply Core Web Vitals optimizations for repositories in the dumps/ folder.
This script can either run the full 'optimize' CLI pipeline (including verification)
or directly call services to apply code changes and generate git patches.
Supports parallel execution to speed up processing.
"""

import sys
import os
import json
import asyncio
import argparse
import subprocess
from pathlib import Path
from typing import Optional, List

# Setup path to include src
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT / "src"))

async def run_patches_only(
    suggestions_path: Path,
    workspace_dir: Path,
    agent: str,
    model: str,
    output_dir: Optional[Path] = None,
):
    """Apply code changes and generate patches without verification.
    
    Calls services directly instead of using the LangGraph CLI.
    """
    try:
        from cwv_optimizer.services.code_optimizer import apply_code_optimizations
        from cwv_optimizer.services.archival import generate_patches
    except ImportError as e:
        print(f"  [{workspace_dir.parent.name}] Error: Could not import cwv_optimizer services. Ensure PYTHONPATH includes 'src'. Details: {e}")
        return False

    # 1. Apply code optimizations
    print(f"  [{workspace_dir.parent.name}] Applying code optimizations using {agent}...")
    result = await apply_code_optimizations(
        suggestions_path=str(suggestions_path.absolute()),
        workspace_dir=str(workspace_dir.absolute()),
        agent=agent,
        model=model,
    )

    if result.get("status") != "success":
        print(f"  [{workspace_dir.parent.name}] Error applying optimizations: {result.get('error')}")
        return False

    # 2. Generate patches
    # If output_dir is provided, we use it. Otherwise patches go to dump_dir/patches/
    patch_output_base = output_dir if output_dir else workspace_dir.parent
    print(f"  [{workspace_dir.parent.name}] Generating patches in {patch_output_base}/patches...")
    
    # generate_patches is synchronous
    patch_result = generate_patches(
        codebase_dir=str(workspace_dir.absolute()),
        output_dir=str(patch_output_base.absolute()),
    )

    if patch_result.get("status") == "success":
        num_patches = len(patch_result.get("patches", []))
        print(f"  [{workspace_dir.parent.name}] Successfully generated {num_patches} patches.")
        return True
    else:
        print(f"  [{workspace_dir.parent.name}] Error generating patches: {patch_result.get('error')}")
        return False

async def run_cli_optimize(
    suggestions_path: Path,
    workspace_dir: Path,
    url: str,
    agent: str,
    model: str,
    venv_python: str,
    dry_run: bool = False
):
    """Run the full optimization-only LangGraph via CLI."""
    cmd = [
        venv_python, "-m", "cwv_optimizer.cli.main", "optimize",
        "--url", url,
        "--parsed-suggestions", str(suggestions_path.absolute()),
        "--workspace-dir", str(workspace_dir.absolute()),
        "--coding-agent-provider", agent,
        "--model", model,
        "--verbose"
    ]
    
    if dry_run:
        print(f"  [{workspace_dir.parent.name}] [DRY RUN] Command: {' '.join(cmd)}")
        return True

    print(f"  [{workspace_dir.parent.name}] Running optimization via CLI...")
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    
    # Run CLI in a separate thread/process to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    try:
        def _run():
            return subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                stdin=subprocess.DEVNULL
            )
        
        result = await loop.run_in_executor(None, _run)
        
        if result.returncode == 0:
            print(f"  [{workspace_dir.parent.name}] Successfully processed with CLI")
            return True
        else:
            print(f"  [{workspace_dir.parent.name}] Failed with CLI (exit code {result.returncode}):")
            print(result.stderr)
            return False
    except Exception as e:
        print(f"  [{workspace_dir.parent.name}] Exception running CLI: {e}")
        return False

async def process_repository(
    run_dir: Path,
    args: argparse.Namespace,
    patches_only: bool,
    semaphore: asyncio.Semaphore
):
    """Process a single repository with semaphore protection."""
    async with semaphore:
        suggestions_path = run_dir / "results" / "cwv_suggestions_mobile.json"
        results_path = run_dir / "results" / "application_results.json"
        workspace_dir = run_dir / "codebase"

        if not suggestions_path.exists():
            return

        if results_path.exists() and not args.force:
            print(f"Skipping '{run_dir.name}' (results already exist).")
            return

        if not workspace_dir.exists():
            print(f"Skipping '{run_dir.name}' (workspace_dir 'codebase' not found).")
            return

        print(f"\nProcessing '{run_dir.name}'...")

        if patches_only:
            if not args.dry_run:
                specific_output_dir = None
                if args.output_dir:
                    specific_output_dir = Path(args.output_dir) / run_dir.name
                    specific_output_dir.mkdir(parents=True, exist_ok=True)
                
                await run_patches_only(
                    suggestions_path=suggestions_path,
                    workspace_dir=workspace_dir,
                    agent=args.agent,
                    model=args.model,
                    output_dir=specific_output_dir
                )
            else:
                print(f"  [{run_dir.name}] [DRY RUN] [PATCHES-ONLY] Would apply code changes and generate patches.")
                if args.output_dir:
                    print(f"  [{run_dir.name}] Target: {args.output_dir}/{run_dir.name}/patches/")
        else:
            try:
                with open(suggestions_path, "r") as f:
                    url = json.load(f).get("url", "http://placeholder.url")
            except:
                url = "http://placeholder.url"
            
            await run_cli_optimize(
                suggestions_path=suggestions_path,
                workspace_dir=workspace_dir,
                url=url,
                agent=args.agent,
                model=args.model,
                venv_python=args.venv_python,
                dry_run=args.dry_run
            )

async def main():
    parser = argparse.ArgumentParser(description="Batch apply CWV optimizations.")
    parser.add_argument("--dumps-dir", default="dumps", help="Path to the dumps directory")
    parser.add_argument("--dry-run", action="store_true", help="Print info without executing")
    parser.add_argument("--force", action="store_true", help="Re-run even if results exist")
    parser.add_argument("--include", help="Only process directories containing this string")
    parser.add_argument("--agent", default="opencode", help="Coding agent to use")
    parser.add_argument("--model", default="azure/gpt-5.1-codex", help="Model to use")
    parser.add_argument("--patches-only", action="store_true", help="Skip verification and only generate patches")
    parser.add_argument("--output-dir", help="Central directory for all generated patches (implies --patches-only)")
    parser.add_argument("--parallel", "-j", type=int, default=1, help="Number of concurrent optimizations")
    parser.add_argument("--venv-python", default="./.venv/bin/python3", help="Path to venv python")

    args = parser.parse_args()
    
    dumps_path = Path(args.dumps_dir)
    if not dumps_path.exists():
        print(f"Error: Dumps directory '{args.dumps_dir}' does not exist.")
        return

    patches_only = args.patches_only or args.output_dir is not None

    # Find all possible run directories
    run_dirs = [d for d in dumps_path.iterdir() if d.is_dir() and "hub.io_" in d.name]
    if args.include:
        run_dirs = [d for d in run_dirs if args.include in d.name]

    print(f"Found {len(run_dirs)} potential run directories.")
    print(f"Parallelism: {args.parallel}")

    semaphore = asyncio.Semaphore(args.parallel)
    tasks = []
    
    for run_dir in sorted(run_dirs):
        tasks.append(process_repository(run_dir, args, patches_only, semaphore))
    
    if tasks:
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(1)
