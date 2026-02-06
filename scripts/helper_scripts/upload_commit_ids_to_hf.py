#!/usr/bin/env python3
"""
Upload COMMIT_ID values from a local CSV to the HF dataset
`behavior-in-the-wild/cwv-bench-v0`.

For each example in the HF split:
- match on repo_id == REPO_ID from the CSV
- write the corresponding COMMIT_ID into a new or existing column
- push the updated split back to the hub

Auth:
- Prefer `HF_TOKEN` / `HUGGINGFACE_HUB_TOKEN` env vars
- Or pass `--hf-token` on the command line
"""

import argparse
import csv
import os
from typing import Dict, Optional

from datasets import load_dataset  # type: ignore[import]


def load_commit_map(csv_path: str) -> Dict[str, str]:
    """Load REPO_ID -> COMMIT_ID mapping from a CSV."""
    mapping: Dict[str, str] = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "REPO_ID" not in reader.fieldnames or "COMMIT_ID" not in reader.fieldnames:
            raise SystemExit("CSV must have columns REPO_ID and COMMIT_ID")
        for row in reader:
            rid = (row.get("REPO_ID") or "").strip()
            cid = (row.get("COMMIT_ID") or "").strip()
            if rid and cid:
                mapping[rid] = cid
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload COMMIT_ID values from local CSV to HF dataset behavior-in-the-wild/cwv-bench-v0."
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="Path to local CSV (e.g. cwv-bench-v0.3.csv) containing REPO_ID and COMMIT_ID columns.",
    )
    parser.add_argument(
        "--dataset-name",
        default="behavior-in-the-wild/cwv-bench-v0",
        help="HF dataset repo id (default: behavior-in-the-wild/cwv-bench-v0).",
    )
    parser.add_argument(
        "--split",
        default="train",
        help="Split name to update (default: train).",
    )
    parser.add_argument(
        "--hf-token",
        default=None,
        help="Hugging Face token (otherwise uses HF_TOKEN / HUGGINGFACE_HUB_TOKEN env vars).",
    )
    parser.add_argument(
        "--col-name",
        default="commit_id",
        help="Name of the column to write in the HF dataset (default: commit_id).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="If set, do not push to hub; just print a summary.",
    )
    args = parser.parse_args()

    if args.hf_token:
        # Let datasets / huggingface_hub pick this up
        os.environ["HF_TOKEN"] = args.hf_token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = args.hf_token

    csv_path = os.path.abspath(args.csv)
    print(f"[config] CSV: {csv_path}")
    print(f"[config] HF dataset: {args.dataset_name}:{args.split}")
    print(f"[config] Target column name: {args.col_name}")

    # 1) Load mapping from CSV
    print("[1/3] Loading REPO_ID -> COMMIT_ID mapping from CSV...")
    repo_to_commit = load_commit_map(csv_path)
    print(f"[1/3] Loaded {len(repo_to_commit)} repo->commit mappings")

    # 2) Load HF dataset
    print("[2/3] Loading HF dataset split...")
    ds = load_dataset(args.dataset_name, split=args.split)
    n = len(ds)
    print(f"[2/3] Loaded {n} examples")

    # 3) Map to add/overwrite column
    target_col = args.col_name

    def add_commit(example):
        rid = example.get("repo_id") or example.get("REPO_ID") or example.get("github_repo")
        rid = str(rid) if rid is not None else ""
        example[target_col] = repo_to_commit.get(rid, None)
        return example

    print("[2/3] Applying commit id mapping to dataset...")
    # Use single-process, in-memory mapping to reduce temporary disk usage.
    ds = ds.map(
        add_commit,
        desc="Updating commit_id column",
        num_proc=1,
        load_from_cache_file=False,
        keep_in_memory=True,
    )

    # Summary
    filled = sum(1 for ex in ds if ex.get(target_col))
    print(f"[summary] {filled}/{n} examples now have non-empty {target_col}")

    if args.dry_run:
        print("[3/3] Dry run enabled; not pushing to hub.")
        return

    # 3) Push to hub
    print("[3/3] Pushing updated split to hub...")
    ds.push_to_hub(
        args.dataset_name,
        split=args.split,
    )
    print("[3/3] Push completed.")


if __name__ == "__main__":
    main()