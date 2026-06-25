#!/usr/bin/env python3
"""Score Mystique suggestion-level evals by selecting the best patch per site.

This consumes the ``metrics.json`` files produced by ``harness/scripts/compute_metrics.py``.
For each model, jobs are grouped by site id. The scorer keeps complete CWV rows,
optionally filters out visual regressions, selects the best suggestion/patch per
site, and aggregates only sites that are valid for both compared models.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from statistics import mean
from typing import Any


DEFAULT_MODELS = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def suggestion_index(job_name: str) -> int | None:
    marker = "_s"
    if marker not in job_name:
        return None
    tail = job_name.split(marker, 1)[1]
    digits = []
    for ch in tail:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    return int("".join(digits)) if digits else None


def visual_verdict(results_dir: Path, job_name: str) -> bool | None:
    data = load_json(results_dir / job_name / "visual.json")
    if not isinstance(data, dict):
        return None
    verdict = data.get("overall_regression")
    return verdict if isinstance(verdict, bool) else None


def metric_counts(metrics: dict[str, Any]) -> tuple[int, int]:
    improved = 0
    degraded = 0
    for row in metrics.get("per_metric", []):
        if row.get("improved") is True:
            improved += 1
        if row.get("degraded") is True:
            degraded += 1
    return improved, degraded


def rank_key(candidate: dict[str, Any]) -> tuple[float, float, int, int, int]:
    metrics = candidate["cwv_metrics"]
    improved, degraded = metric_counts(metrics)
    return (
        float(metrics.get("health_delta", 0.0)),
        float(metrics.get("net_threshold_crossing", 0.0)),
        1 if metrics.get("is_pareto") else 0,
        -1 if metrics.get("is_degraded") else 0,
        improved - degraded,
    )


def candidate_rows(metrics_path: Path, include_visual_regressions: bool) -> dict[str, list[dict[str, Any]]]:
    report = load_json(metrics_path)
    if not isinstance(report, dict):
        raise RuntimeError(f"could not read metrics report: {metrics_path}")

    results_dir = Path(report["results_dir"])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in report.get("jobs", []):
        cwv_metrics = row.get("cwv_metrics")
        if not isinstance(cwv_metrics, dict):
            continue

        verdict = visual_verdict(results_dir, row["job"])
        if verdict is None:
            continue
        if verdict is True and not include_visual_regressions:
            continue

        candidate = {
            **row,
            "visual_regression": verdict,
            "suggestion_index": suggestion_index(row["job"]),
        }
        grouped.setdefault(row["id"], []).append(candidate)
    return grouped


def aggregate(selected: list[dict[str, Any]]) -> dict[str, Any]:
    if not selected:
        return {
            "sites": 0,
            "pareto_count": 0,
            "degraded_count": 0,
            "pareto_rate": None,
            "degraded_rate": None,
            "mean_health_delta": None,
            "mean_net_threshold_crossing": None,
            "per_metric": {},
        }

    metrics = [row["cwv_metrics"] for row in selected]
    keys = [item["key"] for item in metrics[0].get("per_metric", [])]
    per_metric = {}
    for key in keys:
        rows = [
            next(item for item in metric["per_metric"] if item["key"] == key)
            for metric in metrics
        ]
        per_metric[key] = {
            "mean_initial": mean(float(row["initial"]) for row in rows),
            "mean_final": mean(float(row["final"]) for row in rows),
            "mean_delta": mean(float(row["delta"]) for row in rows),
            "mean_health_delta": mean(float(row["h_delta"]) for row in rows),
            "improved_count": sum(1 for row in rows if row.get("improved") is True),
            "degraded_count": sum(1 for row in rows if row.get("degraded") is True),
        }

    return {
        "sites": len(selected),
        "pareto_count": sum(1 for row in metrics if row.get("is_pareto") is True),
        "degraded_count": sum(1 for row in metrics if row.get("is_degraded") is True),
        "pareto_rate": mean(row.get("is_pareto") is True for row in metrics),
        "degraded_rate": mean(row.get("is_degraded") is True for row in metrics),
        "mean_health_delta": mean(float(row["health_delta"]) for row in metrics),
        "mean_net_threshold_crossing": mean(float(row["net_threshold_crossing"]) for row in metrics),
        "per_metric": per_metric,
    }


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f}%"


def fmt_num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--out-prefix", type=Path, default=None)
    parser.add_argument(
        "--include-visual-regressions",
        action="store_true",
        help="Allow visually regressed jobs to be selected if their CWV score is best.",
    )
    parser.add_argument(
        "--all-valid-per-model",
        action="store_true",
        help="Aggregate each model over all of its own valid sites instead of intersecting common sites.",
    )
    args = parser.parse_args()

    out_prefix = args.out_prefix or args.run_root / "suggestion_best_common"

    by_model: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for key, folder in DEFAULT_MODELS.items():
        metrics_path = args.run_root / folder / "metrics.json"
        by_model[key] = candidate_rows(metrics_path, args.include_visual_regressions)

    common_sites = sorted(set.intersection(*(set(rows) for rows in by_model.values())))
    selected: dict[str, list[dict[str, Any]]] = {key: [] for key in by_model}

    detail_rows: list[dict[str, Any]] = []
    for model_key, grouped in by_model.items():
        site_ids = sorted(grouped) if args.all_valid_per_model else common_sites
        for site_id in site_ids:
            best = max(grouped[site_id], key=rank_key)
            selected[model_key].append(best)
            metrics = best["cwv_metrics"]
            improved, degraded = metric_counts(metrics)
            detail_rows.append({
                "site_id": site_id,
                "model": model_key,
                "job": best["job"],
                "suggestion_index": best["suggestion_index"],
                "visual_regression": best["visual_regression"],
                "health_delta": metrics["health_delta"],
                "net_threshold_crossing": metrics["net_threshold_crossing"],
                "is_pareto": metrics["is_pareto"],
                "is_degraded": metrics["is_degraded"],
                "improved_metrics": improved,
                "degraded_metrics": degraded,
            })

    summary = {
        "run_root": str(args.run_root),
        "selection": {
            "common_valid_sites": len(common_sites),
            "mode": "all_valid_per_model" if args.all_valid_per_model else "common_sites",
            "include_visual_regressions": args.include_visual_regressions,
            "ranking": [
                "max health_delta",
                "max net_threshold_crossing",
                "prefer pareto",
                "prefer not degraded",
                "max improved-minus-degraded metric count",
            ],
        },
        "models": {model_key: aggregate(rows) for model_key, rows in selected.items()},
        "common_sites": common_sites,
        "valid_sites_by_model": {model_key: sorted(grouped) for model_key, grouped in by_model.items()},
    }

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    summary_path = out_prefix.with_suffix(".json")
    csv_path = out_prefix.with_suffix(".csv")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    fields = [
        "site_id", "model", "job", "suggestion_index", "visual_regression",
        "health_delta", "net_threshold_crossing", "is_pareto", "is_degraded",
        "improved_metrics", "degraded_metrics",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(detail_rows)

    print(f"Common valid sites: {len(common_sites)}")
    print(f"Wrote: {summary_path}")
    print(f"Wrote: {csv_path}")
    for model_key, row in summary["models"].items():
        print(
            f"{model_key:<8} sites={row['sites']} "
            f"pareto={fmt_pct(row['pareto_rate'])} "
            f"degraded={fmt_pct(row['degraded_rate'])} "
            f"health={fmt_num(row['mean_health_delta'])} "
            f"tier={fmt_num(row['mean_net_threshold_crossing'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
