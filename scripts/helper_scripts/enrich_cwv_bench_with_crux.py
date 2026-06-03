#!/usr/bin/env python3

import argparse
import csv
import json
import os
from pathlib import Path

from datasets import load_dataset


ORDER = ["lcp", "cls", "inp", "fcp", "ttfb"]


def as_bool(x):
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in {"true", "1", "yes", "y"}


def clean_value(x):
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "null"}:
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except Exception:
        return s


def load_crux_by_repo(summary_csv):
    crux_by_repo = {}

    with open(summary_csv, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            repo_id = (row.get("repo_id") or "").strip()
            if not repo_id:
                continue

            url_found = as_bool(row.get("url_crux_found"))
            origin_found = as_bool(row.get("origin_crux_found"))

            if not url_found and not origin_found:
                continue

            strategy = (row.get("strategy") or "").strip() or "unknown"
            checked_url = (row.get("checked_url") or "").strip()

            if url_found:
                level = "url"
                prefix = "url_p75_"
            else:
                level = "origin"
                prefix = "origin_p75_"

            values = [clean_value(row.get(prefix + metric)) for metric in ORDER]

            record = {
                "strategy": strategy,
                "level": level,
                "checked_url": checked_url,
                "values_order": "LCP,CLS,INP,FCP,TTFB",
                "values": values,
            }

            crux_by_repo.setdefault(repo_id, [])

            duplicate = False
            for old in crux_by_repo[repo_id]:
                if old["strategy"] == record["strategy"] and old["level"] == record["level"] and old["checked_url"] == record["checked_url"]:
                    duplicate = True
                    break

            if not duplicate:
                crux_by_repo[repo_id].append(record)

    return crux_by_repo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="behavior-in-the-wild/cwv-bench-v0")
    parser.add_argument("--split", default="train")
    parser.add_argument("--summary-csv", default="hf_crux_discovery_all/summary.csv")
    parser.add_argument("--output-dir", default="cwv_bench_v0_crux_enriched")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--create-pr", action="store_true")
    parser.add_argument("--commit-message", default="Add CrUX presence and p75 metric columns")
    args = parser.parse_args()

    summary_csv = Path(args.summary_csv)

    if not summary_csv.exists():
        raise FileNotFoundError(f"summary.csv not found: {summary_csv}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    crux_by_repo = load_crux_by_repo(summary_csv)

    print(f"CrUX repos found in summary: {len(crux_by_repo)}")

    ds = load_dataset(args.dataset, split=args.split)

    if "REPO_ID" not in ds.column_names:
        raise ValueError(f"Dataset does not contain REPO_ID. Columns: {ds.column_names}")

    def add_crux_columns(example):
        repo_id = str(example.get("REPO_ID") or "").strip()
        records = crux_by_repo.get(repo_id)

        if records:
            example["CRUX_PRESENT"] = "Yes"
            example["CRUX_METRICS_LIST_ORDER_LCP_CLS_INP_FCP_TTFB"] = json.dumps(records, ensure_ascii=False)
        else:
            example["CRUX_PRESENT"] = "-"
            example["CRUX_METRICS_LIST_ORDER_LCP_CLS_INP_FCP_TTFB"] = "-"

        return example

    enriched = ds.map(add_crux_columns, desc="Adding CrUX columns")

    total = len(enriched)
    present = sum(1 for x in enriched["CRUX_PRESENT"] if x == "Yes")

    print(f"Dataset rows: {total}")
    print(f"Rows with CRUX_PRESENT=Yes: {present}")
    print(f"Rows without CrUX: {total - present}")

    enriched.to_parquet(str(out_dir / "train.parquet"))
    enriched.to_csv(str(out_dir / "train.csv"))

    with open(out_dir / "crux_enrichment_summary.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "dataset": args.dataset,
                "split": args.split,
                "summary_csv": str(summary_csv),
                "dataset_rows": total,
                "rows_with_crux_present_yes": present,
                "rows_without_crux": total - present,
                "new_columns": [
                    "CRUX_PRESENT",
                    "CRUX_METRICS_LIST_ORDER_LCP_CLS_INP_FCP_TTFB",
                ],
                "metric_order": ["LCP", "CLS", "INP", "FCP", "TTFB"],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"Saved local enriched dataset to: {out_dir}")

    if args.push:
        enriched.push_to_hub(
            args.dataset,
            split=args.split,
            commit_message=args.commit_message,
            create_pr=args.create_pr,
        )
        print(f"Pushed to hub: {args.dataset}")


if __name__ == "__main__":
    main()
