#!/usr/bin/env python3
"""
Build cwv-bench-v0.3.csv from the HF dataset `behavior-in-the-wild/cwv-bench-v0`.

For each datapoint:
- read repo_id from the HF dataset
- fetch the latest commit SHA from GitHub
- create / reuse a zip snapshot under REPO_SNAPSHOTS/
- write a new CSV with:
    ID, REPO_ID, COMMIT_ID, ZIP_REPO_PATH

Existing zips in REPO_SNAPSHOTS are **reused** (not re-cloned).
"""

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional

from datasets import load_dataset  # type: ignore[import]

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, total=None, desc=None, **kwargs):
        return it


DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
SPLIT = "train"


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


def zip_repo_at_commit(
    repo_id: str,
    commit_sha: str,
    out_zip_path: str,
    timeout: int = 300,
    retries: int = 3,
) -> bool:
    """Clone repo at given commit, zip it, and save to out_zip_path.

    Retries a few times with exponential backoff. Returns False if all attempts fail.
    """
    if not commit_sha:
        return False
    repo_url = f"https://github.com/{repo_id}.git"

    for attempt in range(1, retries + 1):
        try:
            with tempfile.TemporaryDirectory(prefix="repo_") as tmpdir:
                clone_dir = os.path.join(tmpdir, "repo")
                # First try a shallow clone and fetch
                result = subprocess.run(
                    ["git", "clone", "--depth", "1", repo_url, clone_dir],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

                result = subprocess.run(
                    ["git", "fetch", "--depth", "1", "origin", commit_sha],
                    cwd=clone_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"git fetch failed: {result.stderr.strip()}")

                result = subprocess.run(
                    ["git", "checkout", commit_sha],
                    cwd=clone_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"git checkout failed: {result.stderr.strip()}")

                out_dir = os.path.dirname(out_zip_path)
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                base = out_zip_path.replace(".zip", "")
                shutil.make_archive(base, "zip", clone_dir)

            return True
        except Exception as e:
            wait = 2 ** attempt
            print(
                f"[zip] Attempt {attempt}/{retries} failed for {repo_id}@{commit_sha}: {e}",
                file=sys.stderr,
                flush=True,
            )
            if attempt == retries:
                break
            time.sleep(wait)

    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create cwv-bench-v0.3.csv from HF dataset and zip repo snapshots."
    )
    parser.add_argument(
        "--out-csv",
        default=os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "dataset",
            "cwv-bench-v0",
            "data",
            "cwv-bench-v0.3.csv",
        ),
        help="Output CSV path (default: dataset/cwv-bench-v0/data/cwv-bench-v0.3.csv)",
    )
    parser.add_argument(
        "--zips-dir",
        default=None,
        help="Directory to write zip files. Default: dataset/cwv-bench-v0/REPO_SNAPSHOTS/.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Parallel workers for fetching commit IDs (default: 16)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N datapoints (for testing)",
    )
    args = parser.parse_args()

    out_csv = os.path.abspath(args.out_csv)

    # Resolve default zips dir relative to dataset root
    dataset_root = os.path.abspath(
        os.path.join(os.path.dirname(out_csv), "..")
    )  # .../cwv-bench-v0
    zips_dir = os.path.abspath(args.zips_dir) if args.zips_dir else os.path.join(dataset_root, "REPO_SNAPSHOTS")

    print(f"[config] HF dataset: {DATASET_NAME}:{SPLIT}", flush=True)
    print(f"[config] Output CSV: {out_csv}", flush=True)
    print(f"[config] Repo snapshots dir: {zips_dir}", flush=True)

    # Load HF dataset
    print("[1/4] Loading HF dataset...", flush=True)
    ds = load_dataset(DATASET_NAME, split=SPLIT)
    if args.limit is not None:
        ds = ds.select(range(min(args.limit, len(ds))))
    n = len(ds)
    print(f"[1/4] Loaded {n} datapoints", flush=True)

    # Collect repo_ids
    repo_ids = []
    for ex in ds:
        repo_id = ex.get("repo_id") or ex.get("REPO_ID") or ex.get("github_repo")  # best effort
        if not repo_id:
            raise SystemExit("Dataset example missing repo_id / REPO_ID / github_repo field")
        repo_ids.append(str(repo_id))

    # 2) Fetch commit IDs in parallel
    print("[2/4] Fetching latest commit IDs from GitHub...", flush=True)
    repo_to_commit: Dict[str, Optional[str]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(get_last_commit_sha, rid): rid for rid in repo_ids}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Fetching commit IDs", unit="repo"):
            rid = futures[fut]
            try:
                repo_to_commit[rid] = fut.result()
            except Exception:
                repo_to_commit[rid] = None

    # 3) Build rows and ensure zips
    print("[3/4] Creating / reusing repo snapshots...", flush=True)
    os.makedirs(zips_dir, exist_ok=True)

    rows: list[Dict[str, Any]] = []
    for idx, (ex, rid) in enumerate(zip(ds, repo_ids), start=1):
        commit = repo_to_commit.get(rid) or ""
        safe_name = sanitize_repo_id_for_path(rid)
        zip_full = os.path.join(zips_dir, f"{safe_name}.zip")

        zip_rel = os.path.relpath(zip_full, dataset_root)

        # If zip already exists, reuse it; otherwise, create it (if commit known)
        if os.path.isfile(zip_full):
            zip_path_out = zip_rel
        elif commit:
            ok = zip_repo_at_commit(rid, commit, zip_full)
            zip_path_out = zip_rel if ok else ""
        else:
            zip_path_out = ""

        row: Dict[str, Any] = {
            "ID": idx,
            "REPO_ID": rid,
            "COMMIT_ID": commit,
            "ZIP_REPO_PATH": zip_path_out,
        }
        print(row)
        rows.append(row)

    # 4) Write CSV
    print("[4/4] Writing CSV...", flush=True)
    fieldnames = ["ID", "REPO_ID", "COMMIT_ID", "ZIP_REPO_PATH"]
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    filled_commits = sum(1 for r in rows if r["COMMIT_ID"])
    filled_zips = sum(1 for r in rows if r["ZIP_REPO_PATH"])
    print(f"[done] Wrote {len(rows)} rows to {out_csv}", flush=True)
    print(f"[done] Commit IDs filled: {filled_commits}/{len(rows)}", flush=True)
    print(f"[done] Repo snapshots available: {filled_zips}/{len(rows)}", flush=True)


if __name__ == "__main__":
    main()

