#!/usr/bin/env python3
"""
CWV Candidate Analysis Script
Identifies top differentiating IDs across models for benchmark selection.
"""

import csv
import json
import os
import statistics
from pathlib import Path

csv.field_size_limit(10**7)

BASE_DIR = Path("/dev/shm/ayush/web-experience-benchmark")
INPUT_CSV = BASE_DIR / "harness/SAMPLE/input_100.csv"
RESULTS_DIR = BASE_DIR / "final_result_dumps/main_bench"

# Map model dir name -> subdir suffix used
MODEL_SUFFIX = {
    "closed_cc-aider":          "template_aider",
    "closed_cc-codex":          "template_codex",
    "closed_cc-opus-4.6":       "template_claudecode",
    "closed_cc-sonnet-4.6":     "template_claudecode",
    "closed_oc_gemini-2-5-flash": "template_gemini",
    "closed_oc_gemini-2-5-pro":   "template_gemini",
    "closed_oc_gpt-4.1":        "template_opencodegpt41",
    "closed_oc_gpt-5":          "template_opencode",
    "closed_oc_gpt-5.1-codex":  "template_opencodegpt51codex",
    "open_devstral-2-123b":     "template_opencode_os",
    "open_gemma-4-31b-it":      "template_opencode_os",
    "open_glm-4.7-flash":       "template_opencode_os",
    "open_minimax-m2.7":        "template_opencode_os",
    "open_qwen3-coder-next":    "template_opencode_os",
    "open_qwen3.5-122b-a10b":   "template_opencode_os",
    "open_qwen3.5-27b":         "template_opencode_os",
    "open_qwen3.5-35b-a3b":     "template_opencode_os",
    "open_qwen3.5-397b-a17b":   "template_opencode_os",
    "open_qwen3.5-9b":          "template_opencode_os",
}

MODELS = sorted(MODEL_SUFFIX.keys())
print(f"Models ({len(MODELS)}): {MODELS}\n")

def rating_to_score(rating):
    if rating == "Good":
        return 1.0
    elif rating == "Needs Improvement":
        return 0.5
    else:  # Poor or None
        return 0.0

def cwv_score_from_aggregated(agg):
    return (rating_to_score(agg.get("LCP_rating")) +
            rating_to_score(agg.get("CLS_rating")) +
            rating_to_score(agg.get("INP_rating")))

# --- Load CSV ---
rows = {}
with open(INPUT_CSV, newline="") as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows[row["ID"]] = row

print(f"Loaded {len(rows)} rows from CSV.\n")

# --- Scan model results ---
all_data = {}  # id -> {model -> result_dict or None}

for rid in rows:
    all_data[rid] = {}

for model in MODELS:
    suffix = MODEL_SUFFIX[model]
    model_dir = RESULTS_DIR / model
    for rid in rows:
        subdir = model_dir / f"{rid}_{suffix}"
        mobile_json = subdir / "mobile.json"
        result = None
        if mobile_json.exists():
            try:
                with open(mobile_json) as f:
                    data = json.load(f)
                status = data.get("status")
                agg = data.get("aggregated", {})
                valid_runs = agg.get("valid_runs", 0)
                if status == "success" and valid_runs >= 3:
                    result = {
                        "lcp": agg.get("LCP_median"),
                        "cls": agg.get("CLS_median"),
                        "inp": agg.get("INP_median"),
                        "lcp_rating": agg.get("LCP_rating"),
                        "cls_rating": agg.get("CLS_rating"),
                        "inp_rating": agg.get("INP_rating"),
                        "valid_runs": valid_runs,
                        "score": cwv_score_from_aggregated(agg),
                    }
            except Exception as e:
                pass
        all_data[rid][model] = result

# --- Compute per-ID stats ---
summary = []

for rid, row in rows.items():
    # Parse baseline CWV_MOBILE
    try:
        cwv_mobile = json.loads(row["CWV_MOBILE"])
        baseline_lcp = cwv_mobile.get("LCP_median")
        baseline_cls = cwv_mobile.get("CLS_median")
        baseline_inp = cwv_mobile.get("INP_median")
        baseline_lcp_rating = cwv_mobile.get("LCP_rating")
        baseline_cls_rating = cwv_mobile.get("CLS_rating")
        baseline_inp_rating = cwv_mobile.get("INP_rating")
        baseline_score = (rating_to_score(baseline_lcp_rating or "Poor") +
                          rating_to_score(baseline_cls_rating or "Poor") +
                          rating_to_score(baseline_inp_rating or "Poor"))
    except Exception:
        baseline_lcp = None
        baseline_cls = None
        baseline_inp = None
        baseline_score = None

    model_results = all_data[rid]
    successful = {m: r for m, r in model_results.items() if r is not None}
    models_with_results = len(successful)

    scores = [r["score"] for r in successful.values()]

    if len(scores) >= 2:
        score_variance = statistics.variance(scores)
        score_range = max(scores) - min(scores)
        avg_score = statistics.mean(scores)
        min_score = min(scores)
        max_score = max(scores)
    elif len(scores) == 1:
        score_variance = 0.0
        score_range = 0.0
        avg_score = scores[0]
        min_score = scores[0]
        max_score = scores[0]
    else:
        score_variance = 0.0
        score_range = 0.0
        avg_score = None
        min_score = None
        max_score = None

    # baseline_is_medium: LCP > 1000ms and < 5000ms (not trivially fast, not impossibly slow)
    baseline_is_medium = (
        baseline_lcp is not None and
        not (baseline_lcp != baseline_lcp) and  # nan check
        baseline_lcp > 1000 and baseline_lcp < 5000
    )

    summary.append({
        "id": rid,
        "repo_id": row["REPO_ID"],
        "framework": row["FRAMEWORK"],
        "baseline_lcp": baseline_lcp,
        "baseline_cls": baseline_cls,
        "baseline_inp": baseline_inp,
        "baseline_score": baseline_score,
        "baseline_is_medium": baseline_is_medium,
        "models_with_results": models_with_results,
        "score_variance": score_variance,
        "score_range": score_range,
        "avg_score": avg_score,
        "min_score": min_score,
        "max_score": max_score,
    })

