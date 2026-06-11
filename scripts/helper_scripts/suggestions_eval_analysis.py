#!/usr/bin/env python3
"""
Suggestions Eval Analysis
Compares baseline CWV (from input CSV) vs patched CWV (from result dirs) for each
(model, row_id, suggestion_idx) triple.

Outputs:
  - per_job results CSV
  - summary table per model
  - best-suggestion-per-site table (pareto-style)

Usage:
  python scripts/helper_scripts/suggestions_eval_analysis.py
  python scripts/helper_scripts/suggestions_eval_analysis.py --out results/sugg_eval.csv
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from statistics import mean

csv.field_size_limit(10 ** 7)

BASE_DIR = Path("/dev/shm/ayush/web-experience-benchmark")
INPUT_CSV = BASE_DIR / "harness/SAMPLE/input_100.csv"
SUGG_EVAL_DIR = BASE_DIR / "final_result_dumps/direct_suggestion_eval"

MODELS = ["gemma-4-31b-it", "minimax-m2.7", "qwen3.5-27b"]

METRICS = [
    ("lcp_mobile",  "LCP_median", "mobile"),
    ("lcp_desktop", "LCP_median", "desktop"),
    ("cls_mobile",  "CLS_median", "mobile"),
    ("cls_desktop", "CLS_median", "desktop"),
    ("inp_mobile",  "INP_median", "mobile"),
    ("inp_desktop", "INP_median", "desktop"),
]

# Good thresholds for clamping health
GOOD = {"lcp": 2500, "cls": 0.1, "inp": 200}
NI   = {"lcp": 4000, "cls": 0.25, "inp": 500}


def health_score(v, metric):
    g, n = GOOD[metric], NI[metric]
    if v is None:
        return None
    if v <= g:
        return 100.0
    if v >= 2 * n:
        return 0.0
    if v <= n:
        return 100.0 - 50.0 * (v - g) / (n - g)
    return 50.0 * (2 * n - v) / n


def pct_change(before, after):
    """Negative = improvement for LCP/INP/CLS (lower is better)."""
    if before is None or after is None or before == 0:
        return None
    return (after - before) / before * 100.0


def load_baselines():
    baselines = {}
    with open(INPUT_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rid = row["ID"].strip()
            try:
                mob = json.loads(row["CWV_MOBILE"]) if row.get("CWV_MOBILE", "").strip() else {}
                dsk = json.loads(row["CWV_DESKTOP"]) if row.get("CWV_DESKTOP", "").strip() else {}
            except Exception:
                mob, dsk = {}, {}
            baselines[rid] = {"mobile": mob, "desktop": dsk}
    return baselines


def load_result(result_dir: Path):
    """Load patched CWV + visual + suggestion from a result dir."""
    def load_json(name):
        p = result_dir / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None

    mobile  = load_json("mobile.json")
    desktop = load_json("desktop.json")
    visual  = load_json("visual.json")
    sugg    = load_json("input_suggestion.json")

    # Check patch non-empty
    patch_files = list(result_dir.glob("*.patch")) + list(result_dir.glob("*.diff"))
    patch_nonempty = any(p.stat().st_size > 0 for p in patch_files)

    mobile_agg  = mobile.get("aggregated", {})  if mobile  else {}
    desktop_agg = desktop.get("aggregated", {}) if desktop else {}

    vis_ok = visual.get("is_valid") if visual else None

    return {
        "mobile":  mobile_agg,
        "desktop": desktop_agg,
        "visual_valid": vis_ok,
        "patch_nonempty": patch_nonempty,
        "suggestion_metric": sugg.get("metric") if sugg else None,
        "suggestion_title":  sugg.get("title")  if sugg else None,
    }


def analyze_model(model: str, baselines: dict):
    results_dir = SUGG_EVAL_DIR / model / "results"
    if not results_dir.exists():
        return []

    rows = []
    for job_dir in sorted(results_dir.iterdir()):
        if not job_dir.is_dir():
            continue
        name = job_dir.name  # e.g. 998_s2_template_opencode_os_direct
        parts = name.split("_")
        if len(parts) < 2:
            continue
        row_id = parts[0]
        sugg_idx = parts[1] if len(parts) > 1 else "s?"

        baseline = baselines.get(row_id, {})
        result   = load_result(job_dir)

        row = {
            "model":     model,
            "row_id":    row_id,
            "sugg_idx":  sugg_idx,
            "job_dir":   name,
            "suggestion_metric": result["suggestion_metric"],
            "suggestion_title":  result["suggestion_title"],
            "patch_nonempty":    result["patch_nonempty"],
            "visual_valid":      result["visual_valid"],
        }

        for key, agg_key, device in METRICS:
            metric_name = key.split("_")[0]  # lcp, cls, inp
            before = baseline.get(device, {}).get(agg_key)
            after  = result[device].get(agg_key)
            row[f"before_{key}"] = before
            row[f"after_{key}"]  = after
            row[f"pct_{key}"]    = pct_change(before, after)
            row[f"health_before_{key}"] = health_score(before, metric_name)
            row[f"health_after_{key}"]  = health_score(after,  metric_name)

        rows.append(row)
    return rows


def print_summary(all_rows):
    print("\n" + "=" * 72)
    print(f"{'SUGGESTIONS EVAL — MODEL SUMMARY':^72}")
    print("=" * 72)

    for model in MODELS:
        rows = [r for r in all_rows if r["model"] == model]
        if not rows:
            continue

        valid     = [r for r in rows if r["patch_nonempty"]]
        vis_ok    = [r for r in rows if r["visual_valid"] is True]
        vis_fail  = [r for r in rows if r["visual_valid"] is False]

        print(f"\n  {model}  ({len(rows)} jobs, {len(valid)} non-empty patches, "
              f"{len(vis_ok)} visual OK, {len(vis_fail)} visual fail)")

        for key, agg_key, device in METRICS:
            pct_col = f"pct_{key}"
            vals = [r[pct_col] for r in rows if r[pct_col] is not None and r["patch_nonempty"]]
            if not vals:
                continue
            improved = [v for v in vals if v < -0.5]   # >0.5% improvement
            degraded = [v for v in vals if v >  0.5]
            print(f"    {key:20s}  median={_median(vals):+6.1f}%  "
                  f"improved={len(improved):3d}/{len(vals)}  "
                  f"degraded={len(degraded):3d}/{len(vals)}")


def print_best_per_site(all_rows):
    """For each (model, row_id): which suggestion gave the best LCP_mobile improvement?"""
    print("\n" + "=" * 72)
    print(f"{'BEST SUGGESTION PER SITE (LCP mobile, non-empty patches)':^72}")
    print("=" * 72)

    for model in MODELS:
        rows = [r for r in all_rows if r["model"] == model and r["patch_nonempty"]]
        by_site = {}
        for r in rows:
            rid = r["row_id"]
            pct = r.get("pct_lcp_mobile")
            if pct is None:
                continue
            if rid not in by_site or pct < by_site[rid]["pct"]:
                by_site[rid] = {"pct": pct, "sugg": r["sugg_idx"], "metric": r["suggestion_metric"]}

        improved = sum(1 for v in by_site.values() if v["pct"] < -1)
        degraded = sum(1 for v in by_site.values() if v["pct"] >  1)
        total    = len(by_site)
        medians  = sorted(v["pct"] for v in by_site.values())
        med      = _median(medians) if medians else None

        print(f"\n  {model}: {total} sites with LCP data | "
              f"best-sugg improved={improved} degraded={degraded} | "
              f"median best-sugg LCP change={med:+.1f}%" if med is not None else
              f"\n  {model}: {total} sites with LCP data")


def _median(vals):
    if not vals:
        return float("nan")
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(BASE_DIR / "results/sugg_eval_results.csv"),
                        help="Output CSV path")
    parser.add_argument("--models", nargs="+", default=MODELS)
    args = parser.parse_args()

    print("Loading baselines from input CSV...")
    baselines = load_baselines()
    print(f"  {len(baselines)} rows loaded.")

    all_rows = []
    for model in args.models:
        print(f"Analyzing {model}...")
        rows = analyze_model(model, baselines)
        all_rows.extend(rows)
        print(f"  {len(rows)} jobs")

    # Write CSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if all_rows:
        cols = list(all_rows[0].keys())
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nWrote {len(all_rows)} rows to {out_path}")

    print_summary(all_rows)
    print_best_per_site(all_rows)


if __name__ == "__main__":
    main()
