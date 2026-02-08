#!/usr/bin/env python3
"""
Merge CWV aggregated scores and LCP_ENTRIES from desktop_30 and mobile_30
final_results.json into final_sampled_repos.csv, outputting to repos.csv.
Matches by REPO_ID.
"""

import csv
import json
import sys
from pathlib import Path

# Handle CSV files with very large fields (e.g. webpages lists)
csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = SCRIPT_DIR
CSV_IN = SAMPLE_DIR / "final.csv"
CSV_OUT = SAMPLE_DIR / "input.csv"
DESKTOP_JSON = SAMPLE_DIR / "CWV_INIT" / "desktop" / "final_results.json"
MOBILE_JSON = SAMPLE_DIR / "CWV_INIT" / "mobile" / "final_results.json"


def load_json_by_repo(path: Path, cwv_key: str) -> dict:
    """Load JSON array and index by REPO_ID. Extract aggregated and first LCP_ENTRIES list."""
    with open(path, "r") as f:
        data = json.load(f)
    by_repo = {}
    for item in data:
        rid = item.get("REPO_ID")
        if rid is None:
            continue
        cwv = item.get(cwv_key)
        if not isinstance(cwv, dict):
            by_repo[rid] = {"aggregated": None, "LCP_ELEMENTS": None}
            continue
        agg = cwv.get("aggregated")
        lcp_entries = cwv.get("LCP_ENTRIES")
        first_list = lcp_entries[0] if isinstance(lcp_entries, list) and len(lcp_entries) > 0 else None
        by_repo[rid] = {"aggregated": agg, "LCP_ELEMENTS": first_list}
    return by_repo


def main():
    desktop = load_json_by_repo(DESKTOP_JSON, "cwv_desktop")
    mobile = load_json_by_repo(MOBILE_JSON, "cwv_mobile")

    with open(CSV_IN, "r", encoding="utf-8") as fin:
        reader = csv.DictReader(fin)
        out_columns = reader.fieldnames + [
            "LCP_ENTRIES_DESKTOP",
            "LCP_ENTRIES_MOBILE",
        ]
        rows = list(reader)

    for row in rows:
        rid = row.get("REPO_ID", "")
        d = desktop.get(rid, {})
        m = mobile.get(rid, {})

        agg_d = d.get("aggregated")
        agg_m = m.get("aggregated")
        row["CWV_DESKTOP"] = json.dumps(agg_d) if agg_d is not None else ""
        row["CWV_MOBILE"] = json.dumps(agg_m) if agg_m is not None else ""

        lcp_d = d.get("LCP_ELEMENTS")
        lcp_m = m.get("LCP_ELEMENTS")
        row["LCP_ENTRIES_DESKTOP"] = json.dumps(lcp_d) if lcp_d is not None else ""
        row["LCP_ENTRIES_MOBILE"] = json.dumps(lcp_m) if lcp_m is not None else ""

    with open(CSV_OUT, "w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=out_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {CSV_OUT}")


if __name__ == "__main__":
    main()
