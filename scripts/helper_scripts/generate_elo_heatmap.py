#!/usr/bin/env python3
"""
Generate Elo-rated model success heatmap for the CWV benchmark.

Data source: paper_writing/data/final_per_site.csv
  - rows present  → non-regressed patch; is_pareto=1 → Pareto, =0 → no improvement
  - rows absent but result dir present → visual regression
  - result dir absent → missing

Elo ratings are computed from pairwise (A, B) per-site comparisons:
  A beats B when A=pareto and B not; draw otherwise.

Output: CodingAgent/figures/fig_elo_heatmap.pdf + .png
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).resolve().parents[2]
PER_SITE    = BASE_DIR / "paper_writing/data/final_per_site.csv"
RESULTS_DIR = BASE_DIR / "final_result_dumps/main_bench"
OUT_DIR     = BASE_DIR / "CodingAgent/figures"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MODEL_SUFFIX = {
    "closed_cc-aider":              "template_aider",
    "closed_cc-codex":              "template_codex",
    "closed_cc-opus-4.6":           "template_claudecode",
    "closed_cc-sonnet-4.6":         "template_claudecode",
    "closed_oc_gemini-2-5-flash":   "template_gemini",
    "closed_oc_gemini-2-5-pro":     "template_gemini",
    "closed_oc_gpt-4.1":            "template_opencodegpt41",
    "closed_oc_gpt-5":              "template_opencode",
    "closed_oc_gpt-5.1-codex":      "template_opencodegpt51codex",
    "open_devstral-2-123b":         "template_opencode_os",
    "open_gemma-4-31b-it":          "template_opencode_os",
    "open_glm-4.7-flash":           "template_opencode_os",
    "open_minimax-m2.7":            "template_opencode_os",
    "open_qwen3-coder-next":        "template_opencode_os",
    "open_qwen3.5-122b-a10b":       "template_opencode_os",
    "open_qwen3.5-27b":             "template_opencode_os",
    "open_qwen3.5-35b-a3b":         "template_opencode_os",
    "open_qwen3.5-397b-a17b":       "template_opencode_os",
    "open_qwen3.5-9b":              "template_opencode_os",
}

MODEL_LABEL = {
    "closed_cc-aider":              "Aider + GPT-5",
    "closed_cc-codex":              "Codex + GPT-5",
    "closed_cc-opus-4.6":           "Claude Code + Opus 4.6",
    "closed_cc-sonnet-4.6":         "Claude Code + Sonnet 4.6",
    "closed_oc_gemini-2-5-flash":   "OpenCode + Gemini 2.5 Flash",
    "closed_oc_gemini-2-5-pro":     "OpenCode + Gemini 2.5 Pro",
    "closed_oc_gpt-4.1":            "OpenCode + GPT-4.1",
    "closed_oc_gpt-5":              "OpenCode + GPT-5",
    "closed_oc_gpt-5.1-codex":      "OpenCode + GPT-5.1 Codex",
    "open_devstral-2-123b":         "OpenCode + Devstral 2 123B",
    "open_gemma-4-31b-it":          "OpenCode + Gemma 4 31B",
    "open_glm-4.7-flash":           "OpenCode + GLM 4.7 Flash",
    "open_minimax-m2.7":            "OpenCode + MiniMax M2.7",
    "open_qwen3-coder-next":        "OpenCode + Qwen3 Coder Next",
    "open_qwen3.5-122b-a10b":       "OpenCode + Qwen3.5 122B-A10B",
    "open_qwen3.5-27b":             "OpenCode + Qwen3.5 27B",
    "open_qwen3.5-35b-a3b":         "OpenCode + Qwen3.5 35B-A3B",
    "open_qwen3.5-397b-a17b":       "OpenCode + Qwen3.5 397B-A17B",
    "open_qwen3.5-9b":              "OpenCode + Qwen3.5 9B",
}

MODELS = sorted(k for k in MODEL_SUFFIX if k not in ("closed_cc-aider", "closed_cc-codex"))

# ---------------------------------------------------------------------------
# Load ground-truth per-site outcomes from paper data
# ---------------------------------------------------------------------------
# paper_outcomes[model][site_id] → "pareto" | "no_improve"
paper_outcomes = defaultdict(dict)
with open(PER_SITE) as f:
    for row in csv.DictReader(f):
        m, sid = row["model"], row["site_id"]
        paper_outcomes[m][sid] = "pareto" if row["is_pareto"] == "1" else "no_improve"

# Collect all site IDs from result dirs
all_sites = set()
model_result_sites = {}  # model → set of site_ids with a result dir
for model in MODELS:
    model_dir = RESULTS_DIR / model
    suffix = MODEL_SUFFIX[model]
    sites = set()
    if model_dir.exists():
        for subdir in model_dir.iterdir():
            if not subdir.is_dir(): continue
            name = subdir.name
            if f"_{suffix}" in name:
                sid = name.replace(f"_{suffix}", "")
                sites.add(sid)
    model_result_sites[model] = sites
    all_sites.update(sites)

# Also include sites from paper_outcomes (in case they differ)
for m in MODELS:
    all_sites.update(paper_outcomes[m].keys())

site_ids = sorted(all_sites, key=lambda s: int(s) if s.isdigit() else s)
print(f"Total sites across all models: {len(site_ids)}")

# ---------------------------------------------------------------------------
# Build per-(model, site) outcome
# ---------------------------------------------------------------------------
def get_outcome(model, sid):
    # Ground-truth non-regressed results from paper data
    if sid in paper_outcomes.get(model, {}):
        return paper_outcomes[model][sid]
    # If result dir exists but not in paper data → visual regression
    if sid in model_result_sites.get(model, set()):
        return "regression"
    return "missing"

outcomes = {m: {sid: get_outcome(m, sid) for sid in site_ids} for m in MODELS}

# Summary
print("\nOutcomes per model:")
for m in MODELS:
    pareto = sum(1 for v in outcomes[m].values() if v == "pareto")
    reg    = sum(1 for v in outcomes[m].values() if v == "regression")
    noimpr = sum(1 for v in outcomes[m].values() if v == "no_improve")
    total  = sum(1 for v in outcomes[m].values() if v != "missing")
    print(f"  {m:40s}  total={total:3d}  pareto={pareto:3d} ({pareto/max(total,1)*100:.0f}%)  "
          f"reg={reg:3d}  no_improve={noimpr}")

# ---------------------------------------------------------------------------
# Elo rating: computed only on sites where BOTH models were evaluated.
# Win condition: pareto beats non-pareto; all other combos are draws.
# Because model coverage is uneven (aider/codex: 30 sites; others: 70-93),
# we also compute the direct Pareto rate for the Y-axis label.
# ---------------------------------------------------------------------------
K = 32

def expected(ra, rb):
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400))

elo = {m: 1000.0 for m in MODELS}
for _ in range(5):
    for sid in site_ids:
        for i, ma in enumerate(MODELS):
            for mb in MODELS[i+1:]:
                oa, ob = outcomes[ma][sid], outcomes[mb][sid]
                if oa == "missing" or ob == "missing":
                    continue
                is_pa = (oa == "pareto")
                is_pb = (ob == "pareto")
                if is_pa == is_pb:
                    continue
                sa = 1.0 if is_pa else 0.0
                sb = 1.0 if is_pb else 0.0
                ea = expected(elo[ma], elo[mb])
                elo[ma] += K * (sa - ea)
                elo[mb] += K * (sb - (1 - ea))

# Pareto rate per model = pareto / (pareto + no_improve)
# Matches paper: computed only over non-regressed patches, same as tab:all_main
pareto_rate = {}
for m in MODELS:
    n_pareto   = sum(1 for v in outcomes[m].values() if v == "pareto")
    n_no_impr  = sum(1 for v in outcomes[m].values() if v == "no_improve")
    pareto_rate[m] = n_pareto / max(n_pareto + n_no_impr, 1)

# Sort primarily by Pareto rate (matches paper ordering),
# use Elo as tiebreaker
models_by_elo = sorted(MODELS, key=lambda m: (pareto_rate[m], elo[m]), reverse=True)
print("\nRanking (by Pareto rate):")
for rank, m in enumerate(models_by_elo, 1):
    print(f"  {rank:2d}. {MODEL_LABEL[m]:45s}  "
          f"Pareto={pareto_rate[m]*100:.1f}%  Elo={elo[m]:.0f}")

# ---------------------------------------------------------------------------
# Sort sites: only keep sites evaluated by >=10 models, then easy→hard
# ---------------------------------------------------------------------------
site_pareto_rate = {
    sid: sum(1 for m in MODELS if outcomes[m][sid] == "pareto") / len(MODELS)
    for sid in site_ids
}
site_coverage = {
    sid: sum(1 for m in MODELS if outcomes[m][sid] != "missing") / len(MODELS)
    for sid in site_ids
}
# Keep only well-evaluated sites (>=10 of 17 models)
sites_filtered = [s for s in site_ids if site_coverage[s] >= 10/17]
sites_sorted = sorted(sites_filtered, key=lambda s: -site_pareto_rate[s])

# ---------------------------------------------------------------------------
# Build RGB matrix
# ---------------------------------------------------------------------------
PALETTE = {
    "pareto":     "#1a9850",   # rich green
    "no_improve": "#f0f0f0",   # near-white gray
    "regression": "#d73027",   # deep red
    "missing":    "#ffffff",   # white
}

n_models = len(models_by_elo)
n_sites  = len(sites_sorted)

matrix_labels = [
    [outcomes[m][sid] for sid in sites_sorted]
    for m in models_by_elo
]

rgb = np.ones((n_models, n_sites, 3))
for r, row in enumerate(matrix_labels):
    for c, label in enumerate(row):
        hx = PALETTE[label]
        rgb[r, c] = [int(hx[1:3],16)/255, int(hx[3:5],16)/255, int(hx[5:7],16)/255]

# ---------------------------------------------------------------------------
# Global style
# ---------------------------------------------------------------------------
import seaborn as sns  # noqa: E402

sns.set_theme(style="white", font="DejaVu Sans")
plt.rcParams.update({
    "font.size": 9,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "pdf.fonttype": 42,   # embed fonts in PDF
    "ps.fonttype": 42,
})

CELL_W, CELL_H = 0.13, 0.38          # inches per cell
fig_w = max(14, n_sites * CELL_W + 5)
fig_h = max(6,  n_models * CELL_H + 2)

fig, ax = plt.subplots(figsize=(fig_w, fig_h))
fig.patch.set_facecolor("white")

# ---------------------------------------------------------------------------
# Heatmap image
# ---------------------------------------------------------------------------
ax.imshow(rgb, aspect="auto", interpolation="nearest", zorder=2)

# Thin white grid lines between cells
ax.set_xticks(np.arange(-0.5, n_sites,  1), minor=True)
ax.set_yticks(np.arange(-0.5, n_models, 1), minor=True)
ax.tick_params(which="minor", length=0)
ax.grid(which="minor", color="white", linewidth=0.4, zorder=3)

# Draw outer border
for spine in ax.spines.values():
    spine.set_visible(True)
    spine.set_linewidth(0.8)
    spine.set_color("#bbbbbb")

# ---------------------------------------------------------------------------
# Y-axis labels — two-column style: left-aligned model name + right stats
# ---------------------------------------------------------------------------
CLOSED = {
    "closed_cc-opus-4.6", "closed_cc-sonnet-4.6",
    "closed_oc_gemini-2-5-flash", "closed_oc_gemini-2-5-pro",
    "closed_oc_gpt-4.1", "closed_oc_gpt-5", "closed_oc_gpt-5.1-codex",
}

ytick_labels = []
for m in models_by_elo:
    pct = f"{pareto_rate[m]*100:.1f}%"
    ytick_labels.append(f"{MODEL_LABEL[m]}   {pct}")

ax.set_yticks(range(n_models))
ax.set_yticklabels(ytick_labels, fontsize=8.5, fontfamily="monospace")
ax.tick_params(axis="y", length=0, pad=6)

# Bold + coloured labels for top-3 by Pareto
top3 = models_by_elo[:3]
for i, m in enumerate(models_by_elo):
    weight = "bold" if m in top3 else "normal"
    color  = "#1a6b3a" if m in top3 else "#222222"
    ax.get_yticklabels()[i].set_fontweight(weight)
    ax.get_yticklabels()[i].set_color(color)

# Horizontal separator: closed vs open-source
closed_indices = [i for i, m in enumerate(models_by_elo) if m in CLOSED]
if closed_indices:
    split_y = max(closed_indices) + 0.5
    ax.axhline(split_y, color="#555555", linewidth=1.2, linestyle="--",
               alpha=0.6, zorder=4)
    ax.text(n_sites + 1, split_y - 0.35, "closed-source ↑", fontsize=7,
            color="#555555", ha="left", va="bottom", style="italic")
    ax.text(n_sites + 1, split_y + 0.35, "open-source ↓", fontsize=7,
            color="#555555", ha="left", va="top", style="italic")

# ---------------------------------------------------------------------------
# X-axis: difficulty tier bands
# ---------------------------------------------------------------------------
ax.set_xticks([])

easy_cut   = sum(1 for s in sites_sorted if site_pareto_rate[s] > 0.33)
medium_cut = sum(1 for s in sites_sorted if site_pareto_rate[s] > 0.10)

tier_info = [
    (0,           easy_cut,   "#1a9850", "Easy"),
    (easy_cut,    medium_cut, "#fc8d59", "Medium"),
    (medium_cut,  n_sites,    "#d73027", "Hard"),
]
for x0, x1, color, label in tier_info:
    if x1 <= x0: continue
    mid = (x0 + x1) / 2
    # bracket line just above the axes (y in axes fraction = 1.03)
    ax.annotate("", xy=(x1 - 0.5, 1.03), xytext=(x0 + 0.5, 1.03),
                xycoords=("data", "axes fraction"),
                textcoords=("data", "axes fraction"),
                arrowprops=dict(arrowstyle="-", color=color, lw=1.8),
                annotation_clip=False)
    ax.text(mid, 1.055, label, ha="center", va="bottom",
            fontsize=8, color=color, fontweight="bold",
            transform=ax.get_xaxis_transform(), clip_on=False)

# Vertical dividers at tier boundaries
for x in [easy_cut - 0.5, medium_cut - 0.5]:
    if 0 < x < n_sites:
        ax.axvline(x, color="#888888", linewidth=1.0, linestyle=":", alpha=0.8, zorder=5)

ax.set_xlabel(
    f"Sites sorted by difficulty (n={n_sites}, ≥10 models evaluated)",
    fontsize=9, labelpad=8
)

# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------
legend_items = [
    mpatches.Patch(fc="#1a9850", ec="none",    label="Pareto improvement"),
    mpatches.Patch(fc="#f0f0f0", ec="#cccccc", label="No improvement"),
    mpatches.Patch(fc="#d73027", ec="none",    label="Visual regression"),
    mpatches.Patch(fc="white",   ec="#aaaaaa", label="Not evaluated"),
]
leg = ax.legend(
    handles=legend_items, loc="lower right",
    fontsize=8.5, frameon=True, framealpha=0.97,
    edgecolor="#cccccc", ncol=2,
    handlelength=1.2, handleheight=1.0,
    borderpad=0.8, labelspacing=0.4,
)
leg.set_zorder(10)

# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------
ax.set_title(
    "Per-Site Success Matrix  ·  Models Ranked by Pareto Rate  ·  Sites Sorted Easy → Hard",
    fontsize=11, fontweight="bold", pad=14, color="#111111",
)

plt.tight_layout(pad=1.5)
for ext in ("pdf", "png"):
    path = OUT_DIR / f"fig_elo_heatmap.{ext}"
    fig.savefig(path, bbox_inches="tight",
                dpi=200 if ext == "png" else None,
                facecolor="white")
    print(f"Saved: {path}")
