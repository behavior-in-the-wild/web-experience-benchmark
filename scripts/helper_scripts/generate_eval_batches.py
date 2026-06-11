#!/usr/bin/env python3
"""
Generate 10 curated CSV files for benchmarking, saved in harness/SAMPLE/.
Each CSV has 5 rows selected from candidates that best differentiate models.
"""

import csv
import json
import os
import statistics
from collections import defaultdict

csv.field_size_limit(10**7)

BASE = "/dev/shm/ayush/web-experience-benchmark"
SAMPLE_DIR = os.path.join(BASE, "harness/SAMPLE")
RESULTS_DIR = os.path.join(BASE, "final_result_dumps")
OUTPUT_DIR = SAMPLE_DIR

INPUT_100 = os.path.join(SAMPLE_DIR, "input_100.csv")
INPUT_300 = os.path.join(SAMPLE_DIR, "input_300.csv")

# ---------------------------------------------------------------------------
# Step 1: Load baseline rows from both CSVs; input_100.csv takes precedence
# ---------------------------------------------------------------------------

def load_csv(path):
    rows = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows[row["ID"]] = row
    return rows

print("Loading input CSVs...")
rows_100 = load_csv(INPUT_100)
rows_300 = load_csv(INPUT_300)

# Merge: input_300 first, then overwrite with input_100
baseline = {}
baseline.update(rows_300)
baseline.update(rows_100)
print(f"  input_100.csv: {len(rows_100)} rows")
print(f"  input_300.csv: {len(rows_300)} rows")
print(f"  Combined (unique IDs): {len(baseline)} rows")

# ---------------------------------------------------------------------------
# Step 2: Scan all model result dirs, load mobile.json per subdir
# ---------------------------------------------------------------------------

models = sorted(os.listdir(RESULTS_DIR))
print(f"\nScanning {len(models)} model dirs...")

# model_data[model][id] = aggregated dict or None
model_data = {}

for model in models:
    model_dir = os.path.join(RESULTS_DIR, model)
    if not os.path.isdir(model_dir):
        continue
    model_data[model] = {}
    subdirs = os.listdir(model_dir)
    for subdir in subdirs:
        # Parse ID: numeric prefix before first '_'
        parts = subdir.split("_")
        if not parts[0].isdigit():
            continue
        rid = parts[0]
        mobile_json = os.path.join(model_dir, subdir, "mobile.json")
        if not os.path.exists(mobile_json):
            model_data[model][rid] = None
            continue
        try:
            with open(mobile_json) as f:
                data = json.load(f)
            model_data[model][rid] = data
        except Exception as e:
            model_data[model][rid] = None
    print(f"  {model}: {len(model_data[model])} subdirs")

# ---------------------------------------------------------------------------
# Step 3: Per-ID metrics
# ---------------------------------------------------------------------------

def rating_score(rating):
    if rating == "Good":
        return 1.0
    elif rating == "Needs Improvement":
        return 0.5
    else:  # Poor or unknown
        return 0.0

def cwv_score_from_agg(agg):
    return (
        rating_score(agg.get("LCP_rating", "Poor")) +
        rating_score(agg.get("CLS_rating", "Poor")) +
        rating_score(agg.get("INP_rating", "Poor"))
    )

def parse_baseline_cwv(cwv_mobile_str):
    """Parse CWV_MOBILE JSON string and return (lcp_median, lcp_rating)."""
    if not cwv_mobile_str:
        return None, None
    try:
        d = json.loads(cwv_mobile_str)
        return d.get("LCP_median"), d.get("LCP_rating")
    except Exception:
        return None, None

print("\nComputing per-ID metrics...")

# Collect all IDs that appear in ANY model result
all_ids = set()
for model in model_data:
    all_ids.update(model_data[model].keys())
print(f"  Total unique IDs across all models: {len(all_ids)}")

# Only consider IDs that have baseline data
candidate_ids = all_ids & set(baseline.keys())
print(f"  IDs with baseline data: {len(candidate_ids)}")

id_metrics = {}

