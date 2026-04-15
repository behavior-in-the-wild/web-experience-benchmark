#!/usr/bin/env python3
"""Compute CWV paper metrics from a list of initial/final scores.

Each site is a JSON object with six metric keys, each mapping to
[initial_value, final_value]:

    {
      "lcp_mobile":  [3500, 2200],
      "lcp_desktop": [1800, 1600],
      "inp_mobile":  [300, 180],
      "inp_desktop": [50, 48],
      "cls_mobile":  [0.15, 0.08],
      "cls_desktop": [0.05, 0.05]
    }

Usage:
    python cwv_paper_metrics.py sites.json
    cat sites.json | python cwv_paper_metrics.py
"""

from __future__ import annotations

import argparse
import json
import sys
from statistics import mean
from typing import Any

# ---------------------------------------------------------------------------
# Thresholds — Table 4 of the paper
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

METRICS = list(THRESHOLDS.keys())


# ---------------------------------------------------------------------------
# Core formulae
# ---------------------------------------------------------------------------


def health_score(v: float, good: float, ni: float) -> float:
    """Site Health Score H(v, m) — Eq. 1 of the paper.

    Maps a raw CWV value to [0, 100]: 100 at Good, 50 at NI boundary,
    0 for severe degradation.

    Args:
        v: Raw metric value.
        good: Good threshold G_m.
        ni: Needs Improvement threshold N_m.

    Returns:
        Health score in [0.0, 100.0].
    """
    if v <= good:
        return 100.0
    if v <= ni:
        return 100.0 - 50.0 * (v - good) / (ni - good)
    return max(0.0, 50.0 * (1.0 - (v - ni) / ni))


def tier(v: float, good: float, ni: float) -> int:
    """Map a raw value to ordinal tier (2=Good, 1=NI, 0=Poor)."""
    return 2 if v <= good else (1 if v <= ni else 0)


TIER_LABEL = {2: "Good", 1: "NI", 0: "Poor"}


# ---------------------------------------------------------------------------
# Per-site computation
# ---------------------------------------------------------------------------


def site_metrics(record: dict[str, list[float]]) -> dict[str, Any]:
    """Compute all paper metrics for one site.

    Args:
        record: Dict mapping each of the six metric keys to
                [initial_value, final_value].

    Returns:
        Dict with keys: health_delta, net_threshold_crossing,
        is_pareto, is_degraded, per_metric (list of per-metric dicts).
    """
    per_metric = []
    for key in METRICS:
        v_init, v_final = record[key]
        good, ni, noise, meaningful = THRESHOLDS[key]
        h_init = health_score(v_init, good, ni)
        h_final = health_score(v_final, good, ni)
        improvement = v_init - v_final  # positive = lower value = better
        per_metric.append(
            {
                "key": key,
                "h_init": h_init,
                "h_final": h_final,
                "h_delta": h_final - h_init,
                "tier_delta": tier(v_final, good, ni) - tier(v_init, good, ni),
                "improved": improvement >= meaningful,
                "degraded": (-improvement) > noise,
            }
        )

    any_improved = any(m["improved"] for m in per_metric)
    any_degraded = any(m["degraded"] for m in per_metric)

    return {
        "health_delta": mean(m["h_delta"] for m in per_metric),
        "net_threshold_crossing": sum(m["tier_delta"] for m in per_metric),
        "is_pareto": any_improved and not any_degraded,
        "is_degraded": any_degraded,
        "per_metric": per_metric,
    }


# ---------------------------------------------------------------------------
# Aggregate across sites
# ---------------------------------------------------------------------------


def aggregate(site_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate paper metrics across a list of sites.

    Args:
        site_results: List of dicts returned by site_metrics().

    Returns:
        Dict with:
          pareto_rate       – fraction of Pareto sites (0-1)
          degraded_rate     – fraction of degraded sites (0-1)
          mean_health_delta – mean Health Delta across sites
          mean_net_threshold_crossing – mean Net Threshold Crossing
          per_metric_health_delta – mean H_delta per metric key
    """
    n = len(site_results)
    return {
        "n_sites": n,
        "pareto_rate": mean(s["is_pareto"] for s in site_results),
        "degraded_rate": mean(s["is_degraded"] for s in site_results),
        "mean_health_delta": mean(s["health_delta"] for s in site_results),
        "mean_net_threshold_crossing": mean(
            s["net_threshold_crossing"] for s in site_results
        ),
        "per_metric_health_delta": {
            key: mean(
                next(m["h_delta"] for m in s["per_metric"] if m["key"] == key)
                for s in site_results
            )
            for key in METRICS
        },
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_SEP = "=" * 70
_THIN = "-" * 70

_LABELS = {
    "lcp_mobile": "LCP Mobile (ms)", "lcp_desktop": "LCP Desktop (ms)",
    "inp_mobile": "INP Mobile (ms)", "inp_desktop": "INP Desktop (ms)",
    "cls_mobile": "CLS Mobile",      "cls_desktop": "CLS Desktop",
}



def print_aggregate(agg: dict[str, Any]) -> None:
    """Print the aggregate paper metrics table."""
    n = agg["n_sites"]
    print(f"\n{_SEP}")
    print(f"  AGGREGATE METRICS  (n={n} sites)")
    print(_SEP)
    print(f"  Pareto Rate:                   {agg['pareto_rate']*100:>6.1f}%")
    print(f"  Degraded Sites:                {agg['degraded_rate']*100:>6.1f}%")
    print(f"  Mean Health Delta:             {agg['mean_health_delta']:>+7.2f}")
    print(f"  Mean Net Threshold Crossing:   {agg['mean_net_threshold_crossing']:>+7.2f}")
    print(f"\n  Mean Health Delta per metric:")
    print(f"  {'Metric':<20} {'H_delta':>8}")
    print("  " + "-" * 30)
    for key, hd in agg["per_metric_health_delta"].items():
        print(f"  {_LABELS[key]:<20} {hd:>+8.2f}")
    print(_SEP + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse args, load JSON, compute and print metrics."""
    parser = argparse.ArgumentParser(
        description="Compute CWV paper metrics from a JSON list of sites.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
JSON format — each site is an object with six keys, each [init, final]:
  [
    {
      "lcp_mobile":  [3500, 2200],
      "lcp_desktop": [1800, 1600],
      "inp_mobile":  [300, 180],
      "inp_desktop": [50, 48],
      "cls_mobile":  [0.15, 0.08],
      "cls_desktop": [0.05, 0.05]
    }
  ]
""",
    )
    parser.add_argument("input", nargs="?", help="JSON file (default: stdin)")
    args = parser.parse_args()

    src = open(args.input) if args.input else sys.stdin
    sites: list[dict[str, list[float]]] = json.load(src)
    if args.input:
        src.close()

    results = [site_metrics(s) for s in sites]
    agg = aggregate(results)

    print_aggregate(agg)


if __name__ == "__main__":
    main()
