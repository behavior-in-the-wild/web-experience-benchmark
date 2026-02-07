#!/usr/bin/env python3
"""
Plot intra-framework statistics for final_sampled_repos.csv (same metrics as eda_log):
- n_packages distribution
- LOG_SIZE / CODE_SIZE distribution
- SIZE_BIN distribution
- Top packages bar chart

Output: eda/output/intra_{framework}.png per framework.

Requires: pip install matplotlib seaborn
"""

import json
import argparse
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = SCRIPT_DIR.parent / "samples" / "final_sampled_repos.csv"
OUTPUT_DIR = SCRIPT_DIR / "output"
BIN_ORDER = ["B2_medium", "B3_large", "B4_extreme"]
BIN_LABELS = {"B2_medium": "p50–p70", "B3_large": "p70–p90", "B4_extreme": "≥p90"}


def package_count(x):
    if pd.isna(x) or (isinstance(x, str) and x.strip() in ("", "n/a")):
        return 0
    if isinstance(x, str):
        try:
            return len(json.loads(x))
        except (json.JSONDecodeError, TypeError):
            return 0
    if isinstance(x, list):
        return len(x)
    return 0


def load_and_prepare(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["n_packages"] = df["PACKAGES"].apply(package_count)
    for col in ("CODE_SIZE", "LOG_SIZE"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def slug(s: str) -> str:
    return s.replace(" ", "_").replace(".", "")


def plot_framework(df: pd.DataFrame, fw: str, out_dir: Path) -> None:
    sub = df[df["FRAMEWORK"] == fw]
    n = len(sub)
    if n == 0:
        return
    # Count packages for this framework (to size figure)
    all_pkgs_list = []
    for x in sub["PACKAGES"]:
        if pd.isna(x) or (isinstance(x, str) and x.strip() in ("", "n/a")):
            continue
        try:
            all_pkgs_list.extend(json.loads(x) if isinstance(x, str) else x)
        except (json.JSONDecodeError, TypeError):
            pass
    n_packages = len(Counter(all_pkgs_list))
    fig_height = max(8, 4 + n_packages * 0.12)
    fig, axes = plt.subplots(2, 2, figsize=(10, fig_height))
    fig.suptitle(f"{fw} (n={n})", fontsize=14)

    # 1. n_packages histogram
    ax = axes[0, 0]
    vals = sub["n_packages"].dropna()
    if len(vals) > 0:
        ax.hist(vals, bins=min(20, int(vals.max()) + 1) if vals.max() > 0 else 5, color="#3498db", edgecolor="black", linewidth=0.3)
    ax.set_title("Packages per repo")
    ax.set_xlabel("n_packages")
    ax.set_ylabel("Count")

    # 2. LOG_SIZE histogram (or log10(CODE_SIZE) for rows with valid CODE_SIZE)
    ax = axes[0, 1]
    log_size = sub["LOG_SIZE"] if "LOG_SIZE" in sub.columns else None
    if log_size is not None and log_size.notna().any():
        ax.hist(log_size.dropna(), bins=20, color="#9b59b6", edgecolor="black", linewidth=0.3)
    else:
        cs = sub["CODE_SIZE"]
        valid = cs.notna() & (cs > 0)
        if valid.any():
            ax.hist(np.log10(cs[valid]), bins=20, color="#9b59b6", edgecolor="black", linewidth=0.3)
    ax.set_title("LOG_SIZE (log10 CODE_SIZE)")
    ax.set_xlabel("log10(bytes)")
    ax.set_ylabel("Count")

    # 3. SIZE_BIN bar chart
    ax = axes[1, 0]
    bin_col = sub.get("SIZE_BIN")
    if bin_col is not None:
        has_bin = bin_col.notna() & (bin_col.astype(str) != "")
        if has_bin.any():
            counts = sub.loc[has_bin, "SIZE_BIN"].value_counts()
            order = [b for b in BIN_ORDER if b in counts.index]
            counts = counts.reindex(order, fill_value=0)
            colors = ["#3498db", "#9b59b6", "#e74c3c"]
            bars = ax.bar(range(len(counts)), counts.values, color=[colors[i % 3] for i in range(len(counts))], edgecolor="black", linewidth=0.3)
            ax.set_xticks(range(len(counts)))
            ax.set_xticklabels([BIN_LABELS.get(b, b) for b in counts.index])
        else:
            ax.text(0.5, 0.5, "No SIZE_BIN data", ha="center", va="center", transform=ax.transAxes)
    else:
        ax.text(0.5, 0.5, "No SIZE_BIN column", ha="center", va="center", transform=ax.transAxes)
    ax.set_title("Size bin distribution")
    ax.set_ylabel("Count")

    # 4. All packages (sorted by count descending)
    ax = axes[1, 1]
    if all_pkgs_list:
        top = Counter(all_pkgs_list).most_common()
        ax.barh(range(len(top)), [c for _, c in top], color="#2ecc71", edgecolor="black", linewidth=0.3)
        ax.set_yticks(range(len(top)))
        ax.set_yticklabels([p for p, _ in top], fontsize=max(4, 10 - len(top) // 20))
        ax.invert_yaxis()
    else:
        ax.text(0.5, 0.5, "No packages data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title("All packages")
    ax.set_xlabel("Count")

    plt.tight_layout()
    out_path = out_dir / f"intra_{slug(fw)}.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {out_path.name}")


def main():
    parser = argparse.ArgumentParser(description="Plot intra-framework stats for final_sampled_repos.csv")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to final_sampled_repos.csv")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR, help="Directory for output plots")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if not args.csv.exists():
        raise SystemExit(f"CSV not found: {args.csv}")
    df = load_and_prepare(args.csv)
    fw_order = df["FRAMEWORK"].value_counts().index.tolist()
    print(f"Plotting {len(fw_order)} frameworks (by repo count): {fw_order}")
    for fw in fw_order:
        plot_framework(df, fw, args.out_dir)
    print(f"Done. Plots in {args.out_dir}")


if __name__ == "__main__":
    main()
