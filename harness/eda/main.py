#!/usr/bin/env python3
"""
Compare input vs output CWV (Core Web Vitals) scores from the harness benchmark.
Loads input.csv (baseline CWV) and output JSON files from results folder,
then logs and plots quantitative comparisons for mobile and desktop.
"""

import json
import logging
import re
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# Paths: run from harness (cd harness && python eda/main.py)
HARNESS_ROOT = Path.cwd()
INPUT_CSV = HARNESS_ROOT / "SAMPLE" / "input.csv"
RESULTS_DIR = HARNESS_ROOT / "out" / "20260208_011310" / "results"
OUTPUT_DIR = HARNESS_ROOT / "eda" / "output"

# CWV metrics to compare (p75 where available; LCP and INP use p75 per CWV spec)
CWV_METRICS = ["LCP_p75", "INP_p75", "CLS_median", "FID_median", "TTFB_median", "FCP_median"]


def metric_display_label(m: str) -> str:
    """Return human-readable label with p75 or median suffix."""
    base = m.replace("_median", "").replace("_p75", "")
    suffix = "(p75)" if "_p75" in m else "(median)"
    return f"{base} {suffix}"


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def parse_cwv_json_str(s: str) -> dict | None:
    """Parse CWV JSON string from input.csv (e.g. CWV_MOBILE, CWV_DESKTOP columns)."""
    if pd.isna(s) or not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s.startswith("{"):
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def extract_metrics_from_aggregated(agg: dict) -> dict:
    """Extract CWV metric values from aggregated dict (input or output format)."""
    if not agg:
        return {}
    return {m: agg.get(m) for m in CWV_METRICS}


def load_output_json(path: Path) -> dict | None:
    """Load CWV output JSON, handling log lines before the JSON."""
    if not path.exists():
        return None
    try:
        content = path.read_text()
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        d = json.loads(match.group())
        if d.get("status") != "success":
            return None
        return d.get("aggregated", {})
    except Exception:
        return None


def load_input_data() -> pd.DataFrame:
    """Load input.csv and parse CWV columns."""
    df = pd.read_csv(INPUT_CSV)
    return df


def build_comparison_df(df: pd.DataFrame) -> pd.DataFrame:
    """Build a DataFrame with input vs output CWV for each ID and device."""
    rows = []
    for _, row in df.iterrows():
        uid = row["ID"]
        for device in ["mobile", "desktop"]:
            col = "CWV_MOBILE" if device == "mobile" else "CWV_DESKTOP"
            in_json = parse_cwv_json_str(row.get(col))
            in_agg = in_json if isinstance(in_json, dict) else {}

            # Output files: {id}_template_codex_{mobile|desktop}.json
            out_path = RESULTS_DIR / f"{uid}_template_codex_{device}.json"
            out_agg = load_output_json(out_path)

            if not out_agg:
                continue

            in_metrics = extract_metrics_from_aggregated(in_agg)
            out_metrics = extract_metrics_from_aggregated(out_agg)

            for m in CWV_METRICS:
                inv = in_metrics.get(m)
                outv = out_metrics.get(m)
                if inv is not None and outv is not None:
                    rows.append(
                        {
                            "ID": uid,
                            "device": device,
                            "metric": m,
                            "input": float(inv),
                            "output": float(outv),
                            "delta": float(outv) - float(inv),
                            "delta_pct": 100 * (float(outv) - float(inv)) / float(inv) if float(inv) != 0 else 0,
                        }
                    )

    return pd.DataFrame(rows)


