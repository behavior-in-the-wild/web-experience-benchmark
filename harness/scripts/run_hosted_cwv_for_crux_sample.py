#!/usr/bin/env python3
"""Measure hosted CWV for CrUX-covered sample jobs.

Input is a device-level job TSV from prepare_crux_hosted_validation_sample.py.
Each row has one URL/device pair plus the matching CrUX p75 values. This script
measures the hosted URL with the repo's canonical CWV tool and writes raw JSON
per job plus a joined TSV ready for correlation.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cwv_tool.performance_testing import calculate_aggregated_metrics, measure_multiple_runs


METRICS = ("LCP", "CLS", "INP", "FCP", "TTFB")


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def output_name(row: dict[str, str]) -> str:
    return f"{row['ID']}_{row['device']}.json"


async def measure_job(row: dict[str, str], runs: int, headless: bool) -> dict[str, Any]:
    measured_runs, settle_time, success = await measure_multiple_runs(
        row["url"],
        device=row["device"],
        headless=headless,
        num_runs=runs,
    )
    aggregated = calculate_aggregated_metrics(measured_runs)
    return {
        "status": "success" if success else "error",
        "url": row["url"],
        "device": row["device"],
        "runs": measured_runs,
        "final_settle_time": settle_time,
        "aggregated": aggregated,
    }


async def run_job(
    idx: int,
    total: int,
    job: dict[str, str],
    raw_dir: Path,
    runs: int,
    headless: bool,
    force: bool,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    raw_path = raw_dir / output_name(job)
    async with semaphore:
        if raw_path.exists() and not force:
            result = json.loads(raw_path.read_text(encoding="utf-8"))
        else:
            print(f"[{idx}/{total}] measuring {job['device']} {job['ID']} {job['url']}", flush=True)
            try:
                result = await measure_job(job, runs=runs, headless=headless)
            except Exception as exc:
                result = {
                    "status": "error",
                    "url": job.get("url"),
                    "device": job.get("device"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "runs": [],
                    "aggregated": {"valid_runs": 0, "total_runs": runs},
                }
            raw_path.write_text(json.dumps(result, indent=2, allow_nan=True), encoding="utf-8")
        return joined_row(job, result, raw_path)


def joined_row(job: dict[str, str], result: dict[str, Any], raw_path: Path) -> dict[str, Any]:
    agg = result.get("aggregated") or {}
    out: dict[str, Any] = {
        "ID": job.get("ID"),
        "REPO_ID": job.get("REPO_ID"),
        "FRAMEWORK": job.get("FRAMEWORK", ""),
        "device": job.get("device"),
        "url": job.get("url"),
        "crux_level": job.get("crux_level", ""),
        "hosted_status": result.get("status", "error"),
        "hosted_valid_runs": agg.get("valid_runs"),
        "hosted_total_runs": agg.get("total_runs"),
        "hosted_raw_json": str(raw_path),
    }
    for metric in METRICS:
        out[f"crux_{metric}_p75"] = parse_float(job.get(f"crux_{metric}_p75"))
        out[f"hosted_{metric}_p75"] = parse_float(agg.get(f"{metric}_p75") or agg.get(f"{metric}_median"))
    return out


async def main_async() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", default="harness/out/crux_hosted_validation_sample/pilot_jobs.tsv")
    parser.add_argument("--out-dir", default="harness/out/crux_hosted_validation_sample/hosted_cwv")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--parallel", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--devices", choices=["all", "mobile", "desktop"], default="all")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    jobs = read_tsv(Path(args.jobs))
    if args.devices != "all":
        jobs = [row for row in jobs if row.get("device") == args.devices]
    if args.limit:
        jobs = jobs[: args.limit]

    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, args.parallel))
    tasks = [
        asyncio.create_task(
            run_job(
                idx,
                len(jobs),
                job,
                raw_dir,
                runs=args.runs,
                headless=not args.no_headless,
                force=args.force,
                semaphore=semaphore,
            )
        )
        for idx, job in enumerate(jobs, start=1)
    ]
    joined = await asyncio.gather(*tasks)
    joined.sort(key=lambda row: (str(row.get("ID", "")), str(row.get("device", ""))))
    write_tsv(out_dir / "hosted_crux_joined.tsv", joined)

    summary = {
        "jobs_input": args.jobs,
        "jobs_total": len(jobs),
        "runs": args.runs,
        "parallel": args.parallel,
        "successful_jobs": sum(row.get("hosted_status") == "success" for row in joined),
        "joined_tsv": str(out_dir / "hosted_crux_joined.tsv"),
        "raw_dir": str(raw_dir),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
