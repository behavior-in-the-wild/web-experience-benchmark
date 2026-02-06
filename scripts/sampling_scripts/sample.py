import json
import os
import numpy as np
import pandas as pd
from datasets import load_dataset

# ============================================================
# CONFIG
# ============================================================

# Sampled subset: 50 Hugo, 50 Hexo, 50 Jekyll; remainder Static HTML (224 total)
SAMPLED_FRAMEWORK_QUOTAS = {
    "Hugo": 50,
    "Hexo": 50,
    "Jekyll": 50,
    "Static HTML": 224 - 50 - 50 - 50,  # 74
}
assert sum(SAMPLED_FRAMEWORK_QUOTAS.values()) == 224

# All other frameworks kept in full (no sampling)
REMAINING_FRAMEWORKS = [
    "Vue",
    "Express",
    "React",
    "Next.js",
    "Pelican",
    "Quarto",
]

# Size-bin ratio (B2:B3:B4) used to split each framework's quota across bins
BIN_RATIO = (60, 72, 92)  # same as former global quotas; normalized per framework
BIN_NAMES = ["B2_medium", "B3_large", "B4_extreme"]

RANDOM_SEED = 42
np.random.seed(RANDOM_SEED)

# ============================================================
# HELPERS
# ============================================================

def compute_log_size(df):
    df = df.copy()
    df = df[pd.notna(df["CODE_SIZE"]) & (df["CODE_SIZE"] > 0)]
    df["LOG_SIZE"] = np.log10(df["CODE_SIZE"])
    return df


def extract_packages(x):
    # HF dataset: PACKAGES is JSON str or "n/a"; legacy "packages" is list[str] or NaN
    if isinstance(x, list):
        return set(x)
    if isinstance(x, str):
        if x == "n/a" or not x.strip():
            return set()
        try:
            return set(json.loads(x))
        except (json.JSONDecodeError, TypeError):
            return set()
    return set()


def assign_bins(df):
    """
    Framework-local bins, starting from p50.
    """
    p50, p70, p90 = np.percentile(df["LOG_SIZE"], [50, 70, 90])

    def bin_fn(v):
        if v < p50:
            return None
        elif v < p70:
            return "B2_medium"
        elif v < p90:
            return "B3_large"
        else:
            return "B4_extreme"

    df = df.copy()
    df["SIZE_BIN"] = df["LOG_SIZE"].apply(bin_fn)

    return df, {"p50": p50, "p70": p70, "p90": p90}


def greedy_diversity_select(df, k):
    """
    Greedy maximization of package diversity.
    No impact, no rarity, no tf-idf.
    """
    rows = df.to_dict("records")
    selected = []
    covered = set()

    while rows and len(selected) < k:
        best_idx = None
        best_gain = -1

        for i, r in enumerate(rows):
            gain = len(r["PACKAGES"] - covered)
            if gain > best_gain:
                best_gain = gain
                best_idx = i

        if best_idx is None:
            break

        r = rows.pop(best_idx)
        selected.append(r)
        covered |= r["PACKAGES"]

    return pd.DataFrame(selected)


# ============================================================
# MAIN
# ============================================================

