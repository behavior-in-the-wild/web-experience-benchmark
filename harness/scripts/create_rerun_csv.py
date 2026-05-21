#!/usr/bin/env python3
"""
Filter an input CSV to only the rows matching a given set of IDs.

Usage:
  python3 harness/scripts/create_rerun_csv.py \
    --csv harness/SAMPLE/input_100.csv \
    --ids 34,425,492,... \
    --out harness/SAMPLE/rerun_<model>.csv

Or pass IDs via a file (one per line):
  python3 harness/scripts/create_rerun_csv.py \
    --csv harness/SAMPLE/input_100.csv \
    --ids-file ids.txt \
    --out harness/SAMPLE/rerun_model.csv
"""
import argparse
import csv
import sys

csv.field_size_limit(sys.maxsize)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Source CSV (e.g. input_100.csv)")
    ap.add_argument("--out", required=True, help="Output filtered CSV path")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--ids", help="Comma-separated list of IDs to keep")
    group.add_argument("--ids-file", help="File with one ID per line")
    args = ap.parse_args()

    if args.ids:
        keep = set(x.strip() for x in args.ids.split(",") if x.strip())
    else:
        with open(args.ids_file) as f:
            keep = set(line.strip() for line in f if line.strip())

    written = 0
    with open(args.csv, newline="", encoding="utf-8") as fin, \
         open(args.out, "w", newline="", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row["ID"] in keep:
                writer.writerow(row)
                written += 1

    missing = keep - set()  # re-check which IDs were actually found
    print(f"Wrote {written}/{len(keep)} rows to {args.out}")
    if written != len(keep):
        print(f"WARNING: {len(keep) - written} requested IDs not found in source CSV")


if __name__ == "__main__":
    main()
