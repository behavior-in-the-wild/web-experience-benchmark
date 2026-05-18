#!/usr/bin/env python3
import argparse
import csv
import json
import os
from collections import defaultdict


NUMERIC_FIELDS = (
    "requests",
    "failed_requests",
    "missing_usage_requests",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "total_tokens",
    "request_bytes",
    "response_bytes",
)


def add_number(target, key, value):
    if value is None:
        return
    try:
        target[key] += float(value)
    except Exception:
        return


def new_bucket():
    return {
        "requests": 0,
        "failed_requests": 0,
        "missing_usage_requests": 0,
        "latency_ms": 0.0,
        "prompt_tokens": 0.0,
        "completion_tokens": 0.0,
        "reasoning_tokens": 0.0,
        "total_tokens": 0.0,
        "request_bytes": 0.0,
        "response_bytes": 0.0,
    }


def ingest(bucket, record):
    bucket["requests"] += 1
    if not record.get("ok"):
        bucket["failed_requests"] += 1
    if record.get("usage_missing"):
        bucket["missing_usage_requests"] += 1
    for src, dst in (
        ("latency_ms", "latency_ms"),
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("reasoning_tokens", "reasoning_tokens"),
        ("total_tokens", "total_tokens"),
        ("request_bytes", "request_bytes"),
        ("response_bytes", "response_bytes"),
    ):
        add_number(bucket, dst, record.get(src))


def finalize(bucket):
    out = dict(bucket)
    req = out["requests"] or 1
    out["avg_latency_ms"] = round(out["latency_ms"] / req, 3)
    out["latency_ms"] = round(out["latency_ms"], 3)
    for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "request_bytes", "response_bytes"):
        out[key] = int(out[key])
    return out


def write_csv(path, rows, key_fields):
    fields = list(key_fields) + list(NUMERIC_FIELDS) + ["avg_latency_ms"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def main():
    parser = argparse.ArgumentParser(description="Aggregate usage_proxy.py JSONL output.")
    parser.add_argument("--input", required=True, help="api_calls.jsonl")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    overall = new_bucket()
    by_phase = defaultdict(new_bucket)
    by_job = defaultdict(new_bucket)
    by_job_phase = defaultdict(new_bucket)
    by_status = defaultdict(new_bucket)

    records = 0
    if os.path.exists(args.input):
        with open(args.input, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                records += 1
                ingest(overall, record)
                ingest(by_phase[record.get("phase") or "unknown"], record)
                ingest(by_job[record.get("job_label") or "unknown"], record)
                ingest(by_job_phase[(record.get("job_label") or "unknown", record.get("phase") or "unknown")], record)
                ingest(by_status[str(record.get("status_code") or "unknown")], record)

    summary = {
        "input": os.path.abspath(args.input),
        "records": records,
        "overall": finalize(overall),
        "by_phase": {phase: finalize(bucket) for phase, bucket in sorted(by_phase.items())},
        "by_status": {status: finalize(bucket) for status, bucket in sorted(by_status.items())},
    }
    with open(os.path.join(args.output_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)

    phase_rows = [{"phase": phase, **finalize(bucket)} for phase, bucket in sorted(by_phase.items())]
    job_rows = [{"job_label": job, **finalize(bucket)} for job, bucket in sorted(by_job.items())]
    job_phase_rows = [
        {"job_label": job, "phase": phase, **finalize(bucket)}
        for (job, phase), bucket in sorted(by_job_phase.items())
    ]

    write_csv(os.path.join(args.output_dir, "by_phase.csv"), phase_rows, ["phase"])
    write_csv(os.path.join(args.output_dir, "by_job.csv"), job_rows, ["job_label"])
    write_csv(os.path.join(args.output_dir, "by_job_phase.csv"), job_phase_rows, ["job_label", "phase"])


if __name__ == "__main__":
    main()
