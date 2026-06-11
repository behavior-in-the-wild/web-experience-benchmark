#!/usr/bin/env python3
"""
retrofit_visual_json.py — Rewrite overall_regression + is_valid in every
visual.json under final_result_dumps/ using the same ≥2-check agreement
logic that paper_writing/scripts/compute_metrics.py uses.

Old logic (harness): ANY single check True → overall_regression = True
New logic (paper):   ≥2 checks must agree True (or 1 if only 1 valid check ran)

Run:
    python3 scripts/retrofit_visual_json.py [--dry-run]
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUMPS_DIR = ROOT / "final_result_dumps"


def two_tool_regression(checks: dict) -> bool | None:
    """Mirrors compute_regression() in paper_writing/scripts/compute_metrics.py."""
    results = [
        checks.get("structural",     {}).get("regression"),
        checks.get("jaccard_text",   {}).get("regression"),
        checks.get("gpt_visual",     {}).get("regression"),
        checks.get("console_errors", {}).get("regression"),
    ]
    valid  = [r for r in results if r is not None]
    n_valid = len(valid)
    n_true  = sum(valid)

    if n_valid == 0:
        return None          # all checks errored — leave file unchanged
    if n_valid == 1:
        return bool(valid[0])
    return n_true >= 2       # ≥2 checks must agree


def retrofit(dry_run: bool) -> None:
    visual_files = list(DUMPS_DIR.rglob("visual.json"))
    print(f"Found {len(visual_files)} visual.json files under {DUMPS_DIR.name}/\n")

    changed = skipped = errored = 0

    for path in sorted(visual_files):
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            print(f"  [ERROR] {path.relative_to(ROOT)}: {e}")
            errored += 1
            continue

        checks = data.get("checks", {})
        new_reg = two_tool_regression(checks)

        if new_reg is None:
            skipped += 1
            continue

        old_reg = data.get("overall_regression")
        if new_reg == old_reg:
            skipped += 1
            continue

        data["overall_regression"] = new_reg
        data["is_valid"] = not new_reg

        rel = path.relative_to(ROOT)
        print(f"  {'[dry]' if dry_run else 'UPDATED'} {rel}  "
              f"overall_regression: {old_reg} → {new_reg}")

        if not dry_run:
            path.write_text(json.dumps(data, indent=2) + "\n")

        changed += 1

    print(f"\n{'[DRY RUN] Would update' if dry_run else 'Updated'}: {changed} files")
    print(f"Unchanged (already matching): {skipped} files")
    if errored:
        print(f"Errors (skipped):            {errored} files")


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrofit visual.json regression logic.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files")
    args = parser.parse_args()

    if not DUMPS_DIR.exists():
        print(f"ERROR: {DUMPS_DIR} does not exist")
        return

    retrofit(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