for rid in candidate_ids:
    scores = []
    n_success = 0

    for model in model_data:
        entry = model_data[model].get(rid)
        if entry is None:
            continue
        status = entry.get("status")
        agg = entry.get("aggregated")
        if agg is None:
            continue
        valid_runs = agg.get("valid_runs", 0)
        if status == "success" and valid_runs >= 3:
            n_success += 1
            scores.append(cwv_score_from_agg(agg))

    if n_success < 2:
        # Not enough data to compute variance
        continue

    score_range = max(scores) - min(scores)
    score_var = statistics.variance(scores) if len(scores) >= 2 else 0.0

    row = baseline[rid]
    baseline_lcp, baseline_lcp_rating = parse_baseline_cwv(row.get("CWV_MOBILE", ""))

    id_metrics[rid] = {
        "n_models_success": n_success,
        "scores": scores,
        "score_range": score_range,
        "score_variance": score_var,
        "baseline_lcp": baseline_lcp,
        "baseline_lcp_rating": baseline_lcp_rating,
        "framework": row.get("FRAMEWORK", ""),
        "repo_id": row.get("REPO_ID", ""),
    }

print(f"  IDs with sufficient model data: {len(id_metrics)}")

# ---------------------------------------------------------------------------
# Step 4: Filter to good differentiating samples
# ---------------------------------------------------------------------------

filtered = []
reasons = defaultdict(int)

for rid, m in id_metrics.items():
    if m["n_models_success"] < 10:
        reasons["n_models_success < 10"] += 1
        continue
    if m["score_range"] < 1.0:
        reasons["score_range < 1.0"] += 1
        continue
    lcp = m["baseline_lcp"]
    if lcp is None or not (1000 <= lcp <= 6000):
        reasons["baseline_lcp out of [1000,6000]"] += 1
        continue
    if m["baseline_lcp_rating"] not in ("Needs Improvement", "Poor"):
        reasons["baseline_lcp_rating not NI/Poor"] += 1
        continue
    filtered.append(rid)

print(f"\nFiltering results:")
for reason, count in sorted(reasons.items()):
    print(f"  Excluded ({reason}): {count}")
print(f"  Total candidates after filtering: {len(filtered)}")

# Sort by score_variance descending
filtered.sort(key=lambda rid: id_metrics[rid]["score_variance"], reverse=True)

# ---------------------------------------------------------------------------
# Step 5: Assign rows to 10 batches of 5 each
# ---------------------------------------------------------------------------

BATCH_SIZE = 5
N_BATCHES = 10
TOTAL_NEEDED = BATCH_SIZE * N_BATCHES

if len(filtered) < TOTAL_NEEDED:
    print(f"\nWARNING: Only {len(filtered)} candidates available, need {TOTAL_NEEDED}.")
    print("Will reuse rows across batches if necessary, or reduce batch size.")

# Build pools
# Batches 1-3: top score_variance
# Batches 4-6: diverse frameworks (Hugo, Hexo, Jekyll, then Static HTML)
# Batches 7-10: remaining high-range rows

used_ids = set()
batches = [[] for _ in range(N_BATCHES)]

def pick_diverse(pool, n, used, prefer_frameworks=None):
    """Pick n items from pool (list of rids), preferring diversity of frameworks."""
    picked = []
    fw_used = defaultdict(int)
    # First pass: prefer_frameworks order
    remaining = [r for r in pool if r not in used]
    if prefer_frameworks:
        for fw in prefer_frameworks:
            for r in remaining:
                if r not in used and r not in picked and id_metrics[r]["framework"] == fw:
                    if len(picked) < n:
                        picked.append(r)
                        fw_used[fw] += 1
    # Fill remaining slots with diverse frameworks
    for r in remaining:
        if len(picked) >= n:
            break
        if r not in picked:
            picked.append(r)
    return picked[:n]

# Batches 0-2 (1-3): top variance
top_pool = filtered.copy()
for i in range(3):
    available = [r for r in top_pool if r not in used_ids]
    batch = pick_diverse(available, BATCH_SIZE, used_ids)
    batches[i] = batch
    used_ids.update(batch)

