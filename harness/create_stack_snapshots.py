#!/usr/bin/env python3
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed

from tqdm import tqdm


def run(cmd, cwd=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def get_head_commit(repo_dir):
    result = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def zip_repo(src_dir, zip_path):
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_STORED
    ) as zf:
        for root, dirs, files in os.walk(src_dir):
            if ".git" in dirs:
                dirs.remove(".git")
            for filename in files:
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, src_dir)
                zf.write(full_path, rel_path)


def process_repo(record, args):
    repo_id = record.get("repo_id")
    framework = record.get("framework")

    if not repo_id:
        return None, "missing repo_id"

    repo_name = repo_id.split("/")[-1]
    if repo_name.endswith(".github.io"):
        repo_name = repo_name[:-len(".github.io")]

    safe_repo_dir = repo_id.replace("/", "__")
    pid = os.getpid()
    checkout_dir = os.path.join(args.work_dir, f"{pid}_{safe_repo_dir}")

    clone_url = f"https://github.com/{repo_id}.git"

    try:
        result = run(
            ["git", "clone", "--depth", "1", clone_url, checkout_dir]
        )
        if result.returncode != 0:
            return None, f"{repo_id}: clone failed: {result.stderr.strip()}"

        commit = get_head_commit(checkout_dir)
        if not commit:
            return None, f"{repo_id}: failed to read HEAD"

        zip_name = f"{repo_name}___{commit}.zip"
        zip_path = os.path.join(args.snapshots_dir, zip_name)

        if not os.path.exists(zip_path):
            zip_repo(checkout_dir, zip_path)

        return {
            "repo_id": repo_id,
            "snapshot_path": zip_path,
            "framework": framework,
            "latest_commit_id": commit,
        }, None

    finally:
        shutil.rmtree(checkout_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="Create zip snapshots for stack repos (parallel)."
    )
    parser.add_argument(
        "--jsonl",
        default="/home/ssm-user/working/arnav/dataset/cwv-bench-v0/sources/stack/successful_deployments.jsonl",
    )
    parser.add_argument(
        "--snapshots-dir",
        default="/home/ssm-user/working/arnav/harness/REPO_SNAPSHOTS",
    )
    parser.add_argument(
        "--csv",
        default="/home/ssm-user/working/arnav/dataset/cwv-bench-v0/data/stack.csv",
    )
    parser.add_argument(
        "--work-dir",
        default="/home/ssm-user/working/arnav/harness/tmp/stack_snapshot_work",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip repos already present in CSV",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N repos (0 = no limit)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of parallel workers",
    )
    args = parser.parse_args()

    os.makedirs(args.snapshots_dir, exist_ok=True)
    os.makedirs(args.work_dir, exist_ok=True)

    processed_repos = set()
    csv_exists = os.path.exists(args.csv)

    if args.resume and csv_exists:
        with open(args.csv, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("repo_id"):
                    processed_repos.add(row["repo_id"])

    records = []
    with open(args.jsonl, "r") as f:
        for line_no, line in enumerate(f, start=1):
            if args.limit and len(records) >= args.limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[WARN] line {line_no}: invalid JSON: {exc}", file=sys.stderr)
                continue
            repo_id = record.get("repo_id")
            if args.resume and repo_id in processed_repos:
                continue
            records.append(record)

    mode = "a" if args.resume and csv_exists else "w"
    with open(args.csv, mode, newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "repo_id",
                "snapshot_path",
                "framework",
                "latest_commit_id",
            ],
        )
        if not (args.resume and csv_exists):
            writer.writeheader()

        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(process_repo, record, args)
                for record in records
            ]

            for fut in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="Processing repos",
            ):
                result, err = fut.result()
                if err:
                    print(f"[WARN] {err}", file=sys.stderr)
                    continue
                writer.writerow(result)
                

if __name__ == "__main__":
    main()
