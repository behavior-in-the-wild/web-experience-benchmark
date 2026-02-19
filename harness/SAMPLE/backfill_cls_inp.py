#!/usr/bin/env python3
"""
Backfill input.csv with CLS_SHIFTS_MOBILE and INP_INTERACTIONS_MOBILE columns
from a final_results.json dump.

Usage:
    python backfill_cls_inp.py <final_results.json>
"""
import csv
import json
import sys
from pathlib import Path

csv.field_size_limit(10 * 1024 * 1024)

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_CSV = SCRIPT_DIR / "input.csv"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <final_results.json>")
        sys.exit(1)

    results_path = Path(sys.argv[1])
    if not results_path.exists():
        print(f"ERROR: {results_path} not found")
        sys.exit(1)

    # Load results keyed by ID
    with open(results_path) as f:
        results = json.load(f)

    lookup = {}
    for entry in results:
        entry_id = str(entry.get("ID", ""))
        cwv = entry.get("cwv_mobile") or entry.get("cwv_desktop")
        if not cwv or cwv.get("status") != "success":
            continue
        device = cwv.get("device", "mobile")
        lookup[(entry_id, device)] = {
            "cls_shifts": cwv.get("cls_shifts", []),
            "inp_interactions": cwv.get("inp_interactions", []),
        }

    print(f"Loaded {len(lookup)} entries from {results_path}")

    # Read existing CSV
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        orig_fieldnames = list(reader.fieldnames)
        rows = list(reader)

    # Add new columns if they don't exist yet
    new_cols = [
        "CLS_SHIFTS_MOBILE", "CLS_SHIFTS_DESKTOP",
        "INP_INTERACTIONS_MOBILE", "INP_INTERACTIONS_DESKTOP",
    ]
    fieldnames = list(orig_fieldnames)
    for col in new_cols:
        if col not in fieldnames:
            fieldnames.append(col)

    # Backfill
    matched = 0
    for row in rows:
        row_id = str(row.get("ID", ""))

        # Check mobile
        key_m = (row_id, "mobile")
        if key_m in lookup:
            row["CLS_SHIFTS_MOBILE"] = json.dumps(lookup[key_m]["cls_shifts"])
            row["INP_INTERACTIONS_MOBILE"] = json.dumps(lookup[key_m]["inp_interactions"])
            matched += 1

        # Check desktop
        key_d = (row_id, "desktop")
        if key_d in lookup:
            row["CLS_SHIFTS_DESKTOP"] = json.dumps(lookup[key_d]["cls_shifts"])
            row["INP_INTERACTIONS_DESKTOP"] = json.dumps(lookup[key_d]["inp_interactions"])
            matched += 1

        # Ensure empty string for missing columns
        for col in new_cols:
            if col not in row or row[col] is None:
                row[col] = ""

    # Write updated CSV
    with open(INPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Updated {INPUT_CSV}")
    print(f"  Matched {matched} entries")
    print(f"  New columns: {new_cols}")
    print(f"  Total rows: {len(rows)}")


if __name__ == "__main__":
    main()
