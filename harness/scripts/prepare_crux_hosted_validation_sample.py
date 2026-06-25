#!/usr/bin/env python3
"""Prepare CrUX-covered cwv-bench-v0 samples for hosted CWV validation.

This script does not measure anything. It loads the Hugging Face dataset,
filters rows with embedded CrUX field data, samples a deterministic 500-row
candidate set, then optionally samples a smaller pilot set from those rows.

Outputs include row-level TSVs and device-level job TSVs. The job TSVs are the
inputs for run_hosted_cwv_for_crux_sample.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path
from typing import Any


DATASET_NAME = "behavior-in-the-wild/cwv-bench-v0"
CRUX_COLUMN = "CRUX_METRICS_LIST_ORDER_LCP_CLS_INP_FCP_TTFB"
METRICS = ("LCP", "CLS", "INP", "FCP", "TTFB")


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_crux_records(raw: Any) -> list[dict[str, Any]]:
    if raw in (None, "", "-"):
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    try:
        data = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def crux_values_by_device(raw: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for record in parse_crux_records(raw):
        device = str(record.get("strategy") or "").strip().lower()
        if device not in {"mobile", "desktop"}:
            continue
        names = [x.strip() for x in str(record.get("values_order") or "LCP,CLS,INP,FCP,TTFB").split(",")]
        values = record.get("values") or []
        metrics = {name: values[i] if i < len(values) else None for i, name in enumerate(names)}
        if metrics.get("CLS") is not None:
            try:
                cls = float(metrics["CLS"])
                if abs(cls) > 1:
                    metrics["CLS"] = cls / 1000.0
            except (TypeError, ValueError):
                pass
        out[device] = {
            "level": record.get("level", ""),
            "url": record.get("checked_url", ""),
            "metrics": {name: metrics.get(name) for name in METRICS},
        }
    return out


def live_url(row: dict[str, Any], crux_by_device: dict[str, dict[str, Any]]) -> str:
    live = row.get("IS_LIVE") or {}
    if isinstance(live, dict):
        for key in ("CHECKED_URL", "REPO_URL"):
            if live.get(key):
                return str(live[key])
    for device in ("mobile", "desktop"):
        url = crux_by_device.get(device, {}).get("url")
        if url:
            return str(url)
    repo_id = str(row.get("REPO_ID") or "")
    if repo_id.endswith(".github.io") and "/" in repo_id:
        owner = repo_id.split("/", 1)[0]
        return f"https://{owner}.github.io"
    return ""


def row_record(row: dict[str, Any], crux_by_device: dict[str, dict[str, Any]]) -> dict[str, Any]:
    devices = sorted(crux_by_device)
    rec: dict[str, Any] = {
        "ID": row.get("ID"),
        "REPO_ID": row.get("REPO_ID"),
        "FRAMEWORK": row.get("FRAMEWORK") or row.get("framework") or "",
        "SOURCE": row.get("SOURCE") or "",
        "hosted_url": live_url(row, crux_by_device),
        "crux_devices": ",".join(devices),
        "has_mobile_crux": int("mobile" in crux_by_device),
        "has_desktop_crux": int("desktop" in crux_by_device),
    }
    for device in ("mobile", "desktop"):
        info = crux_by_device.get(device, {})
        rec[f"crux_{device}_url"] = info.get("url", "")
        rec[f"crux_{device}_level"] = info.get("level", "")
        for metric in METRICS:
            rec[f"crux_{device}_{metric}_p75"] = info.get("metrics", {}).get(metric)
    return rec


def job_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for row in rows:
        for device in ("mobile", "desktop"):
            url = row.get(f"crux_{device}_url") or row.get("hosted_url")
            if not row.get(f"has_{device}_crux") or not url:
                continue
            job: dict[str, Any] = {
                "ID": row["ID"],
                "REPO_ID": row["REPO_ID"],
                "FRAMEWORK": row.get("FRAMEWORK", ""),
                "device": device,
                "url": url,
                "crux_level": row.get(f"crux_{device}_level", ""),
            }
            for metric in METRICS:
                job[f"crux_{metric}_p75"] = row.get(f"crux_{device}_{metric}_p75")
            jobs.append(job)
    return jobs


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=DATASET_NAME)
    parser.add_argument("--split", default="train")
    parser.add_argument("--out-dir", default="harness/out/crux_hosted_validation_sample")
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--pilot-size", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260622)
    parser.add_argument("--crux-scope", choices=["any", "both"], default="any")
    parser.add_argument("--include-not-live", action="store_true")
    args = parser.parse_args()

    from datasets import load_dataset

    ds = load_dataset(args.dataset, split=args.split)
    eligible: list[dict[str, Any]] = []
    skipped = {"no_crux": 0, "not_live": 0, "scope": 0}
    for row in ds:
        crux_by_device = crux_values_by_device(row.get(CRUX_COLUMN))
        if not crux_by_device:
            skipped["no_crux"] += 1
            continue
        is_live = parse_bool((row.get("IS_LIVE") or {}).get("LIVE")) if isinstance(row.get("IS_LIVE"), dict) else False
        if not args.include_not_live and not is_live:
            skipped["not_live"] += 1
            continue
        if args.crux_scope == "both" and set(crux_by_device) != {"mobile", "desktop"}:
            skipped["scope"] += 1
            continue
        eligible.append(row_record(row, crux_by_device))

    rng = random.Random(args.seed)
    candidates = rng.sample(eligible, min(args.sample_size, len(eligible)))
    pilot = rng.sample(candidates, min(args.pilot_size, len(candidates)))
    candidate_jobs = job_records(candidates)
    pilot_jobs = job_records(pilot)

    out_dir = Path(args.out_dir)
    write_tsv(out_dir / "candidate_rows.tsv", candidates)
    write_tsv(out_dir / "candidate_jobs.tsv", candidate_jobs)
    write_tsv(out_dir / "pilot_rows.tsv", pilot)
    write_tsv(out_dir / "pilot_jobs.tsv", pilot_jobs)
    summary = {
        "dataset": args.dataset,
        "split": args.split,
        "seed": args.seed,
        "requested_sample_size": args.sample_size,
        "requested_pilot_size": args.pilot_size,
        "crux_scope": args.crux_scope,
        "include_not_live": args.include_not_live,
        "dataset_rows": len(ds),
        "eligible_rows": len(eligible),
        "candidate_rows": len(candidates),
        "candidate_jobs": len(candidate_jobs),
        "pilot_rows": len(pilot),
        "pilot_jobs": len(pilot_jobs),
        "skipped": skipped,
        "outputs": {
            "candidate_rows": str(out_dir / "candidate_rows.tsv"),
            "candidate_jobs": str(out_dir / "candidate_jobs.tsv"),
            "pilot_rows": str(out_dir / "pilot_rows.tsv"),
            "pilot_jobs": str(out_dir / "pilot_jobs.tsv"),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
