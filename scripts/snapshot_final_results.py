#!/usr/bin/env python3
"""
snapshot_final_results.py — Copy only the 4 key evaluation JSON files
for every completed job across all model run directories into a single
lightweight final_results/ snapshot.

Files captured per job:
  visual.json         — regression verdict (is_valid, overall_regression, checks)
  mobile.json         — CWV mobile metrics
  desktop.json        — CWV desktop metrics
  baseline_meta.json  — commit actually used as baseline

Directory layout mirrored:
  final_results/
    oss_model_runs/<model>/results/<job_label>/
    oss_scale_eval_run/<model>/results/<job_label>/
    closed_model_runs/<model>/results/<job_label>/

Usage:
    python3 scripts/snapshot_final_results.py
    python3 scripts/snapshot_final_results.py --out /path/to/final_results
    python3 scripts/snapshot_final_results.py --dry-run    # show what would be copied
"""

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (source_dir_name, [model_names])
MODEL_GROUPS = [
    (
        "oss_model_runs",
        ["gemma-4-31b-it", "glm-4.7-flash", "qwen3-coder-next",
         "devstral-2-123b", "minimax-m2.7"],
    ),
    (
        "oss_scale_eval_run",
        ["qwen3.5-9b", "qwen3.5-27b", "qwen3.5-35b-a3b",
         "qwen3.5-122b-a10b", "qwen3.5-397b-a17b"],
    ),
    (
        "closed_model_runs",
        ["gemini-2-5-flash", "gemini-2-5-pro"],
    ),
]

KEEP_FILES = ["visual.json", "mobile.json", "desktop.json", "baseline_meta.json"]


def snapshot(out_root: Path, dry_run: bool = False) -> None:
    total_jobs = 0
    total_files = 0
    missing_summary: dict[str, list[str]] = {}   # file → list of jobs missing it

    for group_dir, models in MODEL_GROUPS:
        for model in models:
            results_src = ROOT / group_dir / model / "results"
            if not results_src.exists():
                print(f"  [skip] {group_dir}/{model}: no results dir")
                continue

            job_dirs = sorted(p for p in results_src.iterdir() if p.is_dir())
            copied_jobs = 0

            for job_dir in job_dirs:
                has_any = any((job_dir / f).exists() for f in KEEP_FILES)
                if not has_any:
                    continue  # agent never ran / no output at all

                dest_job = out_root / group_dir / model / "results" / job_dir.name
                if not dry_run:
                    dest_job.mkdir(parents=True, exist_ok=True)

                for fname in KEEP_FILES:
                    src = job_dir / fname
                    if src.exists():
                        if not dry_run:
                            shutil.copy2(src, dest_job / fname)
                        total_files += 1
                    else:
                        missing_summary.setdefault(fname, []).append(
                            f"{group_dir}/{model}/{job_dir.name}"
                        )

                copied_jobs += 1

            total_jobs += copied_jobs
            print(f"  {'[dry]' if dry_run else '✓'} {group_dir}/{model}: {copied_jobs} jobs")

    print()
    print(f"{'[DRY RUN] Would copy' if dry_run else 'Copied'} {total_files} files across {total_jobs} jobs → {out_root}")

    if missing_summary:
        print()
        print("=== Missing file summary ===")
        for fname, jobs in sorted(missing_summary.items()):
            print(f"  {fname}: {len(jobs)} jobs missing")
            if len(jobs) <= 10:
                for j in jobs:
                    print(f"    - {j}")
            else:
                for j in jobs[:5]:
                    print(f"    - {j}")
                print(f"    ... (+{len(jobs) - 5} more)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot final eval JSON files.")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "final_results",
                        help="Output directory (default: ./final_results)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be copied without writing files")
    args = parser.parse_args()

    out_root = args.out.resolve()

    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
        print(f"Snapshotting final results → {out_root}")
    else:
        print(f"[DRY RUN] Would write to → {out_root}")

    print()
    snapshot(out_root, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
