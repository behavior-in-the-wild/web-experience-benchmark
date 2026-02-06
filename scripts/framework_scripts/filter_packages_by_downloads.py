#!/usr/bin/env python3
"""
Filter final_packages.jsonl to keep only packages with weekly downloads
in the range (>2500, <25000). Reads package_weekly_downloads.txt for
download counts and outputs a new JSONL with filtered packages per row.
"""

import json
import argparse
from pathlib import Path


def load_allowed_packages(downloads_path: Path, min_downloads: int, max_downloads: int) -> set[str]:
    """Load package names that have downloads in (min_downloads, max_downloads)."""
    allowed = set()
    with open(downloads_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            name, raw_count = parts[0], parts[1]
            try:
                count = int(raw_count.replace(",", ""))
            except ValueError:
                continue
            if min_downloads < count < max_downloads:
                allowed.add(name)
    return allowed


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Filter final_packages.jsonl to only packages in download range (>2500, <25000)."
    )
    parser.add_argument(
        "--downloads",
        type=Path,
        default=script_dir / "package_weekly_downloads.txt",
        help="Path to package_weekly_downloads.txt",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=script_dir / "final_packages.jsonl",
        help="Path to input final_packages.jsonl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=script_dir / "final_packages_filtered.jsonl",
        help="Path to output filtered JSONL",
    )
    parser.add_argument(
        "--min",
        type=int,
        default=2500,
        help="Minimum weekly downloads (exclusive)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=25000,
        help="Maximum weekly downloads (exclusive)",
    )
    parser.add_argument(
        "--keep-empty",
        action="store_true",
        help="Keep rows that have zero packages after filtering (default: skip them)",
    )
    args = parser.parse_args()

    allowed = load_allowed_packages(args.downloads, args.min, args.max)
    print(f"Packages in range ({args.min}, {args.max}): {len(allowed)}", file=__import__("sys").stderr)

    kept_rows = 0
    skipped_empty = 0
    with open(args.input, encoding="utf-8") as fin, open(args.output, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            original_packages = record.get("packages") or []
            filtered_packages = [p for p in original_packages if p in allowed]
            record["packages"] = filtered_packages
            record["num_packages"] = len(filtered_packages)
            if not filtered_packages and not args.keep_empty:
                skipped_empty += 1
                continue
            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            kept_rows += 1

    print(f"Output rows: {kept_rows}", file=__import__("sys").stderr)
    if not args.keep_empty:
        print(f"Skipped (empty after filter): {skipped_empty}", file=__import__("sys").stderr)
    print(f"Wrote {args.output}", file=__import__("sys").stderr)


if __name__ == "__main__":
    main()