# --- Print coverage overview ---
print("Coverage overview (models_with_results distribution):")
from collections import Counter
dist = Counter(s["models_with_results"] for s in summary)
for k in sorted(dist.keys(), reverse=True):
    print(f"  {k:2d} models: {dist[k]:3d} IDs")
print()

print("baseline_is_medium count:", sum(1 for s in summary if s["baseline_is_medium"]))
print("models_with_results >= 12 count:", sum(1 for s in summary if s["models_with_results"] >= 12))
print("score_range >= 1.0 count:", sum(1 for s in summary if s["score_range"] >= 1.0))
print()

# --- Filter ---
candidates = [s for s in summary
              if s["models_with_results"] >= 12
              and s["score_range"] >= 1.0
              and s["baseline_is_medium"]]

candidates.sort(key=lambda x: x["score_variance"], reverse=True)

print("=" * 110)
print(f"TOP 20 CANDIDATES (sorted by score_variance desc)  [total candidates: {len(candidates)}]")
print("=" * 110)
header = f"{'ID':>6}  {'REPO_ID':>8}  {'FRAMEWORK':<18}  {'base_lcp':>9}  {'base_sc':>7}  {'n_mod':>5}  {'range':>6}  {'variance':>9}  {'min':>5}  {'max':>5}  {'avg':>5}"
print(header)
print("-" * 110)

top20 = candidates[:20]
for s in top20:
    lcp_str = f"{s['baseline_lcp']:.0f}" if s['baseline_lcp'] is not None else "N/A"
    bsc = f"{s['baseline_score']:.1f}" if s['baseline_score'] is not None else "N/A"
    print(f"{s['id']:>6}  {s['repo_id']:>8}  {s['framework']:<18}  {lcp_str:>9}  {bsc:>7}  {s['models_with_results']:>5}  {s['score_range']:>6.1f}  {s['score_variance']:>9.4f}  {s['min_score']:>5.1f}  {s['max_score']:>5.1f}  {s['avg_score']:>5.2f}")

print()

# --- Per-model score breakdown for top 10 ---
print("=" * 110)
print("PER-MODEL SCORE BREAKDOWN FOR TOP 10 IDs  (score: L=LCP, C=CLS, I=INP ratings; G=Good, N=NI, P=Poor)")
print("=" * 110)

top10 = candidates[:10]
model_short = [m.replace("closed_cc-", "CC:").replace("closed_oc_", "OC:").replace("open_", "OS:") for m in MODELS]

# Print one line per ID, one column per model
col_w = 13
header_row = f"{'ID':>6}  " + "  ".join(f"{ms:<{col_w}}" for ms in model_short)
print(header_row)
print("-" * len(header_row))

for s in top10:
    rid = s["id"]
    cells = []
    for model in MODELS:
        r = all_data[rid][model]
        if r is None:
            cells.append("  --  ")
        else:
            sc = r["score"]
            lr = (r["lcp_rating"] or "?")[0]
            cr = (r["cls_rating"] or "?")[0]
            ir = (r["inp_rating"] or "?")[0]
            cells.append(f"{sc:.1f}({lr}{cr}{ir})")
    row_str = "  ".join(f"{c:<{col_w}}" for c in cells)
    print(f"{rid:>6}  {row_str}")

print()

# --- Detailed per-metric breakdown for top 10 ---
print("=" * 110)
print("DETAILED METRIC BREAKDOWN FOR TOP 10 IDs")
print("=" * 110)
for s in top10:
    rid = s["id"]
    lcp_str = f"{s['baseline_lcp']:.0f}" if s['baseline_lcp'] is not None else "N/A"
    bsc = f"{s['baseline_score']:.1f}" if s['baseline_score'] is not None else "N/A"
    print(f"\nID={rid}  REPO={s['repo_id']}  FW={s['framework']}  baseline_lcp={lcp_str}ms  baseline_score={bsc}")
    print(f"  models_with_results={s['models_with_results']}  score_range={s['score_range']:.1f}  score_variance={s['score_variance']:.4f}  min={s['min_score']:.1f}  max={s['max_score']:.1f}  avg={s['avg_score']:.2f}")
    print(f"  {'Model':<35}  {'Score':>5}  {'LCP_ms':>8}  {'LCP_rating':>14}  {'CLS':>10}  {'CLS_rating':>14}  {'INP_ms':>8}  {'INP_rating':>14}  {'runs':>4}")
    for model in MODELS:
        r = all_data[rid][model]
        if r is None:
            print(f"  {model:<35}  {'--':>5}")
        else:
            cls_str = f"{r['cls']:.5f}" if r['cls'] is not None else "N/A"
            print(f"  {model:<35}  {r['score']:>5.1f}  {r['lcp'] or 0:>8.0f}  {(r['lcp_rating'] or '?'):>14}  {cls_str:>10}  {(r['cls_rating'] or '?'):>14}  {r['inp'] or 0:>8.0f}  {(r['inp_rating'] or '?'):>14}  {r['valid_runs']:>4}")

print()
print(f"\nSummary: {len(candidates)} candidates pass all filters out of {len(summary)} total IDs.")
