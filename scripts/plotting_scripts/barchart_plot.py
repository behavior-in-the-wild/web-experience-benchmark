import json
from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np


# -------------------------------------------------
# CONFIG (EDIT THESE)
# -------------------------------------------------
DUMP_DIR = Path(
    "dumps/cwv_benchmark/mobile_4workers_15runs_20260202_211909"
)

METRIC = "CLS_mean"      # LCP_mean | INP_mean | CLS_mean
DEVICE = "mobile"

# CWV-aware bins
if METRIC == "LCP_mean":
    BINS = [
        0,
        2500,
        4000,
        10000,
        25000,
        float("inf"),
    ]
elif METRIC == "INP_mean":
    BINS = [0, 200, 500, float("inf")]
elif METRIC == "CLS_mean":
    BINS = [0, 0.1, 0.25, float("inf")]

SAVE_PLOT = True
DPI = 150


# -------------------------------------------------
# LOAD checkpoint.jsonl
# -------------------------------------------------
def load_checkpoint(path: Path):
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


# -------------------------------------------------
# EXTRACT METRIC VALUES
# -------------------------------------------------
def extract_values(records):
    values = []

    for r in records:
        cwv_block = r.get(f"cwv_{DEVICE}")
        if not cwv_block:
            continue

        aggregated = cwv_block.get("aggregated")
        if not aggregated:
            continue

        if aggregated.get("valid_runs", 0) == 0:
            continue

        value = aggregated.get(METRIC)
        if value is None:
            continue

        # ---- DROP EXACT ZERO VALUES (INCLUDING CLS == 0) ----
        if value == 0:
            continue

        values.append(value)

    return values


# -------------------------------------------------
# BIN VALUES + COLORS
# -------------------------------------------------
def bin_values(values, bins):
    counts = np.zeros(len(bins) - 1, dtype=int)
    colors = []

    for v in values:
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1]:
                counts[i] += 1
                break

    labels = []
    for i in range(len(bins) - 1):
        left = bins[i]
        right = bins[i + 1]

        # ---- LABEL FORMATTING ----
        if METRIC == "CLS_mean":
            if right == float("inf"):
                labels.append(f">{left:.2f}")
            else:
                labels.append(f"{left:.2f}–{right:.2f}")
        else:
            if right == float("inf"):
                labels.append(f"{int(left)}+")
            else:
                labels.append(f"{int(left)}–{int(right)}")

        # ---- COLOR CODING ----
        if METRIC == "LCP_mean":
            if bins[i] < 2500:
                colors.append("green")
            elif bins[i] < 4000:
                colors.append("gold")
            else:
                colors.append("red")

        elif METRIC == "INP_mean":
            if bins[i] < 200:
                colors.append("green")
            elif bins[i] < 500:
                colors.append("gold")
            else:
                colors.append("red")

        elif METRIC == "CLS_mean":
            if bins[i] < 0.1:
                colors.append("green")
            elif bins[i] < 0.25:
                colors.append("gold")
            else:
                colors.append("red")

    return labels, counts, colors


# -------------------------------------------------
# PLOT
# -------------------------------------------------
def plot_grouped_bar(labels, counts, colors):
    plt.figure(figsize=(10, 5))
    plt.bar(labels, counts, color=colors)

    plt.ylabel("Number of Repositories")
    plt.xlabel(f"{METRIC} range")
    plt.title(f"Distribution of {METRIC} ({DEVICE})")

    # ---- CWV SEMANTIC LEGEND ----
    if METRIC == "LCP_mean":
        legend_title = "LCP CWV Rating"
        legend_elements = [
            Patch(facecolor="green", label="Good (0–2500 ms)"),
            Patch(facecolor="gold", label="Needs Improvement (2500–4000 ms)"),
            Patch(facecolor="red", label="Poor (>4000 ms)"),
        ]

    elif METRIC == "INP_mean":
        legend_title = "INP CWV Rating"
        legend_elements = [
            Patch(facecolor="green", label="Good (0–200 ms)"),
            Patch(facecolor="gold", label="Needs Improvement (200–500 ms)"),
            Patch(facecolor="red", label="Poor (>500 ms)"),
        ]

    elif METRIC == "CLS_mean":
        legend_title = "CLS CWV Rating"
        legend_elements = [
            Patch(facecolor="green", label="Good (0–0.1)"),
            Patch(facecolor="gold", label="Needs Improvement (0.1–0.25)"),
            Patch(facecolor="red", label="Poor (>0.25)"),
        ]

    plt.legend(
        handles=legend_elements,
        title=legend_title,
        title_fontsize=10,
        fontsize=9,
        frameon=True,
    )

    plt.tight_layout()

    if SAVE_PLOT:
        out_path = DUMP_DIR / f"grouped_barchart_{METRIC}.png"
        plt.savefig(out_path, dpi=DPI)
        print(f"Saved plot to {out_path}")
    else:
        plt.show()


# -------------------------------------------------
# MAIN
# -------------------------------------------------
def main():
    checkpoint_path = DUMP_DIR / "checkpoint.jsonl"
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"checkpoint.jsonl not found in {DUMP_DIR}")

    records = load_checkpoint(checkpoint_path)
    values = extract_values(records)

    if not values:
        raise RuntimeError(f"No valid values found for {METRIC}")

    labels, counts, colors = bin_values(values, BINS)
    plot_grouped_bar(labels, counts, colors)


if __name__ == "__main__":
    main()
