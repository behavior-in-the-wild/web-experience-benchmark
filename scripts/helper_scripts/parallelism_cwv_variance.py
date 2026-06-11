"""
For each template (site ID), collect LCP_median across all model runs.
Map each model to its parallelism setting.
Compute per-template std dev within each parallelism group.
Then show how std dev changes as parallelism increases.
"""
import json, os, re, statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[2] / "final_result_dumps/main_bench"
EXCLUDE = set()

# Parallelism used per model (from runner defaults / logs)
MODEL_PARALLEL = {
    "closed_cc-aider":        4,
    "closed_cc-codex":        4,
    "closed_cc-opus-4.6":     5,
    "closed_cc-sonnet-4.6":   5,
    "closed_oc_gemini-2-5-flash": 5,
    "closed_oc_gemini-2-5-pro":   5,
    "closed_oc_gpt-4.1":      5,
    "closed_oc_gpt-5":        5,
    "closed_oc_gpt-5.1-codex": 5,
    "open_devstral-2-123b":   16,
    "open_gemma-4-31b-it":    16,
    "open_glm-4.7-flash":     16,
    "open_minimax-m2.7":      16,
    "open_qwen3.5-122b-a10b": 16,
    "open_qwen3.5-27b":       16,
    "open_qwen3.5-35b-a3b":   16,
    "open_qwen3.5-397b-a17b": 16,
    "open_qwen3.5-9b":        16,
    "open_qwen3-coder-next":  16,
}

# template_id -> { parallel -> [lcp_median, ...] }
data = defaultdict(lambda: defaultdict(list))

for model_dir in sorted(ROOT.iterdir()):
    if model_dir.name in EXCLUDE or not model_dir.is_dir():
        continue
    parallel = MODEL_PARALLEL.get(model_dir.name)
    if parallel is None:
        continue
    for result_dir in model_dir.iterdir():
        if not result_dir.is_dir():
            continue
        m = re.match(r"^(\d+)_", result_dir.name)
        if not m:
            continue
        template_id = m.group(1)
        mjson = result_dir / "mobile.json"
        if not mjson.exists():
            continue
        try:
            d = json.loads(mjson.read_text())
            lcp = d.get("aggregated", {}).get("LCP_median")
            if lcp is not None and d.get("aggregated", {}).get("valid_runs", 0) >= 3:
                data[template_id][parallel].append(lcp)
        except Exception:
            continue

# For each template, compute std dev per parallelism group
# then aggregate: mean std dev per parallelism level across all templates

# parallel -> list of per-template stdevs
parallel_stdevs = defaultdict(list)
# per template: parallel -> stdev (need >=2 values)
per_template = {}

for tid, pdict in data.items():
    row = {}
    for par, lcps in pdict.items():
        if len(lcps) >= 2:
            row[par] = statistics.stdev(lcps)
            parallel_stdevs[par].extend([statistics.stdev(lcps)])
    if row:
        per_template[tid] = row

print(f"Templates with >=2 models per parallelism group: {len(per_template)}")
print()

# Summary: mean/median stdev per parallelism level
print(f"{'Parallel':>10} | {'N templates':>12} | {'Mean stdev LCP':>15} | {'Median stdev LCP':>17} | {'p25':>8} | {'p75':>8}")
print("-" * 80)
for par in sorted(parallel_stdevs):
    vals = parallel_stdevs[par]
    vals_s = sorted(vals)
    n = len(vals)
    mean = statistics.mean(vals)
    med = statistics.median(vals)
    p25 = vals_s[n // 4]
    p75 = vals_s[3 * n // 4]
    print(f"{par:>10} | {n:>12} | {mean:>15.1f} | {med:>17.1f} | {p25:>8.1f} | {p75:>8.1f}")

print()

# Cross-parallelism comparison: templates that appear in BOTH p=4/5 AND p=16
templates_both = {
    tid for tid, row in per_template.items()
    if any(p in (4, 5) for p in row) and 16 in row
}
print(f"Templates with stdev in both low-parallel (4/5) AND high-parallel (16): {len(templates_both)}")

if templates_both:
    low_stdevs = []
    high_stdevs = []
    for tid in templates_both:
        row = per_template[tid]
        low = [v for p, v in row.items() if p in (4, 5)]
        high = [v for p, v in row.items() if p == 16]
        if low and high:
            low_stdevs.append(statistics.mean(low))
            high_stdevs.append(statistics.mean(high))
    print(f"Mean stdev LCP at parallel=4/5:  {statistics.mean(low_stdevs):.1f} ms")
    print(f"Mean stdev LCP at parallel=16:   {statistics.mean(high_stdevs):.1f} ms")
    ratio = statistics.mean(high_stdevs) / statistics.mean(low_stdevs) if statistics.mean(low_stdevs) > 0 else float('nan')
    print(f"Ratio (high/low):                {ratio:.2f}x")

print()
# Also look at within-run stdev (already stored in aggregated.LCP_stdev)
# This is the variance from 5 repeated runs on the SAME model at some parallelism
print("=== Within-run LCP stdev (5 runs same patch) ===")
within_stdevs = defaultdict(list)
for model_dir in sorted(ROOT.iterdir()):
    if model_dir.name in EXCLUDE or not model_dir.is_dir():
        continue
    parallel = MODEL_PARALLEL.get(model_dir.name)
    if parallel is None:
        continue
    for result_dir in model_dir.iterdir():
        if not result_dir.is_dir():
            continue
        mjson = result_dir / "mobile.json"
        if not mjson.exists():
            continue
        try:
            d = json.loads(mjson.read_text())
            agg = d.get("aggregated", {})
            stdev = agg.get("LCP_stdev")
            valid = agg.get("valid_runs", 0)
            if stdev is not None and valid >= 3:
                within_stdevs[parallel].append(stdev)
        except Exception:
            continue

print(f"{'Parallel':>10} | {'N':>6} | {'Mean within-run stdev':>22} | {'Median':>8}")
print("-" * 60)
for par in sorted(within_stdevs):
    vals = within_stdevs[par]
    print(f"{par:>10} | {len(vals):>6} | {statistics.mean(vals):>22.1f} | {statistics.median(vals):>8.1f}")
