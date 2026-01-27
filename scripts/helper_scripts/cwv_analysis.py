#!/usr/bin/env python3
import json
import sys

# ----------------------------
# CONFIG
# ----------------------------
FILE_A = "dumps/cwv_benchmark/mobile_1workers_20260127_170540/final_results.json"
FILE_B = "dumps/cwv_benchmark/mobile_10workers_20260127_172631/final_results.json"

METRICS = [
    "CLS_mean",
    "LCP_mean",
    "INP_mean",
    "FID_mean",
    "TTFB_mean",
]

# ----------------------------
# LOAD
# ----------------------------
with open(FILE_A, "r") as f:
    data_a = json.load(f)

with open(FILE_B, "r") as f:
    data_b = json.load(f)

# Index by REPO_ID
index_a = {row["REPO_ID"]: row for row in data_a}
index_b = {row["REPO_ID"]: row for row in data_b}

common_repos = sorted(set(index_a) & set(index_b))

if not common_repos:
    print("No overlapping REPO_IDs found.")
    sys.exit(0)

# ----------------------------
# COMPARE
# ----------------------------
for repo_id in common_repos:
    a = index_a[repo_id]
    b = index_b[repo_id]

    agg_a = a.get("cwv_mobile", {}).get("aggregated", {})
    agg_b = b.get("cwv_mobile", {}).get("aggregated", {})

    print("=" * 80)
    print(f"REPO_ID: {repo_id}")
    print(f"FRAMEWORK: {a.get('FRAMEWORK')}")

    for m in METRICS:
        va = agg_a.get(m)
        vb = agg_b.get(m)
        print(f"{m:10s} | A: {va} | B: {vb}")

print("=" * 80)
print(f"Compared {len(common_repos)} repos.")
