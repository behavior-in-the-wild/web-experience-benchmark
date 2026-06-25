#!/usr/bin/env python3
"""Compute main metrics on the maximum-overlap six-model common subset."""
from __future__ import annotations

import csv
import importlib.util
import itertools
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path("/dev/shm/ayush/web-experience-benchmark")
PATCH_ROWS = ROOT / "paper_writing" / "data" / "patch_analysis" / "patch_pattern_per_site.csv"
OUT_DIR = ROOT / "paper_writing" / "data" / "patch_analysis"
SCRIPT_OUT_DIR = ROOT / "scripts" / "paper_results" / "patch_analysis"
COMPUTE_METRICS = ROOT / "paper_writing" / "scripts" / "compute_metrics.py"


def load_compute_metrics():
    spec = importlib.util.spec_from_file_location("compute_metrics", COMPUTE_METRICS)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {COMPUTE_METRICS}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def valid_patch_sets() -> tuple[dict[str, set[str]], dict[str, str]]:
    by_model: dict[str, set[str]] = defaultdict(set)
    display: dict[str, str] = {}
    with PATCH_ROWS.open() as f:
        for row in csv.DictReader(f):
            try:
                patch_size = int(float(row.get("patch_size_bytes") or 0))
            except ValueError:
                patch_size = 0
            if row.get("patch_exists") != "1" or patch_size <= 0:
                continue
            by_model[row["model"]].add(row["site_id"])
            display[row["model"]] = row["model_display"]
    return by_model, display


def best_common_subset(by_model: dict[str, set[str]], k: int = 6) -> tuple[list[str], list[str]]:
    best_combo: tuple[str, ...] | None = None
    best_sites: set[str] = set()
    for combo in itertools.combinations(sorted(by_model), k):
        common = set.intersection(*(by_model[m] for m in combo))
        if best_combo is None or len(common) > len(best_sites):
            best_combo = combo
            best_sites = common
    if best_combo is None:
        raise RuntimeError("No model subset found")
    return list(best_combo), sorted(best_sites, key=lambda x: int(x) if x.isdigit() else x)


def task_for(cm, model: str, site_id: str) -> Path:
    hits = [
        p for p in (cm.FINAL_DUMPS / model).iterdir()
        if p.is_dir() and p.name.split("_")[0] == site_id
    ]
    if len(hits) != 1:
        raise RuntimeError(f"Expected one task for {model}/{site_id}, found {len(hits)}")
    return hits[0]


def aggregate_model(cm, model: str, sites: list[str]) -> dict:
    site_metrics = []
    rerun_clean = []
    reg_true = reg_false = reg_excl = 0
    for site_id in sites:
        task_dir = task_for(cm, model, site_id)
        reg = None
        visual_path = task_dir / "visual.json"
        if visual_path.exists():
            reg = cm.compute_regression(visual_path)
            if reg is None:
                reg_excl += 1
            elif reg:
                reg_true += 1
            else:
                reg_false += 1

        rec = cm.build_site_record(site_id, task_dir)
        if rec is None:
            raise RuntimeError(f"Missing CWV data for {model}/{site_id}")
        sm = cm.site_metrics(rec)
        site_metrics.append(sm)
        if reg is False:
            rerun_clean.append(sm)

    agg = cm.aggregate_model(site_metrics, reg_true, reg_false, reg_excl)
    agg["pareto_rate_raw"] = agg.get("pareto_rate")
    agg["pareto_rate_rerun_reg_removed"] = cm.pareto_rate(rerun_clean)
    agg["n_cwv_rerun_reg_removed"] = len(rerun_clean)
    return agg


def pct(v: float | None) -> str:
    return "" if v is None else f"{100.0 * v:.1f}"


def render_tex(rows: list[dict]) -> str:
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\small",
        r"\setlength{\tabcolsep}{4pt}",
        r"\begin{tabular}{lrrrrrrr}",
        r"\toprule",
        r"Model & $n$ & Vis.\ Reg. & Pareto Raw & Pareto Rerun & Net Thresh. & Health $\Delta$ & Degraded \\",
        r" & & (\%) & (\%) & (\%) & & & (\%) \\",
        r"\midrule",
    ]
    for row in rows:
        lines.append(
            f"{row['model_display']} & {row['n_cwv_sites']} & {row['vis_reg_pct']:.1f} & "
            f"{row['pareto_raw_pct']:.1f} & {row['pareto_rerun_pct']:.1f} & "
            f"{row['net_threshold']:+.2f} & {row['health_delta']:+.2f} & "
            f"{row['degraded_pct']:.1f} \\\\"
        )
    lines += [
        r"\bottomrule",
        r"\end{tabular}",
        r"\caption{Validation slice on the maximum-overlap six-model subset, restricted to templates with valid non-empty patches for all six models.}",
        r"\label{tab:max_overlap_six_validation}",
        r"\end{table}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    cm = load_compute_metrics()
    by_model, display = valid_patch_sets()
    models, sites = best_common_subset(by_model, k=6)

    rows = []
    for model in models:
        agg = aggregate_model(cm, model, sites)
        rows.append({
            "model": model,
            "model_display": display[model],
            "n_common_sites": len(sites),
            "n_cwv_sites": agg["n_cwv_sites"],
            "vis_reg_pct": agg["regression_pct"],
            "pareto_raw_pct": 100.0 * agg["pareto_rate_raw"],
            "pareto_rerun_pct": 100.0 * agg["pareto_rate_rerun_reg_removed"],
            "n_rerun_clean": agg["n_cwv_rerun_reg_removed"],
            "net_threshold": agg["mean_net_threshold_crossing"],
            "health_delta": agg["mean_health_delta"],
            "degraded_pct": 100.0 * agg["degraded_rate"],
        })
    rows.sort(key=lambda r: (-r["pareto_raw_pct"], -r["health_delta"], r["degraded_pct"]))

    payload = {"models": models, "sites": sites, "rows": rows}
    for out_dir in (OUT_DIR, SCRIPT_OUT_DIR):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "max_overlap_six_metrics.json").write_text(json.dumps(payload, indent=2))
        with (out_dir / "max_overlap_six_metrics.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        (out_dir / "max_overlap_six_metrics.tex").write_text(render_tex(rows))

    print(render_tex(rows), end="")


if __name__ == "__main__":
    main()
