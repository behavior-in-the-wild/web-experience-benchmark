import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from datasets import load_dataset
from tqdm import tqdm


# -----------------------------
# Helpers
# -----------------------------
def parse_maybe_json(x):
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        x = x.strip()
        if not x:
            return None
        try:
            return json.loads(x)
        except Exception:
            return None
    return None


def normalize_url(url: str) -> str:
    if not url:
        return url
    return url.strip()


def load_done_urls(output_path: Path) -> set[str]:
    done = set()
    if not output_path.exists():
        return done

    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                url = obj.get("url")
                if url:
                    done.add(url)
            except Exception:
                continue
    return done


def extract_checked_url(row: dict):
    is_live = parse_maybe_json(row.get("IS_LIVE"))
    if not is_live:
        return None

    live = is_live.get("LIVE")
    status = is_live.get("STATUS")
    checked_url = is_live.get("CHECKED_URL")

    if checked_url and live is True and status == 200:
        return normalize_url(checked_url)
    return None


def extract_targets(dataset, limit=None):
    """
    Extract one URL per row using IS_LIVE.CHECKED_URL.
    Filters to LIVE == True and STATUS == 200.
    Deduplicates URLs.
    """
    urls = []
    seen = set()

    for row in dataset:
        url = extract_checked_url(row)
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(
            {
                "url": url,
                "repo_id": row.get("REPO_ID"),
                "commit_id": row.get("COMMIT_ID"),
                "framework_label": row.get("FRAMEWORK"),
                "source": row.get("SOURCE"),
            }
        )
        if limit is not None and len(urls) >= limit:
            break

    return urls


# -----------------------------
# Wappalyzer runner
# -----------------------------
def run_wappalyzer(
    target: dict,
    cmd_prefix: list[str],
    timeout_sec: int = 45,
):
    url = target["url"]
    started = time.perf_counter()
    ts = datetime.now(timezone.utc).isoformat()

    # Assumes the CLI prints JSON to stdout.
    # Replace cmd_prefix if your local setup differs.
    cmd = cmd_prefix + [url]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip() or None

        parsed = None
        parse_error = None

        if stdout:
            try:
                parsed = json.loads(stdout)
            except Exception as e:
                parse_error = f"stdout was not valid JSON: {e}"

        result = {
            "url": url,
            "ts": ts,
            "duration_ms": duration_ms,
            "exit_code": proc.returncode,
            "ok": proc.returncode == 0 and parse_error is None,
            "parse_error": parse_error,
            "stderr": stderr,
            "data": parsed,
            # extra metadata from your dataset
            "repo_id": target.get("repo_id"),
            "commit_id": target.get("commit_id"),
            "framework_label": target.get("framework_label"),
            "source": target.get("source"),
        }
        return result

    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "url": url,
            "ts": ts,
            "duration_ms": duration_ms,
            "exit_code": None,
            "ok": False,
            "parse_error": "timeout",
            "stderr": str(e),
            "data": None,
            "repo_id": target.get("repo_id"),
            "commit_id": target.get("commit_id"),
            "framework_label": target.get("framework_label"),
            "source": target.get("source"),
        }
    except Exception as e:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "url": url,
            "ts": ts,
            "duration_ms": duration_ms,
            "exit_code": None,
            "ok": False,
            "parse_error": f"runner_error: {e}",
            "stderr": None,
            "data": None,
            "repo_id": target.get("repo_id"),
            "commit_id": target.get("commit_id"),
            "framework_label": target.get("framework_label"),
            "source": target.get("source"),
        }


# -----------------------------
# Main
# -----------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="behavior-in-the-wild/cwv-bench-v0")
    parser.add_argument("--split", default="train")
    parser.add_argument("--output", default="wappalyzer_results.jsonl")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=45)

    # Example for official Wappalyzer repo CLI:
    # node src/drivers/npm/cli.js https://example.com
    parser.add_argument(
        "--cmd",
        nargs="+",
        default=["node", "src/drivers/npm/cli.js"],
        help="Command prefix for Wappalyzer CLI. URL is appended automatically.",
    )

    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Loading dataset: {args.dataset} [{args.split}]")
    ds = load_dataset(args.dataset, split=args.split)

    print("Extracting LIVE STATUS==200 CHECKED_URL targets...")
    targets = extract_targets(ds, limit=args.limit)

    done_urls = load_done_urls(output_path)
    if done_urls:
        targets = [t for t in targets if t["url"] not in done_urls]

    print(f"Targets to scan: {len(targets)}")
    print(f"Command prefix: {' '.join(args.cmd)}")

    if not targets:
        print("Nothing to do.")
        return

    with output_path.open("a", encoding="utf-8") as fout:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(run_wappalyzer, t, args.cmd, args.timeout): t
                for t in targets
            }

            for fut in tqdm(as_completed(futures), total=len(futures)):
                res = fut.result()
                fout.write(json.dumps(res, ensure_ascii=False) + "\n")
                fout.flush()

    print(f"Done. Results written to: {output_path.resolve()}")


if __name__ == "__main__":
    main()