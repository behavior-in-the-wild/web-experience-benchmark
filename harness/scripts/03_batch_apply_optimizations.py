#!/usr/bin/env python3
"""
Harness batch runner for CWV optimization Stage 2.

Maps suggestion files from harness/out/suggestions/{timestamp}/ to their
corresponding dump workspaces, applies code optimizations with the chosen
coding agent, and generates git patches.

Usage:
    python3 harness/scripts/03_batch_apply_optimizations.py \\
        --suggestions-dir harness/out/suggestions/20260320_163830 \\
        --output-dir harness/out/patches/20260320_163830 \\
        --agent claude \\
        --parallel 2
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import argparse

# Allow direct imports from src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def find_latest_dump(dumps_dir: Path, repo_slug: str) -> Path | None:
    """Return the latest dump directory that has a codebase/ subdirectory."""
    pattern = f"{repo_slug}hub.io_*"
    candidates = sorted(
        [d for d in dumps_dir.glob(pattern) if d.is_dir() and (d / "codebase").exists()]
    )
    return candidates[-1] if candidates else None


async def process_one(
    repo_slug: str,
    suggestion_file: Path,
    workspace_dir: Path,
    repo_output_dir: Path,
    agent: str,
    model: str,
    force: bool,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        checkpoint = repo_output_dir / "patches_applied.json"
        if checkpoint.exists() and not force:
            print(f"  [{repo_slug}] Skipping — already done (use --force to re-run).")
            return {"repo": repo_slug, "status": "skipped"}

        try:
            from cwv_optimizer.services.code_optimizer import apply_code_optimizations
            from cwv_optimizer.services.archival import generate_patches
        except ImportError as exc:
            print(f"  [{repo_slug}] Import error: {exc}")
            return {"repo": repo_slug, "status": "error", "error": str(exc)}

        print(f"  [{repo_slug}] Applying optimizations (agent={agent})…")
        result = await apply_code_optimizations(
            suggestions_path=str(suggestion_file.absolute()),
            workspace_dir=str(workspace_dir.absolute()),
            agent=agent,
            model=model,
        )

        if result.get("status") != "success":
            err = result.get("error", "unknown error")
            print(f"  [{repo_slug}] ERROR applying optimizations: {err}")
            return {"repo": repo_slug, "status": "error", "error": err}

        print(f"  [{repo_slug}] Generating patches…")
        patch_result = generate_patches(
            codebase_dir=str(workspace_dir.absolute()),
            output_dir=str(repo_output_dir.absolute()),
        )

        if patch_result.get("status") == "success":
            num_patches = len(patch_result.get("patches", []))
            print(f"  [{repo_slug}] Done — {num_patches} patch(es) in {repo_output_dir}/patches/")
            checkpoint.write_text(
                json.dumps(
                    {"status": "success", "patches": num_patches, "agent": agent},
                    indent=2,
                )
            )
            return {"repo": repo_slug, "status": "success", "patches": num_patches}
        else:
            err = patch_result.get("error", "unknown error")
            print(f"  [{repo_slug}] Patch generation error: {err}")
            return {"repo": repo_slug, "status": "error", "error": err}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch apply CWV optimizations — harness stage 2.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--suggestions-dir",
        required=True,
        help="Path to suggestions directory (e.g. harness/out/suggestions/20260320_163830)",
    )
    parser.add_argument(
        "--dumps-dir",
        default="dumps",
        help="Path to the dumps directory (default: dumps)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory for patches. Defaults to harness/out/patches/{timestamp}",
    )
    parser.add_argument(
        "--agent",
        default="claude",
        choices=["claude", "codex", "opencode", "aider"],
        help="Coding agent to use (default: claude)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="LLM model (used by opencode/aider; ignored for claude)",
    )
    parser.add_argument(
        "--parallel", "-j",
        type=int,
        default=2,
        help="Number of concurrent repos to process (default: 2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-apply even if patches_applied.json checkpoint exists",
    )
    parser.add_argument(
        "--include",
        default=None,
        help="Only process repos whose slug contains this string",
    )

    args = parser.parse_args()

    suggestions_dir = Path(args.suggestions_dir)
    if not suggestions_dir.exists():
        print(f"ERROR: suggestions directory not found: {suggestions_dir}")
        sys.exit(1)

    dumps_dir = Path(args.dumps_dir)
    if not dumps_dir.exists():
        print(f"ERROR: dumps directory not found: {dumps_dir}")
        sys.exit(1)

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path("harness/out/patches") / ts

    output_dir.mkdir(parents=True, exist_ok=True)

    # Discover suggestion files
    suggestion_files = sorted(suggestions_dir.glob("*.github.io_cwv_suggestions_mobile.json"))
    if not suggestion_files:
        print(f"No suggestion files found in {suggestions_dir}")
        sys.exit(1)

    print(f"Found {len(suggestion_files)} suggestion file(s) in {suggestions_dir}")
    print(f"Patches output: {output_dir}")
    print(f"Agent: {args.agent}  |  Parallel: {args.parallel}")
    print()

    # Build task list
    tasks: list[tuple] = []
    skipped_no_dump: list[str] = []

    for sf in suggestion_files:
        # "aamitn.github.io_cwv_suggestions_mobile.json" -> "aamitn"
        repo_slug = sf.name.split(".github.io_")[0]

        if args.include and args.include not in repo_slug:
            continue

        latest_dump = find_latest_dump(dumps_dir, repo_slug)
        if latest_dump is None:
            print(f"  [{repo_slug}] WARNING: no matching dump with codebase found — skipping.")
            skipped_no_dump.append(repo_slug)
            continue

        workspace_dir = latest_dump / "codebase"
        repo_output_dir = output_dir / repo_slug
        repo_output_dir.mkdir(parents=True, exist_ok=True)

        tasks.append((repo_slug, sf, workspace_dir, repo_output_dir))
        print(f"  [{repo_slug}]  dump → {latest_dump.name}")

    print(f"\nWill process {len(tasks)} repo(s). Skipped (no dump): {len(skipped_no_dump)}")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    if not tasks:
        print("Nothing to do.")
        return

    semaphore = asyncio.Semaphore(args.parallel)
    coros = [
        process_one(slug, sf, ws, out, args.agent, args.model, args.force, semaphore)
        for slug, sf, ws, out in tasks
    ]

    results = await asyncio.gather(*coros)

    # Summary
    success = [r for r in results if r["status"] == "success"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors = [r for r in results if r["status"] == "error"]

    print("\n" + "=" * 60)
    print(f"Summary: {len(success)} succeeded, {len(skipped)} skipped, {len(errors)} failed")
    if errors:
        print("Failed repos:")
        for r in errors:
            print(f"  - {r['repo']}: {r.get('error', '?')}")
    print(f"Patches directory: {output_dir}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
