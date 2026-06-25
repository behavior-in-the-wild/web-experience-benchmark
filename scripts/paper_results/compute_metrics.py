#!/usr/bin/env python3
"""
Compute paper metrics for each model using final_result_dumps/ as the
canonical data source.

CWV metrics (Site Health, Pareto, Net Threshold Crossing, …) are computed
against cwv_baseline_scores/ as the un-patched reference for every row with
valid baseline and patched CWV, including rows flagged as visually regressed.

Regression % uses the following logic per job:
  - Count valid checks (structural / jaccard_text / gpt_visual / console_errors
    that returned True or False, not null/error).
  - If 0 valid checks  → exclude job from regression denominator entirely.
  - If 1 valid check   → take that single check's result.
  - If ≥2 valid checks → regressed only if ≥2 checks return True.

Outputs:
  paper_writing/data/final_per_site.csv         — full per-site rows
  paper_writing/data/final_summary.json         — aggregates per model
  paper_writing/tables/final_main.tex           — main results table
  paper_writing/tables/final_per_metric.tex     — per-metric delta table

Usage:
  python paper_writing/scripts/compute_metrics.py [--print-tables]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT         = Path("/dev/shm/ayush/web-experience-benchmark")
DEFAULT_BASELINE_DIR = ROOT / "final_result_dumps" / "baselines" / "cwv_baseline_scores_p20"
DEFAULT_FINAL_DUMPS  = ROOT / "final_result_dumps" / "main_bench_rerun_20260615"
DEFAULT_V1_VISUAL_DUMPS = ROOT / "final_result_dumps" / "main_bench"
BASELINE_DIR = Path(os.environ.get("PAPER_BASELINE_DIR", DEFAULT_BASELINE_DIR))
FINAL_DUMPS  = Path(os.environ.get("PAPER_FINAL_DUMPS", DEFAULT_FINAL_DUMPS))
V1_VISUAL_DUMPS = Path(os.environ.get("PAPER_V1_VISUAL_DUMPS", DEFAULT_V1_VISUAL_DUMPS))
OUT_DATA     = ROOT / "paper_writing" / "data"
OUT_TABLES   = ROOT / "paper_writing" / "tables"
OUT_DATA.mkdir(parents=True, exist_ok=True)
OUT_TABLES.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Thresholds (Table 4 in paper) — same as cwv_paper_metrics.py
# ---------------------------------------------------------------------------
THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    #                          good    ni     noise  meaningful
    "lcp_mobile":  (2500.0, 4000.0, 100.0, 200.0),
    "lcp_desktop": (2500.0, 4000.0,  50.0, 100.0),
    "inp_mobile":  ( 200.0,  500.0,  24.0,  32.0),
    "inp_desktop": ( 200.0,  500.0,  24.0,  32.0),
    "cls_mobile":  (   0.1,   0.25,  0.01,  0.02),
    "cls_desktop": (   0.1,   0.25,  0.01,  0.02),
}
METRIC_KEYS = list(THRESHOLDS.keys())

OVERALL_SCORE_WEIGHTS = {
    "pareto_rate_rerun_reg_removed": 0.40,
    "mean_health_delta": 0.30,
    "degraded_rate": 0.20,
    "mean_net_threshold_crossing": 0.10,
}

LABELS_LATEX = {
    "lcp_mobile":  r"LCP$_\text{mob}$ (ms)",
    "lcp_desktop": r"LCP$_\text{dsk}$ (ms)",
    "inp_mobile":  r"INP$_\text{mob}$ (ms)",
    "inp_desktop": r"INP$_\text{dsk}$ (ms)",
    "cls_mobile":  r"CLS$_\text{mob}$",
    "cls_desktop": r"CLS$_\text{dsk}$",
}

# ---------------------------------------------------------------------------
# Model registry — maps final_result_dumps folder name → display label
# ---------------------------------------------------------------------------
MODEL_DISPLAY = {
    "closed_cc-opus-4.6":        "Opus 4.6",
    "closed_cc-sonnet-4.6":      "Sonnet 4.6",
    "closed_oc_gemini-2-5-flash":"Gemini Flash",
    "closed_oc_gemini-2-5-pro":  "Gemini Pro",
    "closed_oc_gpt-4.1":         "GPT-4.1",
    "closed_oc_gpt-5":           "GPT-5",
    "closed_oc_gpt-5.1-codex":   "GPT-5.1-Codex",
    "open_devstral-2-123b":      "Devstral 123B",
    "open_gemma-4-31b-it":       "Gemma 31B",
    "open_glm-4.7-flash":        "GLM Flash",
    "open_minimax-m2.7":         "Minimax M2.7",
    "open_qwen3-coder-next":     "Qwen3-Coder",
    "open_qwen3.5-122b-a10b":    "Qwen3.5-122B",
    "open_qwen3.5-27b":          "Qwen3.5-27B",
    "open_qwen3.5-35b-a3b":      "Qwen3.5-35B",
    "open_qwen3.5-397b-a17b":    "Qwen3.5-397B",
    "open_qwen3.5-9b":           "Qwen3.5-9B",
    "closed_cc-aider":           "Aider",
    "closed_cc-codex":           "Codex",
}

# ---------------------------------------------------------------------------
# Regression logic
# ---------------------------------------------------------------------------

def compute_regression(visual_path: Path) -> bool | None:
    """
    Returns:
      True   — job is regressed (≥2 checks agree positive, or single valid check is positive)
      False  — job is not regressed
      None   — all checks failed/null; exclude from regression denominator
    """
    try:
        d = json.loads(visual_path.read_text())
    except Exception:
        return None

    checks = d.get("checks", {})
    results = [
        checks.get("structural",    {}).get("regression"),
        checks.get("jaccard_text",  {}).get("regression"),
        checks.get("gpt_visual",    {}).get("regression"),
        checks.get("console_errors",{}).get("regression"),
    ]

    valid   = [r for r in results if r is not None]   # True or False only
    n_valid = len(valid)
    n_true  = sum(valid)

    if n_valid == 0:
        return None                  # all checks errored — exclude
    if n_valid == 1:
        return bool(valid[0])        # only one check ran — trust it
    # ≥2 valid checks: require ≥2 to agree on True
    return n_true >= 2


# ---------------------------------------------------------------------------
# Health / tier — verbatim from cwv_paper_metrics.py
# ---------------------------------------------------------------------------

def health_score(v: float, good: float, ni: float) -> float:
    if v <= good:
        return 100.0
    if v <= ni:
        return 100.0 - 50.0 * (v - good) / (ni - good)
    return max(0.0, 50.0 * (1.0 - (v - ni) / ni))


def tier(v: float, good: float, ni: float) -> int:
    return 2 if v <= good else (1 if v <= ni else 0)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _safe_load(p: Path) -> dict | None:
    if not p.exists() or p.stat().st_size < 50:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _mad_filter(vals: list[float], k: float = 3.0) -> list[float]:
    """Drop points more than k MADs from the median (robust outlier removal).

    Unlike the IQR fences in cwv_tool (which at n=5 derive Q1/Q3 from the same
    contaminated sample and never trip), the median/MAD center is itself robust,
    so a single 11s glitch run does not widen the fence enough to survive.
    """
    if len(vals) < 3:
        return vals
    med = median(vals)
    mad = median([abs(x - med) for x in vals])
    if mad == 0:                      # tightly clustered — nothing to trim
        return vals
    thr = k * 1.4826 * mad            # 1.4826 -> MAD ~ sigma for normal data
    kept = [x for x in vals if abs(x - med) <= thr]
    return kept or vals


def _p75_inclusive(vals: list[float]) -> float:
    """Proper 75th percentile (R-7 / 'inclusive'), no tail extrapolation."""
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    import statistics as _st
    return _st.quantiles(vals, n=4, method="inclusive")[2]


def _robust_p75_from_runs(dev: dict, kind: str) -> float | None:
    runs = dev.get("runs") or []
    field = {"lcp": "LCP", "inp": "INP"}[kind]
    vals = []
    for r in runs:
        try:
            v = float(r.get(field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:                     # mirror calc_stats: drop non-positive
            vals.append(v)
    if not vals:                      # no samples -> 0, matches calc_stats empty
        return 0.0
    return _p75_inclusive(_mad_filter(vals))


def _extract_metric_pair(baseline_dev: dict, patched_dev: dict, kind: str):
    # LCP/INP aggregation statistic is configurable via PAPER_LCP_STAT:
    #   "p75"        (default) — stored field; mean-of-top-2-of-5-runs, dominated
    #                 by a single contention/cold-start glitch run (BROKEN).
    #   "median"     — stored robust central tendency.
    #   "robust_p75" — recomputed from raw runs[] with MAD outlier removal +
    #                 inclusive (R-7) p75. Keeps p75 semantics, outlier-robust.
    # CLS always uses the stored median (already robust).
    stat = os.environ.get("PAPER_LCP_STAT", "robust_p75").lower()

    if stat == "robust_p75" and kind in ("lcp", "inp"):
        bv = _robust_p75_from_runs(baseline_dev, kind)
        pv = _robust_p75_from_runs(patched_dev, kind)
        if bv is None or pv is None:
            return None
        return (bv, pv)

    suffix = "median" if stat == "median" else "p75"
    field = {"lcp": f"LCP_{suffix}", "inp": f"INP_{suffix}", "cls": "CLS_median"}[kind]
    try:
        bv = float((baseline_dev.get("aggregated") or {}).get(field))
        pv = float((patched_dev.get("aggregated") or {}).get(field))
    except (TypeError, ValueError):
        return None
    if bv != bv or pv != pv:
        return None
    return (bv, pv)


def build_site_record(site_id: str, task_dir: Path) -> dict | None:
    """Build {metric_key: [baseline, patched]} or None if data incomplete."""
    base_m = _safe_load(BASELINE_DIR / site_id / "mobile.json")
    base_d = _safe_load(BASELINE_DIR / site_id / "desktop.json")
    if base_m is None or base_d is None:
        return None

    patched_m = _safe_load(task_dir / "mobile.json")
    patched_d = _safe_load(task_dir / "desktop.json")
    if patched_m is None or patched_d is None:
        return None

    rec: dict[str, list[float]] = {}
    for kind in ("lcp", "inp", "cls"):
        mp = _extract_metric_pair(base_m, patched_m, kind)
        if mp is None:
            return None
        rec[f"{kind}_mobile"] = list(mp)
        dp = _extract_metric_pair(base_d, patched_d, kind)
        if dp is None:
            return None
        rec[f"{kind}_desktop"] = list(dp)
    return rec


# ---------------------------------------------------------------------------
# Per-site CWV metrics
# ---------------------------------------------------------------------------

def site_metrics(record: dict[str, list[float]]) -> dict[str, Any]:
    per_metric = []
    for key in METRIC_KEYS:
        v_init, v_final = record[key]
        good, ni, noise, meaningful = THRESHOLDS[key]
        h_init  = health_score(v_init,  good, ni)
        h_final = health_score(v_final, good, ni)
        improvement = v_init - v_final
        per_metric.append({
            "key":        key,
            "h_init":     h_init,
            "h_final":    h_final,
            "h_delta":    h_final - h_init,
            "tier_delta": tier(v_final, good, ni) - tier(v_init, good, ni),
            "improved":   improvement >= meaningful,
            "degraded":   (-improvement) > noise,
            "v_init":     v_init,
            "v_final":    v_final,
        })
    any_improved = any(m["improved"] for m in per_metric)
    any_degraded = any(m["degraded"] for m in per_metric)
    return {
        "health_delta":           mean(m["h_delta"]    for m in per_metric),
        "net_threshold_crossing": sum(m["tier_delta"]  for m in per_metric),
        "is_pareto":              any_improved and not any_degraded,
        "is_degraded":            any_degraded,
        "per_metric":             per_metric,
    }


def aggregate_model(site_results: list[dict[str, Any]],
                    reg_true: int, reg_false: int, reg_excl: int) -> dict[str, Any]:
    n = len(site_results)
    reg_total = reg_true + reg_false
    agg: dict[str, Any] = {
        "n_cwv_sites":              n,
        "regression_pct":           reg_true  / reg_total * 100 if reg_total else None,
        "regression_count":         reg_true,
        "regression_denom":         reg_total,
        "regression_excluded":      reg_excl,
    }
    if n:
        agg.update({
            "pareto_rate":                  mean(s["is_pareto"]  for s in site_results),
            "degraded_rate":                mean(s["is_degraded"] for s in site_results),
            "mean_health_delta":            mean(s["health_delta"] for s in site_results),
            "median_health_delta":          median(s["health_delta"] for s in site_results),
            "mean_net_threshold_crossing":  mean(s["net_threshold_crossing"] for s in site_results),
            "per_metric_health_delta": {
                key: mean(
                    next(m["h_delta"] for m in s["per_metric"] if m["key"] == key)
                    for s in site_results
                )
                for key in METRIC_KEYS
            },
            "per_metric_pct_improved": {
                key: mean(
                    next(m["improved"] for m in s["per_metric"] if m["key"] == key)
                    for s in site_results
                )
                for key in METRIC_KEYS
            },
            "per_metric_pct_degraded": {
                key: mean(
                    next(m["degraded"] for m in s["per_metric"] if m["key"] == key)
                    for s in site_results
                )
                for key in METRIC_KEYS
            },
        })
    return agg


def pareto_rate(site_results: list[dict[str, Any]]) -> float | None:
    if not site_results:
        return None
    return mean(s["is_pareto"] for s in site_results)


def add_overall_scores(per_model_summary: dict[str, dict[str, Any]]) -> None:
    """Add a 0-100 model score from the four primary ranking metrics.

    Higher is better for Pareto Rerun, Health Delta, and Net Threshold.
    Lower is better for Degraded, so it is inverted after normalization.
    """
    keys = list(OVERALL_SCORE_WEIGHTS)
    valid_models = [
        m for m, s in per_model_summary.items()
        if s.get("n_cwv_sites") and all(s.get(k) is not None for k in keys)
    ]
    if not valid_models:
        return

    bounds: dict[str, tuple[float, float]] = {}
    for key in keys:
        vals = [float(per_model_summary[m][key]) for m in valid_models]
        bounds[key] = (min(vals), max(vals))

    for model in valid_models:
        s = per_model_summary[model]
        components: dict[str, float] = {}
        for key in keys:
            lo, hi = bounds[key]
            value = float(s[key])
            if hi == lo:
                norm = 100.0
            elif key == "degraded_rate":
                norm = 100.0 * (hi - value) / (hi - lo)
            else:
                norm = 100.0 * (value - lo) / (hi - lo)
            components[key] = norm
        score = sum(OVERALL_SCORE_WEIGHTS[k] * components[k] for k in keys)
        s["overall_score"] = score
        s["overall_score_components"] = components
        s["overall_score_weights"] = OVERALL_SCORE_WEIGHTS

    ranked = sorted(
        valid_models,
        key=lambda m: (-per_model_summary[m]["overall_score"], MODEL_DISPLAY.get(m, m)),
    )
    last_score = None
    last_rank = 0
    for idx, model in enumerate(ranked, start=1):
        score = round(per_model_summary[model]["overall_score"], 6)
        if score != last_score:
            last_rank = idx
            last_score = score
        per_model_summary[model]["overall_rank"] = last_rank


# ---------------------------------------------------------------------------
# Main driver — iterates over final_result_dumps/
# ---------------------------------------------------------------------------

def compute_all() -> dict[str, Any]:
    per_site_rows: list[dict] = []
    per_model_cwv: dict[str, list[dict]] = defaultdict(list)
    per_model_cwv_v1_clean: dict[str, list[dict]] = defaultdict(list)
    per_model_cwv_rerun_clean: dict[str, list[dict]] = defaultdict(list)
    per_model_reg: dict[str, dict]       = defaultdict(
        lambda: {"true": 0, "false": 0, "excluded": 0}
    )

    for model_dir in sorted(FINAL_DUMPS.iterdir()):
        if not model_dir.is_dir():
            continue
        model_key = model_dir.name

        for task_dir in sorted(model_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            site_id = task_dir.name.split("_")[0]

            # ── Regression ───────────────────────────────────────────────
            visual_path = task_dir / "visual.json"
            reg = None
            if visual_path.exists():
                reg = compute_regression(visual_path)
                if reg is None:
                    per_model_reg[model_key]["excluded"] += 1
                elif reg:
                    per_model_reg[model_key]["true"]  += 1
                else:
                    per_model_reg[model_key]["false"] += 1

            # ── CWV — include every row with valid CWV, regardless of visual
            # regression status.  Vis. Reg. remains a reported diagnostic.
            rec = build_site_record(site_id, task_dir)
            if rec is None:
                continue
            sm  = site_metrics(rec)
            per_model_cwv[model_key].append(sm)

            if reg is False:
                per_model_cwv_rerun_clean[model_key].append(sm)

            v1_visual_path = V1_VISUAL_DUMPS / model_key / task_dir.name / "visual.json"
            if v1_visual_path.exists() and compute_regression(v1_visual_path) is False:
                per_model_cwv_v1_clean[model_key].append(sm)

            row = {
                "model":    model_key,
                "site_id":  site_id,
                "task":     task_dir.name,
                "health_delta":           sm["health_delta"],
                "net_threshold_crossing": sm["net_threshold_crossing"],
                "is_pareto":              int(sm["is_pareto"]),
                "is_degraded":            int(sm["is_degraded"]),
            }
            for m in sm["per_metric"]:
                row[f"{m['key']}_init"]    = m["v_init"]
                row[f"{m['key']}_final"]   = m["v_final"]
                row[f"{m['key']}_h_delta"] = m["h_delta"]
            per_site_rows.append(row)

    # ── Aggregate ──────────────────────────────────────────────────────────
    all_models = sorted(MODEL_DISPLAY.keys())
    per_model_summary = {
        m: aggregate_model(
            per_model_cwv[m],
            per_model_reg[m]["true"],
            per_model_reg[m]["false"],
            per_model_reg[m]["excluded"],
        )
        for m in all_models
    }
    for m in all_models:
        per_model_summary[m]["pareto_rate_raw"] = per_model_summary[m].get("pareto_rate")
        per_model_summary[m]["pareto_rate_v1_reg_removed"] = pareto_rate(per_model_cwv_v1_clean[m])
        per_model_summary[m]["pareto_rate_rerun_reg_removed"] = pareto_rate(per_model_cwv_rerun_clean[m])
        per_model_summary[m]["n_cwv_v1_reg_removed"] = len(per_model_cwv_v1_clean[m])
        per_model_summary[m]["n_cwv_rerun_reg_removed"] = len(per_model_cwv_rerun_clean[m])

    add_overall_scores(per_model_summary)

    # ── CSV ────────────────────────────────────────────────────────────────
    csv_path = OUT_DATA / "final_per_site.csv"
    if per_site_rows:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(per_site_rows[0].keys()))
            w.writeheader()
            for r in per_site_rows:
                w.writerow(r)

    # ── JSON summary ───────────────────────────────────────────────────────
    payload = {
        "models":     all_models,
        "per_model":  per_model_summary,
        "v1_visual_dumps": str(V1_VISUAL_DUMPS),
    }
    (OUT_DATA / "final_summary.json").write_text(json.dumps(payload, indent=2))

    return payload


# ---------------------------------------------------------------------------
# Table writers
# ---------------------------------------------------------------------------

def _bold_if(value, condition, fmt, sign=False):
    if value is None or value != value:
        return "--"
    body = ("{:+" + fmt + "}").format(value) if sign else ("{:" + fmt + "}").format(value)
    return r"\textbf{" + body + r"}" if condition else body


def _heat_cell(cell: str, value: float | None, lo: float, hi: float,
               *, higher_better: bool = True, color: str = "green") -> str:
    if value is None or value != value:
        return cell
    if hi == lo:
        pct = 28
    else:
        norm = (value - lo) / (hi - lo)
        if not higher_better:
            norm = 1.0 - norm
        pct = round(6 + 30 * max(0.0, min(1.0, norm)))
    return rf"\cellcolor{{{color}!{pct}}}{cell}"


def _bounds(vals: list[float | None]) -> tuple[float, float]:
    present = [float(v) for v in vals if v is not None and v == v]
    if not present:
        return (0.0, 0.0)
    return (min(present), max(present))


# ---------------------------------------------------------------------------
# Model → (agent scaffold, LaTeX display name, section)
# ---------------------------------------------------------------------------
MODEL_META = {
    # key                          agent           latex name           section
    "closed_oc_gpt-5.1-codex":  ("OpenCode",    r"GPT-5.1\,Codex",                    "closed"),
    "closed_oc_gpt-5":          ("OpenCode",    r"GPT-5",                              "closed"),
    "closed_oc_gemini-2-5-pro": ("OpenCode",    r"Gemini\,2.5\,Pro",                  "closed"),
    "closed_oc_gemini-2-5-flash":("OpenCode",   r"Gemini\,2.5\,Flash",                "closed"),
    "closed_oc_gpt-4.1":        ("OpenCode",    r"GPT-4.1",                            "closed"),
    "closed_cc-opus-4.6":       ("Claude~Code", r"Opus\,4.6",                          "closed"),
    "closed_cc-sonnet-4.6":     ("Claude~Code", r"Sonnet\,4.6",                        "closed"),
    "closed_cc-aider":          ("Aider",       r"GPT-5",                              "closed"),
    "closed_cc-codex":          ("Codex",       r"GPT-5",                              "closed"),
    "open_qwen3.5-27b":         ("OpenCode",    r"Qwen3.5\,27B",                       "open"),
    "open_qwen3.5-35b-a3b":     ("OpenCode",    r"Qwen3.5\,35B-A3B$^{\star}$",        "open"),
    "open_qwen3.5-397b-a17b":   ("OpenCode",    r"Qwen3.5\,397B-A17B$^{\star}$",      "open"),
    "open_qwen3.5-122b-a10b":   ("OpenCode",    r"Qwen3.5\,122B-A10B$^{\star}$",      "open"),
    "open_qwen3.5-9b":          ("OpenCode",    r"Qwen3.5\,9B",                        "open"),
    "open_glm-4.7-flash":       ("OpenCode",    r"GLM\,4.7\,Flash$^{\star}$",          "open"),
    "open_gemma-4-31b-it":      ("OpenCode",    r"Gemma\,4\,31B",                      "open"),
    "open_qwen3-coder-next":    ("OpenCode",    r"Qwen3\,Coder\,Next$^{\star}$",       "open"),
    "open_minimax-m2.7":        ("OpenCode",    r"MiniMax\,M2.7$^{\star}$",            "open"),
    "open_devstral-2-123b":     ("OpenCode",    r"Devstral\,2\,123B",                  "open"),
}


def build_main_table(payload: dict) -> str:
    # ── Collect per-model stats ────────────────────────────────────────────
    data = {}
    for m, (agent, latex_name, section) in MODEL_META.items():
        s = payload["per_model"].get(m, {})
        data[m] = {
            "agent":      agent,
            "name":       latex_name,
            "section":    section,
            "n_reg":      s.get("regression_denom", 0),
            "reg_pct":    s.get("regression_pct"),
            "n_cwv":      s.get("n_cwv_sites", 0),
            "pareto":     s.get("pareto_rate"),
            "pareto_v1":  s.get("pareto_rate_v1_reg_removed"),
            "pareto_rr":  s.get("pareto_rate_rerun_reg_removed"),
            "ntc":        s.get("mean_net_threshold_crossing"),
            "health":     s.get("mean_health_delta"),
            "degraded":   s.get("degraded_rate"),
            "overall":    s.get("overall_score"),
        }

    # ── Global best values (for bold) ─────────────────────────────────────
    all_vals = list(data.values())
    best_pareto  = max((d["pareto"]  or 0)    for d in all_vals)
    best_pareto_rr = max((d["pareto_rr"] or 0) for d in all_vals)
    best_ntc     = max((d["ntc"]     or -999) for d in all_vals)
    best_health  = max((d["health"]  or -999) for d in all_vals)
    least_deg    = min((d["degraded"]or 999)  for d in all_vals)
    least_reg    = min((d["reg_pct"] or 999)  for d in all_vals)
    best_overall = max((d["overall"] or -999) for d in all_vals)

    heat_bounds = {
        "reg_pct": _bounds([d["reg_pct"] for d in all_vals]),
        "pareto": _bounds([d["pareto"] * 100 if d["pareto"] is not None else None for d in all_vals]),
        "pareto_rr": _bounds([d["pareto_rr"] * 100 if d["pareto_rr"] is not None else None for d in all_vals]),
        "ntc": _bounds([d["ntc"] for d in all_vals]),
        "health": _bounds([d["health"] for d in all_vals]),
        "degraded": _bounds([d["degraded"] * 100 if d["degraded"] is not None else None for d in all_vals]),
        "overall": _bounds([d["overall"] for d in all_vals]),
    }
    heat_colors = {
        "reg_pct": "blue",
        "pareto": "green",
        "pareto_rr": "cyan",
        "ntc": "magenta",
        "health": "yellow",
        "degraded": "red",
        "overall": "violet",
    }

    def fmt_row(m):
        d = data[m]
        rp = d["reg_pct"]
        pr = d["pareto"]
        prr = d["pareto_rr"]
        dg = d["degraded"]
        pr_pct = pr * 100 if pr is not None else None
        prr_pct = prr * 100 if prr is not None else None
        dg_pct = dg * 100 if dg is not None else None
        return [
            d["agent"],
            d["name"],
            _heat_cell(_bold_if(rp, rp == least_reg, ".1f"), rp, *heat_bounds["reg_pct"], higher_better=False, color=heat_colors["reg_pct"]),
            _heat_cell(_bold_if(pr_pct, pr == best_pareto, ".1f"), pr_pct, *heat_bounds["pareto"], color=heat_colors["pareto"]),
            _heat_cell(_bold_if(prr_pct, prr == best_pareto_rr, ".1f"), prr_pct, *heat_bounds["pareto_rr"], color=heat_colors["pareto_rr"]),
            _heat_cell(_bold_if(d["ntc"], d["ntc"] == best_ntc, ".2f", sign=True), d["ntc"], *heat_bounds["ntc"], color=heat_colors["ntc"]),
            _heat_cell(_bold_if(d["health"], d["health"] == best_health, ".2f", sign=True), d["health"], *heat_bounds["health"], color=heat_colors["health"]),
            _heat_cell(_bold_if(dg_pct, dg == least_deg, ".1f"), dg_pct, *heat_bounds["degraded"], higher_better=False, color=heat_colors["degraded"]),
            _heat_cell(_bold_if(d["overall"], d["overall"] == best_overall, ".1f"), d["overall"], *heat_bounds["overall"], color=heat_colors["overall"]),
        ]

    ranked_models = sorted(
        MODEL_META,
        key=lambda m: (
            data[m]["overall"] is None,
            -(data[m]["overall"] if data[m]["overall"] is not None else -1),
            data[m]["agent"],
            MODEL_DISPLAY.get(m, m),
        ),
    )

    lines = [
        r"\begin{table*}[t]",
        r"\centering",
        r"\small",
        r"\setlength{\tabcolsep}{5pt}",
        r"\begin{adjustbox}{max width=\linewidth}",
        r"\begin{tabular}{llrrrrrrr}",
        r"\toprule",
        r"\textbf{Agent} & \textbf{LLM}"
        r"  & \textbf{Vis.\ Reg.}$\downarrow$"
        r"  & \textbf{Pareto Raw}$\uparrow$"
        r"  & \textbf{Pareto Rerun}$\uparrow$"
        r"  & \textbf{Net Thresh.}$\uparrow$"
        r"  & \textbf{Health $\Delta$}$\uparrow$"
        r"  & \textbf{Degraded}$\downarrow$"
        r"  & \textbf{Overall Score}$\uparrow$ \\",
        r"  &  & \textbf{(\%)} & \textbf{(\%)} & \textbf{(\%)} &  &  & \textbf{(\%)} & \\",
        r"\midrule",
    ]

    for m in ranked_models:
        lines.append(" & ".join(fmt_row(m)) + r" \\")

    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{adjustbox}",
        r"\caption{All evaluated configurations on SWE-WEB, CWV metrics computed for "
        r"all rows with valid baseline and patched CWV, including visually regressed patches. "
        r"\emph{Vis.\ Reg.}: fraction of patches failing the four-signal visual regression check "
        r"($\geq$2-signal agreement; single valid check trusted). "
        r"\emph{Pareto Raw}: fraction of CWV-measured sites where $\geq$1 CWV metric improves "
        r"with no metric degrading beyond noise, with visual regressions included. "
        r"\emph{Pareto Rerun}: the same CWV outcomes filtered by rerun visual labels. "
        r"\emph{Net Thresh.}: mean CWV tier transitions (Poor$\to$Good\,$=+2$, Good$\to$Poor\,$=-2$). "
        r"\emph{Health $\Delta$}: mean change in composite Site Health Score. "
        r"\emph{Degraded}: fraction of CWV-measured sites with any metric regressing beyond noise. "
        r"\emph{Overall Score}: 0--100 weighted normalized composite "
        r"(40\% Pareto Rerun, 30\% Health $\Delta$, 20\% inverse Degraded, "
        r"10\% Net Thresh.). "
        r"Rows are sorted by Overall Score. Numeric columns use distinct pastel column-wise heatmap shading, "
        r"where darker cells indicate better values for that metric. Best per-column value in \textbf{bold}.}",
        r"\label{tab:all_main}",
        r"\end{table*}",
    ]
    return "\n".join(lines)


def build_per_metric_table(payload: dict) -> str:
    rows = []
    for m in payload["models"]:
        s = payload["per_model"].get(m, {})
        if not s.get("n_cwv_sites"):
            continue
        rows.append((MODEL_DISPLAY.get(m, m), s["per_metric_health_delta"],
                     s.get("overall_score")))
    rows.sort(key=lambda r: (-(r[2] if r[2] is not None else -1), r[0]))

    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\small",
        r"\setlength{\tabcolsep}{4pt}",
        r"\begin{tabular}{l" + "r" * (len(METRIC_KEYS) + 1) + "}",
        r"\toprule",
        " & ".join(["Model", r"Overall Score"] + [LABELS_LATEX[k] for k in METRIC_KEYS]) + r" \\",
        r"\midrule",
    ]
    best = {k: max(d[k] for _, d, _ in rows) for k in METRIC_KEYS}
    best_overall = max(score for _, _, score in rows if score is not None)
    heat_bounds = {
        "overall": _bounds([score for _, _, score in rows]),
        **{k: _bounds([d[k] for _, d, _ in rows]) for k in METRIC_KEYS},
    }
    heat_colors = {
        "overall": "violet",
        "lcp_mobile": "green",
        "lcp_desktop": "cyan",
        "inp_mobile": "magenta",
        "inp_desktop": "blue",
        "cls_mobile": "yellow",
        "cls_desktop": "red",
    }
    for model, deltas, score in rows:
        cells = [
            model,
            _heat_cell(_bold_if(score, score == best_overall, ".1f"), score, *heat_bounds["overall"], color=heat_colors["overall"]),
        ] + [
            _heat_cell(
                _bold_if(deltas[k], deltas[k] == best[k], ".2f", sign=True),
                deltas[k],
                *heat_bounds[k],
                color=heat_colors[k],
            )
            for i, k in enumerate(METRIC_KEYS)
        ]
        lines.append(" & ".join(cells) + r" \\")
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\caption{Per-metric Site Health $\Delta$. "
        r"Each cell is the mean change in $H(v,m)$ from baseline to post-agent. "
        r"Rows are sorted by Overall Score; numeric columns use distinct pastel column-wise heatmap shading, "
        r"where darker cells indicate better values for that metric; best per-column value is bold.}",
        r"\label{tab:final_per_metric}",
        r"\end{table}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    global BASELINE_DIR, FINAL_DUMPS, V1_VISUAL_DUMPS

    ap = argparse.ArgumentParser(description="Compute SWE-WEB paper metrics from final_result_dumps/.")
    ap.add_argument("--baseline-dir", type=Path, default=BASELINE_DIR,
                    help="Directory containing per-site baseline mobile.json/desktop.json files.")
    ap.add_argument("--final-dumps", type=Path, default=FINAL_DUMPS,
                    help="Directory containing per-model final result dump folders.")
    ap.add_argument("--v1-visual-dumps", type=Path, default=V1_VISUAL_DUMPS,
                    help="Original main bench dump used only for v1 visual-regression filtering.")
    ap.add_argument("--print-tables", action="store_true",
                    help="Emit LaTeX for main + per-metric tables to stdout.")
    args = ap.parse_args()

    BASELINE_DIR = args.baseline_dir
    FINAL_DUMPS = args.final_dumps
    V1_VISUAL_DUMPS = args.v1_visual_dumps

    print(f"Loading baselines from: {BASELINE_DIR}")
    print(f"Loading final dumps from: {FINAL_DUMPS}")
    print(f"Loading v1 visual labels from: {V1_VISUAL_DUMPS}")

    payload = compute_all()

    if args.print_tables:
        main_tex       = build_main_table(payload)
        per_metric_tex = build_per_metric_table(payload)
        print(main_tex)
        print()
        print(per_metric_tex)
        print()
        # Also save to disk
        (OUT_TABLES / "final_main.tex").write_text(main_tex)
        (OUT_TABLES / "final_per_metric.tex").write_text(per_metric_tex)

    # Console summary
    print()
    print(f"{'Model':<22} {'N_reg':>6} {'Reg%':>7}  {'N_cwv':>6} {'Pareto%':>8} "
          f"{'Degraded%':>10} {'MeanHd':>8} {'MeanNTC':>8}")
    print("-" * 80)
    for m in payload["models"]:
        s = payload["per_model"].get(m) or {}
        n_reg  = s.get("regression_denom", 0)
        rp     = s.get("regression_pct")
        n_cwv  = s.get("n_cwv_sites", 0)
        name   = MODEL_DISPLAY.get(m, m)
        rp_str = f"{rp:6.1f}%" if rp is not None else "   n/a"
        pr_str = f"{s['pareto_rate']*100:7.1f}%" if s.get("pareto_rate") is not None else "     n/a"
        dg_str = f"{s['degraded_rate']*100:9.1f}%" if s.get("degraded_rate") is not None else "       n/a"
        hd_str = f"{s['mean_health_delta']:+8.2f}" if s.get("mean_health_delta") is not None else "      n/a"
        nt_str = f"{s['mean_net_threshold_crossing']:+8.2f}" if s.get("mean_net_threshold_crossing") is not None else "      n/a"
        print(f"{name:<22} {n_reg:>6} {rp_str}  {n_cwv:>6} {pr_str} {dg_str} {hd_str} {nt_str}")
    print()
    print(f"Data written to: {OUT_DATA}/final_*.{{csv,json}}")
    if args.print_tables:
        print("LaTeX tables printed above.")
    else:
        print("Re-run with --print-tables to emit LaTeX.")
    print()


if __name__ == "__main__":
    main()
