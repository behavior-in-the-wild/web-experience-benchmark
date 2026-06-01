#!/usr/bin/env python3
"""
filter_csv_no_errors.py — drop rows from an evaluate.sh-style CSV where the
baseline CWV measurement failed.

A row is considered \"errored\" if either CWV_MOBILE or CWV_DESKTOP parses to a
JSON object whose top-level "status" field equals "error". Empty cells, missing
columns, or unparseable JSON are also dropped (we want only rows with a
successful baseline so $\Delta$ Health and Pareto Rate can be computed).

Default input: harness/SAMPLE/github_100.csv
Default output: harness/SAMPLE/github_47_clean.csv  (overwrites if present)

Usage:
  python3 scripts/filter_csv_no_errors.py
  python3 scripts/filter_csv_no_errors.py --in PATH --out PATH
  python3 scripts/filter_csv_no_errors.py --require both    # default: both mobile and desktop must be clean
  python3 scripts/filter_csv_no_errors.py --require either  # keep rows where EITHER device is clean
  python3 scripts/filter_csv_no_errors.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path("/dev/shm/ayush/web-experience-benchmark")
DEFAULT_IN = ROOT / "harness" / "SAMPLE" / "github_100.csv"
DEFAULT_OUT = ROOT / "harness" / "SAMPLE" / "github_47_clean.csv"


def is_clean(cell: str) -> bool:
    """Return True iff the CWV JSON blob in `cell` parses and is not status=error.

    Two schemas appear in practice:
      * Successful baselines:   {"LCP_median": ..., "LCP_p75": ..., "CLS_median": ..., ...}
        (aggregated fields at the top level; no "status" key)
      * Failed baselines:       {"status": "error", "error": "server_timeout", "runs": [], "aggregated": {}}

    We accept the first and reject the second. We also reject parse failures
    and empty cells. We require at least one CWV signal (LCP_median or LCP_p75)
    to guard against partial blobs.
    """
    if not cell:
        return False
    try:
        d = json.loads(cell)
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    if d.get("status") == "error":
        return False
    # Accept either flat or nested schema; require an LCP signal.
    if "LCP_median" in d or "LCP_p75" in d:
        return True
    agg = d.get("aggregated") or {}
    return bool(agg.get("LCP_median") or agg.get("LCP_p75"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--in", dest="src", type=Path, default=DEFAULT_IN)
    ap.add_argument("--out", dest="dst", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--require", choices=("both", "either"), default="both",
                    help="Both: mobile AND desktop must be clean (default). "
                         "Either: at least one device must be clean.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report counts without writing the output CSV.")
    args = ap.parse_args()

    if not args.src.exists():
        print(f"ERROR: input CSV not found: {args.src}", file=sys.stderr)
        sys.exit(1)

    csv.field_size_limit(10**8)
    with open(args.src, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if not fieldnames or "CWV_MOBILE" not in fieldnames or "CWV_DESKTOP" not in fieldnames:
        print(f"ERROR: CSV must have CWV_MOBILE and CWV_DESKTOP columns. Got: {fieldnames}", file=sys.stderr)
        sys.exit(1)

    kept: list[dict] = []
    drop_reasons: dict[str, int] = {"mobile_only": 0, "desktop_only": 0, "both": 0}
    for r in rows:
        m_ok = is_clean(r.get("CWV_MOBILE", ""))
        d_ok = is_clean(r.get("CWV_DESKTOP", ""))
        if args.require == "both":
            ok = m_ok and d_ok
        else:
            ok = m_ok or d_ok
        if ok:
            kept.append(r)
        else:
            if m_ok and not d_ok:
                drop_reasons["desktop_only"] += 1
            elif d_ok and not m_ok:
                drop_reasons["mobile_only"] += 1
            else:
                drop_reasons["both"] += 1

    print(f"Input:  {args.src}  ({len(rows)} rows)")
    print(f"Kept:   {len(kept)}  (rule: --require {args.require})")
    print(f"Dropped: {len(rows) - len(kept)}")
    print(f"  both devices errored:    {drop_reasons['both']}")
    print(f"  only desktop errored:    {drop_reasons['desktop_only']}")
    print(f"  only mobile errored:     {drop_reasons['mobile_only']}")

    if args.dry_run:
        print("\n[dry-run] no file written")
        return

    args.dst.parent.mkdir(parents=True, exist_ok=True)
    with open(args.dst, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in kept:
            w.writerow(r)
    print(f"\nWrote -> {args.dst}")


if __name__ == "__main__":
    main()
