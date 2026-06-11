#!/usr/bin/env python3
"""Crawl-provided URL list and benchmark CWV for every page.

Reads a plain `urls.txt` (one URL per line) or accepts `--urls-file`.
Uses the project's CWV measurement functions (Playwright-based) and
writes results to JSONL and a final JSON file.

Usage:
    python scripts/helper_scripts/crawling/crawl_and_benchmark.py --urls-file urls.txt --out results.jsonl --concurrency 4 --num-runs 3
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import List

# allow importing from project src folder
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from cwv_tool.performance_testing import (
    measure_cwv_metrics,
    calculate_aggregated_metrics,
)


async def _measure_url(semaphore: asyncio.Semaphore, url: str, device: str, runs: int):
    async with semaphore:
        runs_data = []
        for i in range(runs):
            try:
                metrics = await measure_cwv_metrics(url, device, headless=True)
            except Exception as e:
                metrics = {"error": str(e)}
            runs_data.append(metrics)
            await asyncio.sleep(0.25)

        aggregated = calculate_aggregated_metrics(runs_data)
        return {"url": url, "runs": runs_data, "aggregated": aggregated}


async def run_all(urls: List[str], device: str, runs: int, concurrency: int, out_path: Path):
    sem = asyncio.Semaphore(concurrency)
    tasks = [asyncio.create_task(_measure_url(sem, u, device, runs)) for u in urls]

    # Write results as they complete to a JSONL file
    with out_path.open("w") as f:
        results = []
        for coro in asyncio.as_completed(tasks):
            res = await coro
            f.write(json.dumps(res) + "\n")
            f.flush()
            results.append(res)

    # Save a final aggregated JSON file nearby
    final = out_path.with_suffix(".json")
    final.write_text(json.dumps(results, indent=2))
    print(f"Saved {len(results)} results to {out_path} and {final}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls-file", required=True, help="Path to newline-delimited URLs file")
    p.add_argument("--device", default="mobile", choices=["mobile", "desktop"])
    p.add_argument("--num-runs", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--out", default="cwv_pages_results.jsonl")
    args = p.parse_args()

    urls = [l.strip() for l in open(args.urls_file) if l.strip()]
    if not urls:
        print("No URLs found in", args.urls_file)
        raise SystemExit(1)

    out_path = Path(args.out)

    asyncio.run(run_all(urls, args.device, args.num_runs, args.concurrency, out_path))


if __name__ == "__main__":
    main()
