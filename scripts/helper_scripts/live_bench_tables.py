"""
Compute main metrics table + extended per-metric delta table for live bench.
Mirrors the paper's table format.

Pareto definition (matching static bench paper):
  A patch is Pareto-improving if:
    - no visual regression
    - delta_LCP_mobile < 0  (LCP improved)
    AND delta_CLS_mobile <= threshold (not worsened, threshold = +0.1)
    AND delta_INP_mobile <= threshold (not worsened, threshold = +50ms)

Pareto rate = pareto / (pareto + no_improve)   [excludes visual regressions]
"""

import json, os, re
from pathlib import Path
from collections import defaultdict

LIVE_ROOT = Path("harness_live_bench/out/suggestions_eval/20260604_034545")
MODELS = ["gemma-4-31b-it", "minimax-m2.7", "qwen3.5-27b"]
MODEL_LABELS = {
    "gemma-4-31b-it": "Gemma-4-31B",
    "minimax-m2.7":   "MiniMax-M2.7",
    "qwen3.5-27b":    "Qwen3.5-27B",
}

CLS_THRESH = 0.1
INP_THRESH = 50.0

def load_agg(path):
    try:
        d = json.loads(Path(path).read_text())
        if d.get("status") != "success":
            return None
        a = d.get("aggregated", {})
        if not a or a.get("valid_runs", 0) < 1:
            return None
        return a
    except Exception:
        return None

def is_pareto(b_mob, p_mob):
    """True if patch improves LCP and doesn't worsen CLS or INP vs baseline."""
    if b_mob is None or p_mob is None:
        return None
    delta_lcp = p_mob["LCP_median"] - b_mob["LCP_median"]
    delta_cls = p_mob["CLS_median"] - b_mob["CLS_median"]
    delta_inp = (p_mob.get("INP_median") or 0) - (b_mob.get("INP_median") or 0)
    if delta_lcp < 0 and delta_cls <= CLS_THRESH and delta_inp <= INP_THRESH:
        return True
    return False

results = {}

for model in MODELS:
    model_dir = LIVE_ROOT / model / "results"
    stats = {
        "total": 0,
        "has_cwv": 0,
        "visual_regression": 0,
        "empty_patch": 0,
        "pareto": 0,
        "no_improve": 0,
        # delta accumulators (non-regressed, CWV available)
        "d_lcp_mob": [], "d_cls_mob": [], "d_inp_mob": [],
        "d_lcp_dsk": [], "d_cls_dsk": [], "d_inp_dsk": [],
        # pareto-only deltas
        "p_lcp_mob": [], "p_cls_mob": [], "p_inp_mob": [],
        "p_lcp_dsk": [], "p_cls_dsk": [], "p_inp_dsk": [],
    }

    for rd in sorted(model_dir.iterdir()):
        if not rd.is_dir():
            continue
        stats["total"] += 1

        # visual regression
        vjson = rd / "visual.json"
        if vjson.exists():
            try:
                v = json.loads(vjson.read_text())
                if v.get("overall_regression") is True:
                    stats["visual_regression"] += 1
                    continue
            except Exception:
                pass

        # patch empty?
        patch_files = list(rd.glob("*.patch"))
        patch_empty = not patch_files or all(p.stat().st_size == 0 for p in patch_files)
        if patch_empty:
            stats["empty_patch"] += 1

        # CWV data
        b_mob = load_agg(rd / "baseline_mobile.json")
        p_mob = load_agg(rd / "mobile.json")
        b_dsk = load_agg(rd / "baseline_desktop.json")
        p_dsk = load_agg(rd / "desktop.json")

        if p_mob is None:
            continue
        stats["has_cwv"] += 1

        # deltas (patched - baseline), skip if no baseline
        if b_mob is not None:
            dl = p_mob["LCP_median"] - b_mob["LCP_median"]
            dc = p_mob["CLS_median"] - b_mob["CLS_median"]
            di = (p_mob.get("INP_median") or 0) - (b_mob.get("INP_median") or 0)
            stats["d_lcp_mob"].append(dl)
            stats["d_cls_mob"].append(dc)
            stats["d_inp_mob"].append(di)

            prt = is_pareto(b_mob, p_mob)
            if prt:
                stats["pareto"] += 1
                stats["p_lcp_mob"].append(dl)
                stats["p_cls_mob"].append(dc)
                stats["p_inp_mob"].append(di)
            else:
                stats["no_improve"] += 1

        if b_dsk is not None and p_dsk is not None:
            dl = p_dsk["LCP_median"] - b_dsk["LCP_median"]
            dc = p_dsk["CLS_median"] - b_dsk["CLS_median"]
            di = (p_dsk.get("INP_median") or 0) - (b_dsk.get("INP_median") or 0)
            stats["d_lcp_dsk"].append(dl)
            stats["d_cls_dsk"].append(dc)
            stats["d_inp_dsk"].append(di)

    results[model] = stats

