#!/usr/bin/env python3
"""Correlate embedded CrUX p75 values with hosted CWV measurements."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


METRICS = ("LCP", "CLS", "INP", "FCP", "TTFB")


def parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denx == 0 or deny == 0:
        return None
    return num / (denx * deny)


def ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    out = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            out[indexed[k][0]] = rank
        i = j
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return pearson(ranks(xs), ranks(ys))


def round_or_none(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--joined", default="harness/out/crux_hosted_validation_sample/hosted_cwv/hosted_crux_joined.tsv")
    parser.add_argument("--out-dir", default="harness/out/crux_hosted_validation_sample/correlation")
    parser.add_argument("--min-n", type=int, default=10)
    args = parser.parse_args()

    rows = read_tsv(Path(args.joined))
    pair_rows: list[dict[str, Any]] = []
    correlations: list[dict[str, Any]] = []
    for device in ("mobile", "desktop"):
        device_rows = [row for row in rows if row.get("device") == device and row.get("hosted_status") == "success"]
        for metric in METRICS:
            pairs: list[tuple[dict[str, str], float, float]] = []
            for row in device_rows:
                crux = parse_float(row.get(f"crux_{metric}_p75"))
                hosted = parse_float(row.get(f"hosted_{metric}_p75"))
                if crux is not None and hosted is not None:
                    pairs.append((row, crux, hosted))
                    pair_rows.append(
                        {
                            "device": device,
                            "metric": metric,
                            "ID": row.get("ID"),
                            "REPO_ID": row.get("REPO_ID"),
                            "url": row.get("url"),
                            "crux_p75": crux,
                            "hosted_p75": hosted,
                        }
                    )
            xs = [p[1] for p in pairs]
            ys = [p[2] for p in pairs]
            correlations.append(
                {
                    "device": device,
                    "metric": metric,
                    "n": len(pairs),
                    "pearson": round_or_none(pearson(xs, ys)),
                    "spearman": round_or_none(spearman(xs, ys)),
                    "meets_min_n": int(len(pairs) >= args.min_n),
                }
            )

    out_dir = Path(args.out_dir)
    write_tsv(out_dir / "pairs.tsv", pair_rows)
    write_tsv(out_dir / "correlations.tsv", correlations)
    summary = {
        "joined_tsv": args.joined,
        "rows": len(rows),
        "successful_measurements": sum(row.get("hosted_status") == "success" for row in rows),
        "min_n": args.min_n,
        "correlations": correlations,
        "outputs": {
            "pairs": str(out_dir / "pairs.tsv"),
            "correlations": str(out_dir / "correlations.tsv"),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
