#!/usr/bin/env python3
"""
Log per-framework statistics for final_sampled_repos.csv:
- Packages distribution (count, min, max, mean, std, percentiles)
- CODE_SIZE distribution
- LOG_SIZE distribution
- SIZE_BIN distribution

Output written to eda/output/eda_log.txt
"""

import json
import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = SCRIPT_DIR.parent / "samples" / "final_sampled_repos.csv"
OUTPUT_DIR = SCRIPT_DIR / "output"


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


def describe_series(s: pd.Series, name: str) -> str:
    """Format series stats for logging."""
    valid = s.dropna()
    if len(valid) == 0:
        return f"  {name}: (no valid data)\n"
    p = valid.quantile([0.25, 0.5, 0.75]).values
    lines = [
        f"  {name}:",
        f"    count: {len(valid)}",
        f"    min: {valid.min():.2f}",
        f"    max: {valid.max():.2f}",
        f"    mean: {valid.mean():.2f}",
        f"    std: {valid.std():.2f}",
        f"    p25: {p[0]:.2f}  p50: {p[1]:.2f}  p75: {p[2]:.2f}",
    ]
    return "\n".join(lines) + "\n"


def log_framework(df: pd.DataFrame, fw: str, buf: list[str]) -> None:
    sub = df[df["FRAMEWORK"] == fw]
    if len(sub) == 0:
        buf.append(f"\n--- {fw} ---\n  (no rows)\n")
        return
    buf.append(f"\n{'='*60}\n{fw} (n={len(sub)})\n{'='*60}\n")
    # Packages
    buf.append(describe_series(sub["n_packages"], "n_packages"))
    # CODE_SIZE
    cs = sub["CODE_SIZE"]
    if cs.notna().any() and (cs > 0).any():
        buf.append(describe_series(np.log10(cs[cs > 0]), "log10(CODE_SIZE)"))
        buf.append(describe_series(cs[cs > 0], "CODE_SIZE (bytes)"))
    else:
        buf.append(f"  CODE_SIZE: (no valid data)\n")
    # LOG_SIZE
    if "LOG_SIZE" in sub.columns:
        buf.append(describe_series(sub["LOG_SIZE"], "LOG_SIZE"))
    # SIZE_BIN
    bin_col = sub.get("SIZE_BIN")
    if bin_col is not None:
        has_bin = bin_col.notna() & (bin_col != "")
        if has_bin.any():
            counts = sub.loc[has_bin, "SIZE_BIN"].value_counts()
            buf.append("  SIZE_BIN distribution:\n")
            for b, c in counts.items():
                buf.append(f"    {b}: {c}\n")
        else:
            buf.append("  SIZE_BIN: (no valid data)\n")
    # Top packages (aggregate)
    all_pkgs = []
    for x in sub["PACKAGES"]:
        if pd.isna(x) or (isinstance(x, str) and x.strip() in ("", "n/a")):
            continue
        try:
            all_pkgs.extend(json.loads(x) if isinstance(x, str) else x)
        except (json.JSONDecodeError, TypeError):
            pass
    if all_pkgs:
        from collections import Counter
        top = Counter(all_pkgs).most_common(15)
        buf.append("  Top 15 packages (across repos):\n")
        for pkg, cnt in top:
            buf.append(f"    {pkg}: {cnt}\n")


def main():
    parser = argparse.ArgumentParser(description="Log per-framework EDA stats for final_sampled_repos.csv")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to final_sampled_repos.csv")
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR, help="Directory for output log file")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.out_dir / "eda_log.txt"
    if not args.csv.exists():
        raise SystemExit(f"CSV not found: {args.csv}")
    df = load_and_prepare(args.csv)
    fw_order = df["FRAMEWORK"].value_counts().index.tolist()
    buf = [
        f"EDA per-framework log: {args.csv.name}",
        f"Generated: {datetime.now().isoformat()}",
        f"Total rows: {len(df)}",
        f"Frameworks (by repo count): {fw_order}",
    ]
    for fw in fw_order:
        log_framework(df, fw, buf)
    buf.append("\n" + "=" * 60 + "\nEND\n")
    out = "".join(buf)
    log_path.write_text(out, encoding="utf-8")
    print(f"Wrote {log_path}")


if __name__ == "__main__":
    main()
