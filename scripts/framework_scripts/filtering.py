# cwv-bench/scripts/framework_filtering/filtering.py
import json
import subprocess
import tempfile
import shutil
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from individual_frameworks import DETECTORS, detect_static_html

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------
# Git utilities
# ------------------------

def git_clone(repo_url: str, dst: Path) -> bool:
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dst)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        return dst.exists() and any(dst.iterdir())
    except Exception:
        return False


# ------------------------
# Single repo processor (for threading)
# ------------------------

def process_single_repo(obj: dict, repo_field: str):
    """Process a single repo and return result dict or None."""
    repo = obj.get(repo_field)

    if not repo:
        return None, "missing_field"

    repo_url = (
        repo if repo.startswith("http")
        else f"https://github.com/{repo}.git"
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="ssg_detect_"))

    try:
        if not git_clone(repo_url, tmpdir):
            return None, "clone_failed"

        frameworks = []

        # Run all detectors (their logic is unchanged; we ignore evidence)
        for name, detector in DETECTORS:
            ok, _ev = detector(tmpdir)
            if ok:
                frameworks.append(name)

        # Only check for Static HTML if no SSG framework was detected
        if not frameworks:
            static_ok, _static_ev = detect_static_html(tmpdir)
            if static_ok:
                frameworks.append("Static HTML")

        if frameworks:
            obj["framework"] = ",".join(frameworks)
            # NOTE: framework_evidence intentionally not produced
            return obj, "found"
        else:
            return None, "no_framework"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------
# Main JSONL processor (multithreaded)
# ------------------------

def process_jsonl(
    input_path: str,
    output_path: str,
    repo_field: str = "repo_name",
    max_workers: int = 8,
):
    # Check input file exists
    if not Path(input_path).exists():
        logger.error(f"Input file not found: {input_path}")
        logger.error("Please provide a valid input file with -i/--input")
        return

    # Load all entries (skip empty lines)
    with open(input_path, "r") as f:
        entries = []
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping invalid JSON at line {line_num}: {e}")

    if not entries:
        logger.error(f"No valid entries found in {input_path}")
        return

    total = len(entries)
    logger.info(f"Processing {total} repos from {input_path} with {max_workers} threads")

    # Thread-safe counters
    kept = 0
    clone_failed = 0
    no_framework = 0
    missing_field = 0

    # Lock for file writing and counter updates
    write_lock = threading.Lock()

    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_entry = {
            executor.submit(process_single_repo, entry, repo_field): entry
            for entry in entries
        }

        # Process results as they complete with progress bar
        with tqdm(total=total, desc="Scanning repos", unit="repo") as pbar:
            for future in as_completed(future_to_entry):
                entry = future_to_entry[future]
                repo = entry.get(repo_field, "unknown")

                try:
                    result, status = future.result()

                    with write_lock:
                        if status == "found":
                            results.append(result)
                            kept += 1
                            logger.info(f"✓ {repo} → {result['framework']}")
                        elif status == "clone_failed":
                            clone_failed += 1
                        elif status == "no_framework":
                            no_framework += 1
                        elif status == "missing_field":
                            missing_field += 1

                except Exception as e:
                    logger.error(f"Error processing {repo}: {e}")

                pbar.update(1)

    # Write results to output file (create parent directories if needed)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as fout:
        for result in results:
            fout.write(json.dumps(result) + "\n")

    logger.info("=" * 50)
    logger.info(f"✅ Done — kept {kept}/{total} repos")
    logger.info(f"   Clone failed: {clone_failed}")
    logger.info(f"   No framework detected: {no_framework}")
    if missing_field:
        logger.info(f"   Missing field: {missing_field}")
    logger.info(f"Output written to: {output_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Detect frameworks in GitHub repos from a JSONL dataset (modular detectors)"
    )
    parser.add_argument(
        "-i", "--input",
        default="../../../cwv-bench-exps/gh_25_github_io_repos_filtered.jsonl",
        help="Path to input JSONL file"
    )
    parser.add_argument(
        "-o", "--output",
        default="../../../cwv-bench-exps/tech_stacks_filtered/gh_25_11_tech_stacks_filtered.jsonl",
        help="Path to output JSONL file"
    )
    parser.add_argument(
        "-f", "--field",
        default="repo_name",
        help="JSON field containing repo name/URL"
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=8,
        help="Number of parallel threads"
    )

    args = parser.parse_args()

    process_jsonl(
        input_path=args.input,
        output_path=args.output,
        repo_field=args.field,
        max_workers=args.workers,
    )

# python filtering.py -i ../../../cwv-bench-exps/stack_github_io_websites_filtered.jsonl -o ../../../cwv-bench-exps/11_tech_stacks_filtered/stack_11_tech_stacks_filtered.jsonl -f repo_name -w 8
# python filtering.py -i ../../benchmark/stack_github_io_websites_filtered.jsonl -o ../../benchmark -f repo_name -w 8