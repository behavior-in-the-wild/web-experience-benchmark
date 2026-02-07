#!/usr/bin/env python3
"""
EDA of final_sampled_repos.csv across the metrics used for sampling:
- FRAMEWORK (target vs remaining)
- CODE_SIZE / LOG_SIZE (size-based binning)
- SIZE_BIN (B2_medium, B3_large, B4_extreme)
- PACKAGES (diversity-based selection)

Saves plots to the same directory as this script.

Requires: pip install matplotlib seaborn
"""

import json
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# Default path to CSV (relative to this script)
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = SCRIPT_DIR.parent / "samples" / "final_sampled_repos.csv"
OUTPUT_DIR = SCRIPT_DIR / "output"

# Sampling config (mirror sample.py for labels)
TARGET_FRAMEWORKS = ["Static HTML", "Hexo", "Hugo", "Jekyll"]
BIN_ORDER = ["B2_medium", "B3_large", "B4_extreme"]
BIN_LABELS = {"B2_medium": "p50–p70", "B3_large": "p70–p90", "B4_extreme": "≥p90"}


def load_and_prepare(csv_path: Path) -> pd.DataFrame:
    """Load CSV and derive columns for EDA."""
    df = pd.read_csv(csv_path)
    # Parse PACKAGES to get count (JSON array or "n/a")
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

    df["n_packages"] = df["PACKAGES"].apply(package_count)
    # Ensure numeric
    for col in ("CODE_SIZE", "LOG_SIZE"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def plot_framework_distribution(df: pd.DataFrame) -> None:
    """Bar plot: count by FRAMEWORK."""
    fig, ax = plt.subplots(figsize=(10, 5))
    counts = df["FRAMEWORK"].value_counts()
    colors = [
        "#2ecc71" if fw in TARGET_FRAMEWORKS else "#95a5a6"
        for fw in counts.index
    ]
    counts.plot(kind="bar", ax=ax, color=colors, edgecolor="black", linewidth=0.5)
    ax.set_title("Sample: repos by framework (green = target, gray = remaining)")
    ax.set_ylabel("Count")
    ax.set_xlabel("Framework")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "eda_framework.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("Saved: eda_framework.png")


def plot_size_bin_distribution(df: pd.DataFrame) -> None:
    """Bar plot: count by SIZE_BIN (only rows that have it)."""
    has_bin = df["SIZE_BIN"].notna() & (df["SIZE_BIN"] != "")
    sub = df.loc[has_bin]
    if sub.empty:
        print("Skipping SIZE_BIN plot: no rows with SIZE_BIN")
        return
    fig, ax = plt.subplots(figsize=(6, 4))
    order = [b for b in BIN_ORDER if b in sub["SIZE_BIN"].values]
    counts = sub["SIZE_BIN"].value_counts().reindex(order).fillna(0)
    labels = [BIN_LABELS.get(b, b) for b in counts.index]
    bars = ax.bar(range(len(counts)), counts.values, color=["#3498db", "#9b59b6", "#e74c3c"], edgecolor="black", linewidth=0.5)
    ax.set_xticks(range(len(counts)))
    ax.set_xticklabels(labels)
    ax.set_title("Sample: target frameworks by size bin (LOG_SIZE percentiles)")
    ax.set_ylabel("Count")
    ax.set_xlabel("Size bin")
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "eda_size_bin.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("Saved: eda_size_bin.png")


def plot_framework_by_bin(df: pd.DataFrame) -> None:
    """Stacked bar or heatmap: FRAMEWORK × SIZE_BIN for target frameworks."""
    has_bin = df["SIZE_BIN"].notna() & (df["SIZE_BIN"] != "")
    sub = df.loc[has_bin & df["FRAMEWORK"].isin(TARGET_FRAMEWORKS)]
    if sub.empty:
        print("Skipping framework×bin plot: no target rows with SIZE_BIN")
        return
    ct = pd.crosstab(sub["FRAMEWORK"], sub["SIZE_BIN"])
    ct = ct.reindex(columns=[b for b in BIN_ORDER if b in ct.columns], fill_value=0)
    fig, ax = plt.subplots(figsize=(8, 5))
    ct.plot(kind="bar", stacked=True, ax=ax, color=["#3498db", "#9b59b6", "#e74c3c"], edgecolor="black", linewidth=0.3)
    ax.set_title("Target frameworks: distribution across size bins")
    ax.set_ylabel("Count")
    ax.set_xlabel("Framework")
    ax.legend(title="Size bin", labels=[BIN_LABELS.get(b, b) for b in ct.columns])
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "eda_framework_by_bin.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("Saved: eda_framework_by_bin.png")


def plot_code_size_distribution(df: pd.DataFrame) -> None:
    """Histogram + KDE of LOG_SIZE (and optionally CODE_SIZE)."""
    valid = df["LOG_SIZE"].notna() & (df["LOG_SIZE"] > 0)
    sub = df.loc[valid]
    if sub.empty:
        print("Skipping CODE_SIZE/LOG_SIZE plot: no valid values")
        return
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    # LOG_SIZE
    axes[0].hist(sub["LOG_SIZE"], bins=30, density=True, alpha=0.7, color="#3498db", edgecolor="black", linewidth=0.3)
    sub["LOG_SIZE"].plot(kind="kde", ax=axes[0], color="#2c3e50", linewidth=2)
    axes[0].set_title("LOG_SIZE (log10(CODE_SIZE))")
    axes[0].set_ylabel("Density")
    axes[0].set_xlabel("log10(bytes)")
    # CODE_SIZE (log scale x-axis)
    valid_cs = sub["CODE_SIZE"].notna() & (sub["CODE_SIZE"] > 0)
    if valid_cs.any():
        axes[1].hist(
            np.log10(sub.loc[valid_cs, "CODE_SIZE"]),
            bins=30,
            density=True,
            alpha=0.7,
            color="#9b59b6",
            edgecolor="black",
            linewidth=0.3,
        )
        axes[1].set_title("CODE_SIZE (x-axis log10)")
        axes[1].set_ylabel("Density")
        axes[1].set_xlabel("log10(CODE_SIZE bytes)")
    plt.suptitle("Sample: code size distribution", y=1.02)
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "eda_code_size.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("Saved: eda_code_size.png")


def plot_packages_distribution(df: pd.DataFrame) -> None:
    """Histogram and by-framework view of number of packages per repo."""
    sub = df[df["n_packages"] >= 0].copy()
    if sub.empty or sub["n_packages"].max() == 0 and sub["n_packages"].min() == 0:
        # Still plot if all zeros
        pass
    fig, axes = plt.subplots(2, 1, figsize=(10, 8))
    # Overall histogram
    axes[0].hist(sub["n_packages"], bins=min(40, int(sub["n_packages"].max()) + 1), color="#2ecc71", edgecolor="black", linewidth=0.3)
    axes[0].set_title("Sample: number of PACKAGES per repo")
    axes[0].set_ylabel("Count")
    axes[0].set_xlabel("Number of packages")
    # By framework (box or bar of means)
    fw_order = sub["FRAMEWORK"].value_counts().index.tolist()
    sns.boxplot(data=sub, x="FRAMEWORK", y="n_packages", order=fw_order, ax=axes[1], palette="Set2")
    axes[1].set_title("Packages per repo by framework")
    axes[1].set_ylabel("Number of packages")
    axes[1].set_xlabel("Framework")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "eda_packages.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("Saved: eda_packages.png")


def plot_log_size_by_framework(df: pd.DataFrame) -> None:
    """Box plot: LOG_SIZE by FRAMEWORK."""
    valid = df["LOG_SIZE"].notna() & (df["LOG_SIZE"] > 0)
    sub = df.loc[valid]
    if sub.empty:
        print("Skipping LOG_SIZE by framework: no valid LOG_SIZE")
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    order = sub["FRAMEWORK"].value_counts().index.tolist()
    sns.boxplot(data=sub, x="FRAMEWORK", y="LOG_SIZE", order=order, ax=ax, palette="Set3")
    ax.set_title("Sample: LOG_SIZE (log10 CODE_SIZE) by framework")
    ax.set_ylabel("log10(CODE_SIZE bytes)")
    ax.set_xlabel("Framework")
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    fig.savefig(OUTPUT_DIR / "eda_log_size_by_framework.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("Saved: eda_log_size_by_framework.png")


def print_summary_stats(df: pd.DataFrame) -> None:
    """Print summary statistics to console."""
    print("\n" + "=" * 60)
    print("EDA SUMMARY: final_sampled_repos.csv")
    print("=" * 60)
    print(f"Total rows: {len(df)}")
    print(f"\nFrameworks:\n{df['FRAMEWORK'].value_counts().to_string()}")
    has_bin = df["SIZE_BIN"].notna() & (df["SIZE_BIN"] != "")
    if has_bin.any():
        print(f"\nSize bins (target frameworks):\n{df.loc[has_bin, 'SIZE_BIN'].value_counts().to_string()}")
    if "LOG_SIZE" in df.columns and df["LOG_SIZE"].notna().any():
        print(f"\nLOG_SIZE: min={df['LOG_SIZE'].min():.2f}, max={df['LOG_SIZE'].max():.2f}, mean={df['LOG_SIZE'].mean():.2f}")
    print(f"\nPackages per repo: min={df['n_packages'].min()}, max={df['n_packages'].max()}, mean={df['n_packages'].mean():.1f}")
    print("=" * 60 + "\n")


def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="EDA of final_sampled_repos.csv (sampling metrics).")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to final_sampled_repos.csv")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR, help="Directory to save plots")
    args = parser.parse_args()
    OUTPUT_DIR = args.out_dir
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not args.csv.exists():
        raise SystemExit(f"CSV not found: {args.csv}")
    df = load_and_prepare(args.csv)
    print_summary_stats(df)
    plot_framework_distribution(df)
    plot_size_bin_distribution(df)
    plot_framework_by_bin(df)
    plot_code_size_distribution(df)
    plot_packages_distribution(df)
    plot_log_size_by_framework(df)
    print("Done. Plots saved to:", OUTPUT_DIR)


if __name__ == "__main__":
    main()