def main():
    ds = load_dataset("behavior-in-the-wild/cwv-bench-v0", split="train")
    df = pd.DataFrame(ds)

    # Prefer PACKAGES (JSON / "n/a"), fall back to legacy "packages" (list)
    if "PACKAGES" in df.columns:
        df["_packages_parsed"] = df["PACKAGES"].apply(extract_packages)
    elif "packages" in df.columns:
        df["_packages_parsed"] = df["packages"].apply(extract_packages)
    else:
        raise AssertionError("Dataset must have 'PACKAGES' or 'packages' column")
    df["PACKAGES"] = df["_packages_parsed"]
    df = df.drop(columns=["_packages_parsed"])

    assert "FRAMEWORK" in df.columns, "`FRAMEWORK` column missing in dataset"
    assert "CODE_SIZE" in df.columns, "`CODE_SIZE` column missing in dataset"

    # --------------------------------------------------------
    # Split: frameworks we sample (Vue, Hugo, Hexo, Static HTML) vs rest (kept in full)
    # --------------------------------------------------------
    sampled_frameworks = list(SAMPLED_FRAMEWORK_QUOTAS.keys())
    sample_pool_df = df[df["FRAMEWORK"].isin(sampled_frameworks)].copy()
    remaining_df = df[df["FRAMEWORK"].isin(REMAINING_FRAMEWORKS)].copy()

    # --------------------------------------------------------
    # Sample 224: 50 Hugo, 50 Hexo, 50 Jekyll; remainder Static HTML
    # Any shortfall in Hugo/Hexo/Jekyll goes to Static HTML so total = 224.
    # --------------------------------------------------------
    def sample_one_framework(fw_df, quota, framework_name):
        """Sample up to `quota` from fw_df using size bins + greedy diversity."""
        if len(fw_df) == 0 or quota <= 0:
            return pd.DataFrame(), 0
        fw_df = compute_log_size(fw_df)
        if len(fw_df) == 0:
            return pd.DataFrame(), 0
        fw_df, pct = assign_bins(fw_df)
        fw_df = fw_df[pd.notna(fw_df["SIZE_BIN"])]
        take_total = min(quota, len(fw_df))
        # Allocate take_total across bins by BIN_RATIO
        total_ratio = sum(BIN_RATIO)
        sub_quotas = [int(take_total * BIN_RATIO[i] / total_ratio) for i in range(3)]
        sub_quotas[2] += take_total - sum(sub_quotas)  # ensure sum == take_total
        sub_quotas = [max(0, q) for q in sub_quotas]
        picked = []
        spill = 0
        for i, bin_name in enumerate(BIN_NAMES):
            q = sub_quotas[i] + spill
            C = fw_df[fw_df["SIZE_BIN"] == bin_name]
            if len(C) == 0:
                spill = q
                continue
            take = min(q, len(C))
            chosen = greedy_diversity_select(C, take)
            picked.append(chosen)
            spill = q - len(chosen)
        out = pd.concat(picked, ignore_index=True) if picked else pd.DataFrame()
        return out, len(out)

    picked_parts = []
    actual_hugo = actual_hexo = actual_jekyll = 0
    print("\nSampling by framework (50 Hugo, 50 Hexo, 50 Jekyll; remainder Static HTML):")
    for fw in ["Hugo", "Hexo", "Jekyll"]:
        fw_df = sample_pool_df[sample_pool_df["FRAMEWORK"] == fw].copy()
        quota = SAMPLED_FRAMEWORK_QUOTAS[fw]
        chosen, n = sample_one_framework(fw_df, quota, fw)
        if not chosen.empty:
            picked_parts.append(chosen)
        if fw == "Hugo":
            actual_hugo = n
        elif fw == "Hexo":
            actual_hexo = n
        else:
            actual_jekyll = n
        print(f"  {fw}: sampled {n} (quota {quota})")
    # Static HTML: remainder to reach 224
    static_quota = 224 - actual_hugo - actual_hexo - actual_jekyll
    static_df = sample_pool_df[sample_pool_df["FRAMEWORK"] == "Static HTML"].copy()
    chosen_static, n_static = sample_one_framework(static_df, static_quota, "Static HTML")
    if not chosen_static.empty:
        picked_parts.append(chosen_static)
    print(f"  Static HTML: sampled {n_static} (quota {static_quota})")

    sampled_df = pd.concat(picked_parts, ignore_index=True) if picked_parts else pd.DataFrame()
    print("\nSampled rows (Static HTML + Hugo + Hexo + Jekyll):", len(sampled_df))
    if len(sampled_df) < 224:
        print(f"  Warning: only {len(sampled_df)} available (target 224); some framework(s) had fewer rows.")
    assert len(sampled_df) <= 224 and len(sampled_df) >= 50 * 3, (
        f"Sampled subset size unexpected: got {len(sampled_df)}"
    )

    # --------------------------------------------------------
    # Final dataset: sampled + all remaining frameworks
    # --------------------------------------------------------
    FINAL_DF = pd.concat(
        [sampled_df, remaining_df],
        ignore_index=True
    )

    print("Final dataset size:", len(FINAL_DF))

    # Serialize PACKAGES (sets) to JSON strings for CSV
    out_df = FINAL_DF.copy()
    out_df["PACKAGES"] = out_df["PACKAGES"].apply(
        lambda s: json.dumps(sorted(s)) if isinstance(s, set) else s
    )
    os.makedirs("samples", exist_ok=True)
    out_df.to_csv("samples/final_sampled_repos.csv", index=False)
    print("Saved: samples/final_sampled_repos.csv")


if __name__ == "__main__":
    main()
