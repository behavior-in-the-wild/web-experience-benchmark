#!/usr/bin/env python3
"""Compute release metrics for one model output folder.

Input can be either a model folder containing ``results/`` or the ``results``
folder itself. Each child result directory is expected to contain the files
written by ``harness/evaluate.sh``: ``visual.json``, ``mobile.json``,
``desktop.json``, ``usage.json``, and optionally ``cwv_data.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path
from statistics import mean
from typing import Any


THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    # key           good    needs-improvement  noise  meaningful
    "lcp_mobile":  (2500.0, 4000.0,            100.0, 200.0),
    "lcp_desktop": (2500.0, 4000.0,             50.0, 100.0),
    "inp_mobile":  ( 200.0,  500.0,             24.0,  32.0),
    "inp_desktop": ( 200.0,  500.0,             24.0,  32.0),
    "cls_mobile":  (   0.1,    0.25,             0.01,  0.02),
    "cls_desktop": (   0.1,    0.25,             0.01,  0.02),
}

DEVICES = ("mobile", "desktop")
METRICS = ("lcp", "inp", "cls")
METRIC_LABELS = {
    "lcp_mobile": "LCP mobile",
    "lcp_desktop": "LCP desktop",
    "inp_mobile": "INP mobile",
    "inp_desktop": "INP desktop",
    "cls_mobile": "CLS mobile",
    "cls_desktop": "CLS desktop",
}


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def as_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"nan", "none", "null", "n/a"}:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        return None
    return num if math.isfinite(num) else None


def parse_jsonish(value: str | None) -> Any | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text in {" ", "null", "None"}:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def resolve_results_dir(model_dir: Path) -> Path:
    if (model_dir / "results").is_dir():
        return model_dir / "results"
    return model_dir


def job_dirs(results_dir: Path) -> list[Path]:
    if not results_dir.exists():
        raise FileNotFoundError(f"results folder not found: {results_dir}")
    dirs = [
        p for p in results_dir.iterdir()
        if p.is_dir() and any((p / name).exists() for name in (
            "visual.json", "mobile.json", "desktop.json", "usage.json"
        ))
    ]
    return sorted(dirs, key=lambda p: p.name)


def job_id(job_dir: Path) -> str | None:
    name = job_dir.name
    match = re.match(r"^(.+?)_s\d+(?:_|$)", name)
    if match:
        return match.group(1)
    match = re.match(r"^(.+?)_template_", name)
    if match:
        return match.group(1)
    match = re.match(r"^(\d+)(?:_|$)", name)
    return match.group(1) if match else None


def load_baseline_csv(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    rows: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rid = (row.get("ID") or "").strip()
            if not rid:
                continue
            rows[rid] = {
                "mobile": parse_jsonish(row.get("CWV_MOBILE")),
                "desktop": parse_jsonish(row.get("CWV_DESKTOP")),
            }
    return rows


def metric_from_dict(data: Any, metric: str) -> float | None:
    if not isinstance(data, dict):
        return None

    upper = metric.upper()
    key_candidates = {
        "lcp": (
            "LCP_p75", "LCP", "lcp", "largest_contentful_paint",
            "largestContentfulPaint",
        ),
        "inp": (
            "INP_p75", "INP", "inp", "interaction_to_next_paint",
            "interactionToNextPaint",
        ),
        "cls": (
            "CLS_median", "CLS", "cls", "cumulative_layout_shift",
            "cumulativeLayoutShift",
        ),
    }[metric]

    for key in key_candidates:
        value = as_number(data.get(key))
        if value is not None:
            return value

    aggregated = data.get("aggregated")
    if isinstance(aggregated, dict):
        value = metric_from_dict(aggregated, metric)
        if value is not None:
            return value

    metrics_obj = data.get("metrics")
    if isinstance(metrics_obj, dict):
        value = metric_from_dict(metrics_obj, metric)
        if value is not None:
            return value
        psi_names = {
            "lcp": ("LARGEST_CONTENTFUL_PAINT_MS", "largest-contentful-paint"),
            "inp": ("INTERACTION_TO_NEXT_PAINT", "interactive"),
            "cls": ("CUMULATIVE_LAYOUT_SHIFT_SCORE", "cumulative-layout-shift"),
        }[metric]
        for name in psi_names:
            item = metrics_obj.get(name)
            if isinstance(item, dict):
                value = as_number(item.get("percentile") or item.get("numericValue"))
                if value is not None:
                    return value

    audits = data.get("audits")
    if isinstance(audits, dict):
        audit_names = {
            "lcp": ("largest-contentful-paint",),
            "inp": ("interaction-to-next-paint", "interactive"),
            "cls": ("cumulative-layout-shift",),
        }[metric]
        for name in audit_names:
            item = audits.get(name)
            if isinstance(item, dict):
                value = as_number(item.get("numericValue"))
                if value is not None:
                    return value

    if upper in data:
        return as_number(data.get(upper))
    return None


def extract_cwv_values(data: Any, device: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for metric in METRICS:
        value = metric_from_dict(data, metric)
        if value is not None:
            values[f"{metric}_{device}"] = value
    return values


def baseline_result_dir_for_job(job_dir: Path, baseline_results_dir: Path | None) -> Path | None:
    if baseline_results_dir is None:
        return None
    jid = job_id(job_dir)
    if not jid:
        return None
    direct = baseline_results_dir / f"{jid}_template_null"
    if direct.is_dir():
        return direct
    matches = sorted(
        p for p in baseline_results_dir.glob(f"{jid}_*")
        if p.is_dir() and ((p / "mobile.json").exists() or (p / "desktop.json").exists())
    )
    return matches[0] if matches else None


def baseline_for_job(
    job_dir: Path,
    csv_rows: dict[str, dict[str, Any]],
    baseline_results_dir: Path | None = None,
) -> dict[str, float]:
    values: dict[str, float] = {}
    cwv_data = load_json(job_dir / "cwv_data.json")
    if isinstance(cwv_data, dict):
        values.update(extract_cwv_values(cwv_data.get("CWV_BASELINE_MOBILE"), "mobile"))
        values.update(extract_cwv_values(cwv_data.get("CWV_BASELINE_DESKTOP"), "desktop"))

    if len(values) == len(THRESHOLDS):
        return values

    baseline_dir = baseline_result_dir_for_job(job_dir, baseline_results_dir)
    if baseline_dir is not None:
        for device in DEVICES:
            data = load_json(baseline_dir / f"{device}.json")
            for key, value in extract_cwv_values(data, device).items():
                values.setdefault(key, value)

    if len(values) == len(THRESHOLDS):
        return values

    jid = job_id(job_dir)
    csv_row = csv_rows.get(jid or "")
    if csv_row:
        values.update({k: v for k, v in extract_cwv_values(csv_row.get("mobile"), "mobile").items() if k not in values})
        values.update({k: v for k, v in extract_cwv_values(csv_row.get("desktop"), "desktop").items() if k not in values})

    # Optional fallback when a baseline measurement was saved alongside the job.
    for device in DEVICES:
        for candidate in (
            job_dir / f"init_cwv_{device}.json",
            job_dir / f"init_psi_{device}.json",
            job_dir / f"baseline_{device}.json",
        ):
            data = load_json(candidate)
            if data is None:
                continue
            for key, value in extract_cwv_values(data, device).items():
                values.setdefault(key, value)
    return values


def final_for_job(job_dir: Path) -> dict[str, float]:
    values: dict[str, float] = {}
    for device in DEVICES:
        data = load_json(job_dir / f"{device}.json")
        if data is None:
            data = load_json(job_dir / f"final_psi_{device}.json")
        values.update(extract_cwv_values(data, device))
    return values


def health_score(value: float, good: float, needs_improvement: float) -> float:
    if value <= good:
        return 100.0
    if value <= needs_improvement:
        return 100.0 - 50.0 * (value - good) / (needs_improvement - good)
    return max(0.0, 50.0 * (1.0 - (value - needs_improvement) / needs_improvement))


def tier(value: float, good: float, needs_improvement: float) -> int:
    return 2 if value <= good else (1 if value <= needs_improvement else 0)


def cwv_site_metrics(baseline: dict[str, float], final: dict[str, float]) -> dict[str, Any] | None:
    if not all(key in baseline and key in final for key in THRESHOLDS):
        return None

    per_metric: list[dict[str, Any]] = []
    for key, (good, needs_improvement, noise, meaningful) in THRESHOLDS.items():
        initial = baseline[key]
        patched = final[key]
        improvement = initial - patched
        h_initial = health_score(initial, good, needs_improvement)
        h_final = health_score(patched, good, needs_improvement)
        per_metric.append({
            "key": key,
            "initial": initial,
            "final": patched,
            "delta": patched - initial,
            "improvement": improvement,
            "h_init": h_initial,
            "h_final": h_final,
            "h_delta": h_final - h_initial,
            "tier_delta": tier(patched, good, needs_improvement) - tier(initial, good, needs_improvement),
            "improved": improvement >= meaningful,
            "degraded": (-improvement) > noise,
        })

    return {
        "health_delta": mean(m["h_delta"] for m in per_metric),
        "net_threshold_crossing": sum(m["tier_delta"] for m in per_metric),
        "is_pareto": any(m["improved"] for m in per_metric) and not any(m["degraded"] for m in per_metric),
        "is_degraded": any(m["degraded"] for m in per_metric),
        "per_metric": per_metric,
    }


def aggregate_cwv(site_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not site_results:
        return {
            "n_sites": 0,
            "pareto_rate": None,
            "degraded_rate": None,
            "mean_health_delta": None,
            "mean_net_threshold_crossing": None,
            "per_metric": {},
        }
    return {
        "n_sites": len(site_results),
        "pareto_count": sum(1 for row in site_results if row["is_pareto"]),
        "degraded_count": sum(1 for row in site_results if row["is_degraded"]),
        "pareto_rate": mean(row["is_pareto"] for row in site_results),
        "degraded_rate": mean(row["is_degraded"] for row in site_results),
        "mean_health_delta": mean(row["health_delta"] for row in site_results),
        "mean_net_threshold_crossing": mean(row["net_threshold_crossing"] for row in site_results),
        "per_metric": {
            key: {
                "mean_initial": mean(
                    next(m["initial"] for m in row["per_metric"] if m["key"] == key)
                    for row in site_results
                ),
                "mean_final": mean(
                    next(m["final"] for m in row["per_metric"] if m["key"] == key)
                    for row in site_results
                ),
                "mean_delta": mean(
                    next(m["delta"] for m in row["per_metric"] if m["key"] == key)
                    for row in site_results
                ),
                "mean_health_delta": mean(
                    next(m["h_delta"] for m in row["per_metric"] if m["key"] == key)
                    for row in site_results
                ),
                "improved_count": sum(
                    1 for row in site_results
                    if next(m["improved"] for m in row["per_metric"] if m["key"] == key)
                ),
                "degraded_count": sum(
                    1 for row in site_results
                    if next(m["degraded"] for m in row["per_metric"] if m["key"] == key)
                ),
            }
            for key in THRESHOLDS
        },
    }


def visual_summary(jobs: list[Path]) -> dict[str, Any]:
    valid = 0
    regressions = 0
    invalid = 0
    by_signal: dict[str, dict[str, int]] = {}
    for job in jobs:
        visual = load_json(job / "visual.json")
        if not isinstance(visual, dict):
            invalid += 1
            continue
        verdict = visual.get("overall_regression")
        if verdict is None:
            invalid += 1
        else:
            valid += 1
            regressions += 1 if verdict is True else 0
        checks = visual.get("checks")
        if isinstance(checks, dict):
            for name, check in checks.items():
                if not isinstance(check, dict):
                    continue
                slot = by_signal.setdefault(name, {"valid": 0, "true": 0})
                if check.get("regression") is not None:
                    slot["valid"] += 1
                    slot["true"] += 1 if check.get("regression") is True else 0
    return {
        "evaluated": valid,
        "invalid_or_missing": invalid,
        "regression_count": regressions,
        "regression_rate": (regressions / valid) if valid else None,
        "checks": {
            name: {
                **counts,
                "true_rate": (counts["true"] / counts["valid"]) if counts["valid"] else None,
            }
            for name, counts in sorted(by_signal.items())
        },
    }


def patch_stats(jobs: list[Path]) -> dict[str, Any]:
    files = 0
    nonempty = 0
    added = 0
    deleted = 0
    for job in jobs:
        candidates = sorted(job.glob("*.patch"))
        if not candidates:
            continue
        files += 1
        text = candidates[0].read_text(encoding="utf-8", errors="replace")
        if text.strip():
            nonempty += 1
        for line in text.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                deleted += 1
    return {
        "patch_files": files,
        "nonempty_patch_files": nonempty,
        "added_lines": added,
        "deleted_lines": deleted,
        "mean_added_lines": added / files if files else None,
        "mean_deleted_lines": deleted / files if files else None,
    }


def usage_summary(jobs: list[Path]) -> dict[str, Any]:
    rows = [load_json(job / "usage.json") for job in jobs]
    rows = [row for row in rows if isinstance(row, dict)]
    token_totals: dict[str, float] = {}
    cost_values: list[float] = []
    wall_values: list[float] = []
    tool_values: list[float] = []
    for row in rows:
        tokens = row.get("tokens")
        if isinstance(tokens, dict):
            for key, value in tokens.items():
                num = as_number(value)
                if num is not None:
                    token_totals[key] = token_totals.get(key, 0.0) + num
        for key, dest in (
            ("cost_usd", cost_values),
            ("wall_clock_seconds", wall_values),
            ("tool_calls", tool_values),
        ):
            num = as_number(row.get(key))
            if num is not None:
                dest.append(num)
    return {
        "files": len(rows),
        "total_cost_usd": sum(cost_values) if cost_values else None,
        "mean_cost_usd": mean(cost_values) if cost_values else None,
        "mean_wall_clock_seconds": mean(wall_values) if wall_values else None,
        "mean_tool_calls": mean(tool_values) if tool_values else None,
        "tokens": token_totals,
    }


def cwv_summary(
    jobs: list[Path],
    csv_rows: dict[str, dict[str, Any]],
    baseline_results_dir: Path | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    site_results: list[dict[str, Any]] = []
    missing_pairs = 0
    per_job: list[dict[str, Any]] = []
    for job in jobs:
        baseline = baseline_for_job(job, csv_rows, baseline_results_dir)
        final = final_for_job(job)
        metrics = cwv_site_metrics(baseline, final)
        row = {
            "job": job.name,
            "id": job_id(job),
            "baseline_keys": sorted(baseline),
            "final_keys": sorted(final),
            "cwv_metrics": metrics,
        }
        per_job.append(row)
        if metrics is None:
            missing_pairs += 1
        else:
            site_results.append(metrics)
    summary = aggregate_cwv(site_results)
    summary["missing_or_incomplete"] = missing_pairs
    return summary, per_job


def compute(
    model_dir: Path,
    baseline_csv: Path | None,
    baseline_results_dir: Path | None = None,
) -> dict[str, Any]:
    results_dir = resolve_results_dir(model_dir)
    jobs = job_dirs(results_dir)
    csv_rows = load_baseline_csv(baseline_csv)
    cwv, per_job = cwv_summary(jobs, csv_rows, baseline_results_dir)
    return {
        "model_dir": str(model_dir),
        "results_dir": str(results_dir),
        "job_count": len(jobs),
        "visual": visual_summary(jobs),
        "cwv": cwv,
        "patches": patch_stats(jobs),
        "usage": usage_summary(jobs),
        "jobs": per_job,
    }


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def fmt_num(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def print_text(report: dict[str, Any]) -> None:
    print(f"Model folder: {report['model_dir']}")
    print(f"Results dir:   {report['results_dir']}")
    print(f"Jobs:          {report['job_count']}")

    visual = report["visual"]
    print("\nVisual")
    print(f"  evaluated:          {visual['evaluated']}")
    print(f"  invalid/missing:    {visual['invalid_or_missing']}")
    print(f"  regressions:        {visual['regression_count']} ({fmt_pct(visual['regression_rate'])})")
    for name, counts in visual["checks"].items():
        print(f"  {name:<15} {counts['true']:>4}/{counts['valid']:<4} true ({fmt_pct(counts['true_rate'])})")

    cwv = report["cwv"]
    print("\nCWV")
    print(f"  complete pairs:     {cwv['n_sites']}")
    print(f"  incomplete pairs:   {cwv['missing_or_incomplete']}")
    print(f"  pareto rate:        {fmt_pct(cwv['pareto_rate'])}")
    print(f"  degraded rate:      {fmt_pct(cwv['degraded_rate'])}")
    print(f"  mean health delta:  {fmt_num(cwv['mean_health_delta'])}")
    print(f"  mean tier delta:    {fmt_num(cwv['mean_net_threshold_crossing'])}")
    if cwv["per_metric"]:
        print("  per metric:")
        for key, row in cwv["per_metric"].items():
            print(
                f"    {METRIC_LABELS[key]:<12} delta={fmt_num(row['mean_delta'])} "
                f"health={fmt_num(row['mean_health_delta'])} "
                f"improved={row['improved_count']} degraded={row['degraded_count']}"
            )

    patches = report["patches"]
    print("\nPatches")
    print(f"  patch files:        {patches['patch_files']}")
    print(f"  nonempty patches:   {patches['nonempty_patch_files']}")
    print(f"  lines +/-:          +{patches['added_lines']} / -{patches['deleted_lines']}")

    usage = report["usage"]
    print("\nUsage")
    print(f"  usage files:        {usage['files']}")
    print(f"  total cost USD:     {fmt_num(usage['total_cost_usd'], 4)}")
    print(f"  mean wall seconds:  {fmt_num(usage['mean_wall_clock_seconds'], 1)}")
    print(f"  mean tool calls:    {fmt_num(usage['mean_tool_calls'], 1)}")
    if usage["tokens"]:
        token_text = ", ".join(f"{k}={int(v)}" for k, v in sorted(usage["tokens"].items()))
        print(f"  tokens:             {token_text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_dir", type=Path, help="Model output folder or its results/ folder")
    parser.add_argument("--baseline-csv", type=Path, default=None,
                        help="Optional harness CSV with CWV_MOBILE/CWV_DESKTOP baseline columns")
    parser.add_argument("--baseline-results-dir", type=Path, default=None,
                        help="Optional evaluate.sh baseline results folder with <ID>_template_null/mobile.json and desktop.json")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="Output format")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="Optional path to write the full JSON report")
    args = parser.parse_args()

    try:
        report = compute(args.model_dir, args.baseline_csv, args.baseline_results_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
