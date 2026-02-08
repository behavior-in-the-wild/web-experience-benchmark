#!/usr/bin/env python3
"""
Pick the first 3 samples from each tech stack in repos.csv and write to final.csv.
If a tech stack has fewer than 3 samples, all samples are included.
"""

import csv
import sys
from pathlib import Path

# Allow large CSV fields (repos.csv has big JSON columns)
csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_CSV = SCRIPT_DIR / "repos.csv"
OUTPUT_CSV = SCRIPT_DIR / "final.csv"
SAMPLES_PER_STACK = 3


def main():
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    # Group rows by framework (tech stack)
    by_framework: dict[str, list[dict]] = {}
    for row in rows:
        framework = row.get("FRAMEWORK", "").strip()
        if not framework:
            framework = "(unknown)"
        if framework not in by_framework:
            by_framework[framework] = []
        by_framework[framework].append(row)

    # Take the first SAMPLES_PER_STACK from each tech stack
    sampled = []
    for framework, items in sorted(by_framework.items()):
        n = min(SAMPLES_PER_STACK, len(items))
        sampled.extend(items[:n])

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sampled)

    print(f"Wrote {len(sampled)} samples from {len(by_framework)} tech stacks to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
