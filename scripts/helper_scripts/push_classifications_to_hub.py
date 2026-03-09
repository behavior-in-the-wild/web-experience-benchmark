#!/usr/bin/env python3
"""
push_classifications_to_hub.py

Reads webpage_classifications_vlm.jsonl, extracts the final {url: page_type}
mapping for each repo_id, and pushes a new column 'webpage_classifications'
to the existing HuggingFace dataset.

The new column is a list of dicts: [{"url": "...", "page_type": "..."}, ...]
matching the order of deduped_webpages for that row.

Usage:
    python push_classifications_to_hub.py \
        --input webpage_classifications_vlm.jsonl \
        --dataset behavior-in-the-wild/cwv-bench-v0 \
        --split train

Requires HF_TOKEN env var (or huggingface-cli login) with write access.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--input",
        default="webpage_classifications_vlm.jsonl",
        help="Path to the classifications JSONL file (default: webpage_classifications_vlm.jsonl)",
    )
    p.add_argument(
        "--dataset",
        default="behavior-in-the-wild/cwv-bench-v0",
        help="HuggingFace dataset repo ID",
    )
    p.add_argument(
        "--split",
        default="train",
        help="Dataset split to update (default: train)",
    )
    p.add_argument(
        "--column",
        default="webpage_classifications",
        help="Name of the new column to add (default: webpage_classifications)",
    )
    return p.parse_args()


def load_classifications(jsonl_path: str) -> dict[str, dict[str, str]]:
    """Load JSONL and build a mapping: repo_id -> {url -> page_type}.

    Each row in the JSONL looks like:
        {
            "index": 0,
            "repo_id": "some/repo",
            "webpage_types": [{"url": "...", "page_type": "..."}, ...]
        }
    """
    path = Path(jsonl_path)
    if not path.exists():
        logger.error("Input file not found: %s", jsonl_path)
        sys.exit(1)

    repo_map: dict[str, dict[str, str]] = {}
    with open(path, "r") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Skipping malformed JSON on line %d: %s", lineno, e)
                continue

            repo_id = row.get("repo_id") or row.get("REPO_ID") or ""
            if not repo_id:
                logger.warning("Line %d has no repo_id, skipping", lineno)
                continue

            url_to_type: dict[str, str] = {}
            for entry in row.get("webpage_types", []):
                url = entry.get("url", "")
                page_type = entry.get("page_type", "other")
                if url:
                    url_to_type[url] = page_type

            repo_map[repo_id] = url_to_type

    logger.info("Loaded classifications for %d repos from %s", len(repo_map), jsonl_path)
    return repo_map


def build_classification_column(
    ds,
    repo_map: dict[str, dict[str, str]],
) -> list[list[dict]]:
    """Build the new column value for every row in the dataset.

    For each row, returns a list of {"url": ..., "page_type": ...} dicts
    ordered the same way as deduped_webpages (falling back to 'other' for
    any URL not found in the classification data).
    """
    column = []
    missing_repos = 0

    for row in ds:
        repo_id = row.get("REPO_ID") or row.get("repo_id") or ""
        deduped = row.get("deduped_webpages") or []

        url_to_type = repo_map.get(repo_id)
        if url_to_type is None:
            missing_repos += 1
            # No classification data at all — mark everything as unknown
            column.append([{"url": u, "page_type": "unknown"} for u in deduped])
        else:
            classification = [
                {"url": u, "page_type": url_to_type.get(u, "other")}
                for u in deduped
            ]
            column.append(classification)

    if missing_repos:
        logger.warning(
            "%d rows had no matching classification data (marked 'unknown')",
            missing_repos,
        )
    return column


def main() -> None:
    args = parse_args()

    # Check HF token
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        logger.warning(
            "HF_TOKEN environment variable not set. Assuming huggingface-cli login is active."
        )

    # Import here so the script gives a clean error without a stack trace
    try:
        from datasets import load_dataset
    except ImportError:
        logger.error("'datasets' package not found. Install: pip install datasets")
        sys.exit(1)

    # 1. Load classification data
    repo_map = load_classifications(args.input)

    # 2. Load the dataset
    logger.info("Loading dataset %s (split=%s)...", args.dataset, args.split)
    ds = load_dataset(args.dataset, split=args.split)
    logger.info("Dataset loaded: %d rows, columns: %s", len(ds), ds.column_names)

    # 3. Build the new column
    logger.info("Building '%s' column...", args.column)
    new_column = build_classification_column(ds, repo_map)

    # 4. Add column to dataset
    ds = ds.add_column(args.column, new_column)
    logger.info(
        "Column '%s' added. Updated columns: %s", args.column, ds.column_names
    )

    # Quick sanity check
    sample = ds[0][args.column]
    logger.info(
        "Sample (row 0, first 3 entries): %s",
        json.dumps(sample[:3], indent=2),
    )

    # 5. Push to hub
    logger.info("Pushing to hub: %s (split=%s)...", args.dataset, args.split)
    ds.push_to_hub("Ayush-Singh/cwv-bench-v2", split=args.split, token=hf_token)
    logger.info("Done! Dataset updated on the Hub.")


if __name__ == "__main__":
    main()