def log_summary(comp: pd.DataFrame) -> None:
    """Log quantitative summary statistics."""
    log.info("=" * 60)
    log.info("CWV INPUT vs OUTPUT COMPARISON SUMMARY")
    log.info("=" * 60)

    for device in ["mobile", "desktop"]:
        sub = comp[comp["device"] == device]
        if sub.empty:
            log.info(f"\n[{device}] No data")
            continue

        log.info(f"\n--- {device.upper()} ---")
        log.info(f"  Samples (ID × metric pairs): {len(sub)}")
        log.info(f"  Unique IDs: {sub['ID'].nunique()}")

        for m in CWV_METRICS:
            msub = sub[sub["metric"] == m]
            if msub.empty:
                continue
            mean_in = msub["input"].mean()
            mean_out = msub["output"].mean()
            mean_delta = msub["delta"].mean()
            mean_delta_pct = msub["delta_pct"].mean()
            # All CWV metrics: lower is better
            improved = (msub["delta"] < 0).sum()
            n = len(msub)
            log.info(f"  {metric_display_label(m)}: input mean={mean_in:.2f}, output mean={mean_out:.2f}, "
                     f"delta mean={mean_delta:+.2f} ({mean_delta_pct:+.1f}%), improved={improved}/{n}")

    log.info("\n" + "=" * 60)


def plot_comparisons(comp: pd.DataFrame) -> None:
    """Generate comparison plots for mobile and desktop."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for device in ["mobile", "desktop"]:
        sub = comp[comp["device"] == device]
        if sub.empty:
            continue

        metrics = sub["metric"].unique().tolist()
        n_metrics = len(metrics)
        if n_metrics == 0:
            continue

        fig, axes = plt.subplots(2, 3, figsize=(12, 8))
        axes = axes.flatten()

        for i, m in enumerate(metrics):
            if i >= len(axes):
                break
            ax = axes[i]
            msub = sub[sub["metric"] == m].copy()
            msub = msub.sort_values("input")

            ax.scatter(msub["input"], msub["output"], alpha=0.7, s=40, edgecolors="k", linewidths=0.5)
            lims = [
                min(msub["input"].min(), msub["output"].min()),
                max(msub["input"].max(), msub["output"].max()),
            ]
            ax.plot(lims, lims, "k--", alpha=0.5, label="y=x (no change)")
            ax.set_xlabel("Input (baseline)")
            ax.set_ylabel("Output (after optimization)")
            ax.set_title(metric_display_label(m))
            ax.set_aspect("equal", adjustable="box")
            ax.legend(loc="upper right", fontsize=8)
            ax.grid(True, alpha=0.3)

        for j in range(i + 1, len(axes)):
            axes[j].set_visible(False)

        fig.suptitle(f"CWV Input vs Output – {device.capitalize()}", fontsize=14)
        plt.tight_layout()
        out_path = OUTPUT_DIR / f"cwv_input_vs_output_{device}.png"
        plt.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close()
        log.info(f"Saved: {out_path}")

        # Delta distribution
        fig2, axes2 = plt.subplots(2, 3, figsize=(12, 8))
        axes2 = axes2.flatten()
        for i, m in enumerate(metrics):
            if i >= len(axes2):
                break
            ax = axes2[i]
            msub = sub[sub["metric"] == m]
            ax.hist(msub["delta"], bins=min(20, max(5, len(msub) // 3)), edgecolor="black", alpha=0.7)
            ax.axvline(0, color="red", linestyle="--", linewidth=2)
            ax.set_xlabel("Delta (output - input)")
            ax.set_title(metric_display_label(m))
            ax.grid(True, alpha=0.3)

        for j in range(i + 1, len(axes2)):
            axes2[j].set_visible(False)

        fig2.suptitle(f"CWV Delta Distribution – {device.capitalize()}", fontsize=14)
        plt.tight_layout()
        out_path2 = OUTPUT_DIR / f"cwv_delta_dist_{device}.png"
        plt.savefig(out_path2, dpi=150, bbox_inches="tight")
        plt.close()
        log.info(f"Saved: {out_path2}")


def main() -> None:
    log.info("Loading input CSV: %s", INPUT_CSV)
    df = load_input_data()
    log.info("Building comparison with results from: %s", RESULTS_DIR)
    comp = build_comparison_df(df)
    if comp.empty:
        log.warning("No matching input/output pairs found. Check paths and result files.")
        return

    log.info("Matched %d input-output pairs", len(comp))
    log_summary(comp)
    plot_comparisons(comp)
    log.info("Done.")


if __name__ == "__main__":
    main()