# Batches 3-5 (4-6): diverse frameworks
diverse_frameworks = ["Hugo", "Hexo", "Jekyll", "Static HTML", "Next.js", "Gatsby"]
for i in range(3, 6):
    available = [r for r in filtered if r not in used_ids]
    batch = pick_diverse(available, BATCH_SIZE, used_ids, prefer_frameworks=diverse_frameworks)
    batches[i] = batch
    used_ids.update(batch)

# Batches 6-9 (7-10): remaining sorted by score_range
remaining_pool = [r for r in filtered if r not in used_ids]
# Sort by score_range desc
remaining_pool.sort(key=lambda r: id_metrics[r]["score_range"], reverse=True)
for i in range(6, 10):
    available = [r for r in remaining_pool if r not in used_ids]
    batch = pick_diverse(available, BATCH_SIZE, used_ids)
    batches[i] = batch
    used_ids.update(batch)

# If we don't have enough unique rows, allow reuse to fill batches
if len(filtered) < TOTAL_NEEDED:
    all_sorted = filtered.copy()  # already sorted by variance
    idx = 0
    for i in range(10):
        while len(batches[i]) < BATCH_SIZE and idx < len(all_sorted):
            r = all_sorted[idx % len(all_sorted)]
            idx += 1
            if r not in batches[i]:
                batches[i].append(r)
        if len(batches[i]) < BATCH_SIZE:
            # cycle through
            for r in all_sorted:
                if r not in batches[i]:
                    batches[i].append(r)
                if len(batches[i]) >= BATCH_SIZE:
                    break

# ---------------------------------------------------------------------------
# Step 6: Write CSV files
# ---------------------------------------------------------------------------

COLUMNS = [
    "ID", "REPO_ID", "FRAMEWORK", "COMMIT_ID", "ZIP_REPO_PATH", "HOST_FILE_PATH",
    "CWV_MOBILE", "CWV_DESKTOP", "LCP_ENTRIES_MOBILE", "LCP_ENTRIES_DESKTOP",
    "CLS_SHIFTS_MOBILE", "CLS_SHIFTS_DESKTOP", "INP_INTERACTIONS_MOBILE", "INP_INTERACTIONS_DESKTOP"
]

os.makedirs(OUTPUT_DIR, exist_ok=True)

print(f"\nWriting {N_BATCHES} CSV files to {OUTPUT_DIR}...")
for i, batch in enumerate(batches):
    fname = f"eval_batch_{i+1:02d}.csv"
    fpath = os.path.join(OUTPUT_DIR, fname)
    with open(fpath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for rid in batch:
            row = baseline[rid]
            writer.writerow({col: row.get(col, "") for col in COLUMNS})
    print(f"  Written: {fname} ({len(batch)} rows)")

# ---------------------------------------------------------------------------
# Step 7: Summary
# ---------------------------------------------------------------------------

print(f"\n{'='*80}")
print(f"SUMMARY: {len(filtered)} total candidates")
print(f"{'='*80}")
print(f"{'Batch':<8} {'ID':<8} {'REPO_ID':<30} {'FRAMEWORK':<18} {'Baseline LCP':>12} {'Score Range':>12} {'Score Var':>10}")
print(f"{'-'*8} {'-'*8} {'-'*30} {'-'*18} {'-'*12} {'-'*12} {'-'*10}")

for i, batch in enumerate(batches):
    for j, rid in enumerate(batch):
        m = id_metrics[rid]
        prefix = f"Batch {i+1:02d}" if j == 0 else " " * 8
        lcp_str = f"{m['baseline_lcp']:.0f}ms" if m['baseline_lcp'] else "N/A"
        print(f"{prefix:<8} {rid:<8} {m['repo_id'][:30]:<30} {m['framework'][:18]:<18} {lcp_str:>12} {m['score_range']:>12.2f} {m['score_variance']:>10.4f}")
    print()

print(f"\nDone. Files saved to: {OUTPUT_DIR}")
