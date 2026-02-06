#!/usr/bin/env python3
"""
Update cwv-bench-v0.2.csv with latest commit IDs for each repo, and optionally
zip each repo at that commit and set ZIP_REPO_PATH.

Usage:
  # Only update COMMIT_ID in the CSV (no zipping)
  python update_csv_commits_and_zips.py --csv /path/to/cwv-bench-v0.2.csv

  # Update COMMIT_ID and zip each repo, then update ZIP_REPO_PATH
  python update_csv_commits_and_zips.py --csv /path/to/cwv-bench-v0.2.csv --zip --zips-dir /path/to/zips
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:
    _HAS_TQDM = False
    def tqdm(it, total=None, desc=None, file=None, **kwargs):
        """Fallback when tqdm not installed: print progress every 50 items."""
        it = iter(it)
        n = 0
        while True:
            try:
                x = next(it)
                n += 1
                if total and n % 50 == 0:
                    print(f"  {desc or 'Progress'}: {n}/{total}", flush=True)
                yield x
            except StopIteration:
                break


# =========================
# CONFIG
# =========================
def get_last_commit_sha(repo_id: str, timeout: int = 15) -> Optional[str]:
    """Get latest commit SHA for repo_id (owner/repo) via git ls-remote."""
    repo_url = f"https://github.com/{repo_id}.git"
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        return result.stdout.split()[0]
    except Exception:
        return None


def sanitize_repo_id_for_path(repo_id: str) -> str:
    """Turn owner/repo into a safe filename (e.g. owner_repo)."""
    return re.sub(r"[^\w\-.]", "_", repo_id)


def zip_repo_at_commit(repo_id: str, commit_sha: str, out_zip_path: str, timeout: int = 180) -> bool:
    """Clone repo at given commit, zip it, and save to out_zip_path."""
    repo_url = f"https://github.com/{repo_id}.git"
    if not commit_sha:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="repo_") as tmpdir:
            clone_dir = os.path.join(tmpdir, "repo")
            # Shallow clone then fetch the specific commit and checkout
            result = subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, clone_dir],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                return False
            # Fetch and checkout the desired commit
            result = subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", commit_sha],
                cwd=clone_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                return False
            result = subprocess.run(
                ["git", "checkout", commit_sha],
                cwd=clone_dir,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False
            out_dir = os.path.dirname(out_zip_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            base = out_zip_path.replace(".zip", "")
            shutil.make_archive(base, "zip", os.path.dirname(clone_dir), "repo")
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Update CSV with commit IDs and optionally zip repos.")
    parser.add_argument(
        "--csv",
        default=os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..", "..", "..", "dataset", "cwv-bench-v0", "data", "cwv-bench-v0.2.csv"
        ),
        help="Path to cwv-bench-v0.2.csv",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output CSV path (default: overwrite --csv)",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="Also clone and zip each repo at the resolved commit",
    )
    parser.add_argument(
        "--zips-dir",
        default=None,
        help="Directory to write zip files. Default when --zip: dataset/cwv-bench-v0/REPO_SNAPSHOTS/.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel workers for fetching commit IDs (default: 8)",
    )
    parser.add_argument(
        "--zip-workers",
        type=int,
        default=2,
        help="Parallel workers for zipping (default: 2)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N rows (for testing)",
    )
    args = parser.parse_args()

    csv_path = os.path.abspath(args.csv)
    out_path = os.path.abspath(args.out or args.csv)

    # Default zips dir: dataset/cwv-bench-v0/REPO_SNAPSHOTS/
    dataset_root = os.path.abspath(os.path.join(os.path.dirname(csv_path), ".."))
    default_zips_dir = os.path.join(dataset_root, "REPO_SNAPSHOTS")
    zips_dir_abs = os.path.abspath(args.zips_dir) if args.zips_dir else default_zips_dir

    if args.zip:
        print(f"Zips will be written to: {zips_dir_abs}")

    # Read CSV (full file so we can write back all rows)
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames
    except Exception as e:
        raise SystemExit(f"Failed to read CSV: {e}")
    to_process = rows[: args.limit] if args.limit else rows

    if "COMMIT_ID" not in fieldnames or "REPO_ID" not in fieldnames:
        raise SystemExit("CSV must have columns REPO_ID and COMMIT_ID")

    # 1) Fetch all commit IDs in parallel (only for rows we're processing)
    repo_ids = list({r["REPO_ID"] for r in to_process})
    repo_to_commit = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(get_last_commit_sha, rid): rid for rid in repo_ids}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Fetching commit IDs", unit="repo"):
            repo_id = futures[fut]
            try:
                repo_to_commit[repo_id] = fut.result()
            except Exception:
                repo_to_commit[repo_id] = None
    commit_cache = {r["REPO_ID"]: repo_to_commit.get(r["REPO_ID"]) for r in rows}

    # 2) Update COMMIT_ID for all rows
    zip_path_col = "ZIP_REPO_PATH"
    for r in rows:
        rid = r["REPO_ID"]
        try:
            r["COMMIT_ID"] = (commit_cache.get(rid) or r.get("COMMIT_ID") or "").strip() or ""
        except Exception:
            r["COMMIT_ID"] = ""

    # 3) Optionally zip repos (with progress bar; failures don't crash; skip if zip exists)
    if args.zip and zips_dir_abs:
        to_zip = []
        for r in to_process:
            if not r.get("COMMIT_ID"):
                continue
            rid = r["REPO_ID"]
            safe_name = sanitize_repo_id_for_path(rid)
            zip_full = os.path.join(zips_dir_abs, f"{safe_name}.zip")
            if os.path.isfile(zip_full):
                r[zip_path_col] = os.path.relpath(zip_full, dataset_root) if zip_full.startswith(dataset_root) else zip_full
                continue
            to_zip.append((r, rid, zip_full))
        num_with_commit = sum(1 for r in to_process if r.get("COMMIT_ID"))
        print(f"Zipping {len(to_zip)} repos (skipping {num_with_commit - len(to_zip)} already present)...", flush=True)
        for r, rid, zip_full in tqdm(to_zip, desc="Zipping repos", unit="repo", file=sys.stderr, mininterval=0.5, dynamic_ncols=True):
            try:
                if zip_repo_at_commit(rid, r["COMMIT_ID"], zip_full):
                    r[zip_path_col] = os.path.relpath(zip_full, dataset_root) if zip_full.startswith(dataset_root) else zip_full
                else:
                    r[zip_path_col] = ""
            except Exception:
                r[zip_path_col] = ""
        for r in rows:
            if zip_path_col not in r:
                r[zip_path_col] = ""

    # 3) Ensure ZIP_REPO_PATH column exists
    if zip_path_col not in fieldnames:
        fieldnames = list(fieldnames) + [zip_path_col]

    # 5) Write CSV
    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    except Exception as e:
        raise SystemExit(f"Failed to write CSV: {e}")

    filled = sum(1 for r in rows if r.get("COMMIT_ID"))
    zipped = sum(1 for r in rows if r.get(zip_path_col))
    print(f"Updated {out_path} with commit IDs (and zips if --zip).")
    print(f"Commit IDs filled: {filled}/{len(rows)}.")
    if args.zip and zips_dir_abs:
        num_to_zip = sum(1 for r in to_process if r.get("COMMIT_ID"))
        print(f"Repos zipped: {zipped}/{num_to_zip} (failures skipped, process did not crash).")


if __name__ == "__main__":
    main()