def mean(lst):
    return sum(lst) / len(lst) if lst else float("nan")

def fmt_ms(v):
    if v != v:  # nan
        return "—"
    return f"{v:+.0f}"

def fmt_cls(v):
    if v != v:
        return "—"
    return f"{v:+.4f}"

# ─── Table 1: Main metrics ───────────────────────────────────────────────────
print("=" * 70)
print("TABLE 1 — Live Bench: Main Metrics")
print("=" * 70)
print(f"{'Model':<16} {'Agent':<10} {'LLM':<16} {'Dirs':>5} {'No-regr':>8} {'CWV':>5} {'Pareto':>7} {'ParRate':>8}")
print("-" * 70)

for model in MODELS:
    s = results[model]
    denom = s["pareto"] + s["no_improve"]
    par_rate = s["pareto"] / denom if denom > 0 else float("nan")
    label = MODEL_LABELS[model]
    print(f"{label:<16} {'OpenCode':<10} {model:<16} "
          f"{s['total']:>5} {s['total']-s['visual_regression']:>8} "
          f"{s['has_cwv']:>5} {s['pareto']:>7} {par_rate*100:>7.1f}%")

print()
print("Columns: Dirs=total result dirs, No-regr=passed visual check,")
print("         CWV=has patched CWV data, Pareto=LCP improved & CLS/INP not worsened")
print("         ParRate = Pareto / (Pareto + No-improve)")

# ─── Table 2: Extended per-metric deltas ────────────────────────────────────
print()
print("=" * 90)
print("TABLE 2 — Live Bench: Per-Metric Deltas (median over non-regressed patches with baseline CWV)")
print("=" * 90)
print(f"{'Model':<16} | {'ΔLCP_mob':>9} {'ΔCLS_mob':>10} {'ΔINP_mob':>9} | {'ΔLCP_dsk':>9} {'ΔCLS_dsk':>10} {'ΔINP_dsk':>9} | {'N_mob':>6} {'N_dsk':>6}")
print("-" * 90)

for model in MODELS:
    s = results[model]
    label = MODEL_LABELS[model]
    print(f"{label:<16} | "
          f"{fmt_ms(mean(s['d_lcp_mob'])):>9} "
          f"{fmt_cls(mean(s['d_cls_mob'])):>10} "
          f"{fmt_ms(mean(s['d_inp_mob'])):>9} | "
          f"{fmt_ms(mean(s['d_lcp_dsk'])):>9} "
          f"{fmt_cls(mean(s['d_cls_dsk'])):>10} "
          f"{fmt_ms(mean(s['d_inp_dsk'])):>9} | "
          f"{len(s['d_lcp_mob']):>6} {len(s['d_lcp_dsk']):>6}")

print()
print("Delta = patched_median - baseline_median. Negative = improvement.")
print("Only rows with both baseline and patched CWV and no visual regression included.")

# ─── Bonus: Pareto-only deltas ───────────────────────────────────────────────
print()
print("=" * 70)
print("TABLE 2b — Pareto patches only: mean delta LCP/CLS/INP (mobile)")
print("=" * 70)
print(f"{'Model':<16} | {'ΔLCP_mob':>9} {'ΔCLS_mob':>10} {'ΔINP_mob':>9} | {'N_pareto':>9}")
print("-" * 70)
for model in MODELS:
    s = results[model]
    label = MODEL_LABELS[model]
    print(f"{label:<16} | "
          f"{fmt_ms(mean(s['p_lcp_mob'])):>9} "
          f"{fmt_cls(mean(s['p_cls_mob'])):>10} "
          f"{fmt_ms(mean(s['p_inp_mob'])):>9} | "
          f"{len(s['p_lcp_mob']):>9}")
