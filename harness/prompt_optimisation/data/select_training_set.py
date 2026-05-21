"""
Stratified sample of 60 repos from harness/SAMPLE/input.csv.

Stratification dimensions:
  - framework: jekyll, hugo, express, static, other
  - LCP tier: poor (>4000ms), needs_improvement (2500-4000ms), good (<2500ms)

Outputs data/training_set.csv with 50 train + 10 validation rows.
"""
from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).parent.parent.parent
INPUT_CSV = ROOT / "harness" / "SAMPLE" / "input.csv"
OUTPUT_CSV = Path(__file__).parent / "training_set.csv"

TRAIN_SIZE = 50
VAL_SIZE = 10
SEED = 42

FRAMEWORK_MAP = {
    "jekyll": "jekyll",
    "hugo": "hugo",
    "express": "express",
    "static": "static",
}


def _normalise_framework(fw: str) -> str:
    fw = (fw or "").strip().lower()
    return FRAMEWORK_MAP.get(fw, "other")


def _lcp_tier(mobile_json: str) -> str:
    try:
        d = json.loads(mobile_json) if isinstance(mobile_json, str) else mobile_json
        lcp = float(d.get("LCP_mean") or d.get("LCP_median") or 0)
    except Exception:
        return "unknown"
    if lcp > 4000:
        return "poor"
    if lcp > 2500:
        return "needs_improvement"
    return "good"


def select(
    input_csv: Path = INPUT_CSV,
    output_csv: Path = OUTPUT_CSV,
    train_size: int = TRAIN_SIZE,
    val_size: int = VAL_SIZE,
    seed: int = SEED,
) -> pd.DataFrame:
    rng = random.Random(seed)
    df = pd.read_csv(input_csv)

    # Require non-empty mobile CWV and repo ID
    df = df[df["REPO_ID"].notna() & df["CWV_MOBILE"].notna()]
    df = df[df["CWV_MOBILE"].str.strip() != ""]

    df["_fw"] = df["FRAMEWORK"].apply(_normalise_framework)
    df["_tier"] = df["CWV_MOBILE"].apply(_lcp_tier)
    df["_stratum"] = df["_fw"] + "__" + df["_tier"]

    # Stratified reservoir sample: fill strata proportionally
    total = train_size + val_size
    strata: dict[str, list] = defaultdict(list)
    for _, row in df.iterrows():
        strata[row["_stratum"]].append(row)

    # Sort strata by size desc for stable allocation
    stratum_names = sorted(strata, key=lambda k: -len(strata[k]))
    selected: list = []
    remaining = total
    for i, name in enumerate(stratum_names):
        rows = strata[name]
        n_strata_left = len(stratum_names) - i
        alloc = min(len(rows), max(1, remaining // n_strata_left))
        chosen = rng.sample(rows, alloc)
        selected.extend(chosen)
        remaining -= alloc
        if remaining <= 0:
            break

    # If we still need more (small dataset), fill randomly from remainder
    used_ids = {r["ID"] for r in selected}
    leftovers = [r for _, r in df.iterrows() if r["ID"] not in used_ids]
    if len(selected) < total and leftovers:
        extra = rng.sample(leftovers, min(total - len(selected), len(leftovers)))
        selected.extend(extra)

    rng.shuffle(selected)
    selected = selected[:total]

    out_df = pd.DataFrame(selected).drop(columns=["_fw", "_tier", "_stratum"])
    out_df["split"] = ["train"] * train_size + ["validation"] * val_size

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(output_csv, index=False)
    print(f"Wrote {train_size} train + {val_size} validation rows → {output_csv}")
    return out_df


if __name__ == "__main__":
    select()
