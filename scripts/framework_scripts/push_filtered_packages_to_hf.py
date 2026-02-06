#!/usr/bin/env python3
"""
Push the packages list from final_packages_filtered.jsonl to the Hugging Face
dataset as a new column PACKAGES.

- Match HF rows by REPO_ID (or repo_id) to repo_id in the JSONL.
- Matched rows: PACKAGES = list of package names (from filtered JSONL).
- Unmatched rows: PACKAGES = "n/a".

PACKAGES is stored as a string column (Arrow does not allow mixing list and str):
- Matched: JSON array string, e.g. '["pkg1","pkg2"]' (parse with json.loads to get list).
- Unmatched: literal "n/a".

Only the PACKAGES column is added/updated; other datapoints are unchanged.

Auth: HF_TOKEN / HUGGINGFACE_HUB_TOKEN env vars, or --hf-token.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any

from datasets import load_dataset  # type: ignore[import]


def load_packages_by_repo(jsonl_path: Path) -> dict[str, list[str]]:
    """Load final_packages_filtered.jsonl into repo_id -> packages list."""
    mapping: dict[str, list[str]] = {}
    if not jsonl_path.exists():
        raise FileNotFoundError(f"Packages file not found: {jsonl_path}")
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rid = rec.get("repo_id")
            if rid is None or rid == "":
                continue
            packages = rec.get("packages")
            mapping[str(rid).strip()] = (
                packages if isinstance(packages, list) else []
            )
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push PACKAGES column from final_packages_filtered.jsonl to HF (match by REPO_ID)."
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="Path to final_packages_filtered.jsonl (default: same dir as script).",
    )
    parser.add_argument(
        "--dataset-name",
        default="behavior-in-the-wild/cwv-bench-v0",
        help="HF dataset repo id.",
    )
    parser.add_argument(
        "--split",
        default="train",
        help="Split name to update.",
    )
    parser.add_argument(
        "--hf-token",
        default=None,
        help="Hugging Face token (else HF_TOKEN / HUGGINGFACE_HUB_TOKEN).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not push to hub; print summary only.",
    )
    args = parser.parse_args()

    if args.hf_token:
        os.environ["HF_TOKEN"] = args.hf_token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = args.hf_token

    script_dir = Path(__file__).resolve().parent
    jsonl_path = args.jsonl or (script_dir / "final_packages_filtered.jsonl")
    jsonl_path = jsonl_path.resolve()

    print(f"[config] JSONL: {jsonl_path}")
    print(f"[config] HF dataset: {args.dataset_name}:{args.split}")

    # 1) Load packages by repo_id from filtered JSONL
    print("[1/3] Loading packages from final_packages_filtered.jsonl...")
    packages_by_repo = load_packages_by_repo(jsonl_path)
    print(f"[1/3] Loaded {len(packages_by_repo)} repo_id -> PACKAGES records")

    # 2) Load HF dataset
    print("[2/3] Loading HF dataset split...")
    ds = load_dataset(args.dataset_name, split=args.split)
    n = len(ds)
    print(f"[2/3] Loaded {n} examples")

    def get_repo_id(example: dict[str, Any]) -> str:
        rid = (
            example.get("REPO_ID")
            or example.get("repo_id")
            or example.get("github_repo")
        )
        return str(rid).strip() if rid is not None else ""

    def add_packages_column(example: dict[str, Any]) -> dict[str, Any]:
        rid = get_repo_id(example)
        if rid in packages_by_repo:
            # Store as JSON string so column type is uniform (Arrow rejects list|str mix)
            example["PACKAGES"] = json.dumps(packages_by_repo[rid])
        else:
            example["PACKAGES"] = "n/a"
        return example

    # Remove existing PACKAGES column if present so we can add it cleanly
    if "PACKAGES" in ds.column_names:
        ds = ds.remove_columns(["PACKAGES"])
    print("[2/3] Applying PACKAGES column (matched = JSON list string, unmatched = 'n/a')...")
    ds = ds.map(
        add_packages_column,
        desc="Adding PACKAGES column",
        num_proc=1,
        load_from_cache_file=False,
        keep_in_memory=True,
    )

    matched = sum(
        1
        for i in range(n)
        if get_repo_id(ds[i]) in packages_by_repo
    )
    print(f"[summary] PACKAGES set for {matched}/{n} rows (matched); rest = 'n/a'")

    if args.dry_run:
        print("[3/3] Dry run enabled; not pushing to hub.")
        return

    print("[3/3] Pushing updated split to hub...")
    ds.push_to_hub(args.dataset_name, split=args.split)
    print("[3/3] Push completed.")


if __name__ == "__main__":
    main()
