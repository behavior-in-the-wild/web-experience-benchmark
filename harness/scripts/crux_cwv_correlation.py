#!/usr/bin/env python3
"""Fetch CrUX field CWV for input_100 live pages and compare to local CWV.

The script uses the Chrome UX Report API directly. By default it queries
URL-level CrUX records for live pages, falls back to origin-level records when
URL-level data is not available, and correlates CrUX p75 values against local
benchmark p75s. Cached raw responses are reused so summaries can be regenerated
without an API key.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
FORM_FACTORS = {"mobile": "PHONE", "desktop": "DESKTOP"}
CRUX_METRICS = {
    "LCP": "largest_contentful_paint",
    "CLS": "cumulative_layout_shift",
    "INP": "interaction_to_next_paint",
}
QUERY_MODES = ("url-then-origin", "url-only", "origin-only")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def percentile(values: list[float], p: float) -> float | None:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


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
    indexed = sorted(enumerate(values), key=lambda x: x[1])
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


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def origin_for(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def crux_query(api_key: str, form_factor: str, *, url: str | None = None, origin: str | None = None) -> tuple[dict[str, Any] | None, str]:
    query_url = f"{CRUX_ENDPOINT}?key={urllib.parse.quote(api_key)}"
    body: dict[str, Any] = {"formFactor": form_factor}
    if url:
        body["url"] = url
    elif origin:
        body["origin"] = origin
    else:
        raise ValueError("url or origin is required")

    req = urllib.request.Request(
        query_url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "web-benchmark-crux-correlation/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read()), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            message = payload.get("error", {}).get("message", raw)
        except Exception:
            message = raw
        return None, f"http_{exc.code}:{message[:200]}"
    except Exception as exc:
        return None, f"{type(exc).__name__}:{str(exc)[:200]}"


def crux_p75(payload: dict[str, Any]) -> dict[str, float | None]:
    metrics = payload.get("record", {}).get("metrics", {})
    out: dict[str, float | None] = {}
    for short, name in CRUX_METRICS.items():
        out[short] = parse_float(metrics.get(name, {}).get("percentiles", {}).get("p75"))
    return out


def local_cwv_p75(path: Path) -> dict[str, float | None]:
    if not path.exists():
        return {"LCP": None, "CLS": None, "INP": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"LCP": None, "CLS": None, "INP": None}
    if data.get("status") != "success":
        return {"LCP": None, "CLS": None, "INP": None}
    runs = [r for r in data.get("runs", []) if isinstance(r, dict) and r.get("status") == "success"]
    out = {}
    for metric in ("LCP", "CLS", "INP"):
        vals = [parse_float(r.get(metric)) for r in runs]
        out[metric] = percentile([v for v in vals if v is not None], 0.75)
    return out


def is_live(row: dict[str, str]) -> bool:
    code = row.get("final_code", "")
    return code.isdigit() and 200 <= int(code) < 400


def round_or_none(value: float | None, ndigits: int = 4) -> float | None:
    return None if value is None else round(value, ndigits)


def cache_path_for(raw_dir: Path, row_id: str, device: str, query_mode: str) -> Path:
    if query_mode == "url-then-origin":
        return raw_dir / f"{row_id}_{device}.json"
    return raw_dir / f"{row_id}_{device}_{query_mode}.json"


def query_crux_with_mode(api_key: str, form_factor: str, url: str, query_mode: str) -> tuple[dict[str, Any] | None, str, str]:
    if query_mode == "url-only":
        payload, error = crux_query(api_key, form_factor, url=url)
        return payload, error, "url"
    if query_mode == "origin-only":
        payload, error = crux_query(api_key, form_factor, origin=origin_for(url))
        return payload, error, "origin"

    payload, error = crux_query(api_key, form_factor, url=url)
    record_type = "url"
    if payload is None:
        record_type = "origin"
        payload, origin_error = crux_query(api_key, form_factor, origin=origin_for(url))
        if payload is None:
            error = f"url={error}; origin={origin_error}"
        else:
            error = ""
    return payload, error, record_type


def has_any_metric(row: dict[str, Any], device: str, prefix: str = "crux") -> bool:
    return any(parse_float(row.get(f"{prefix}_{device}_{metric}_p75")) is not None for metric in ("LCP", "CLS", "INP"))


def write_paper_overlap_summary(out_dir: Path, summary: dict[str, Any], joined_rows: list[dict[str, Any]], min_correlation_n: int) -> dict[str, str]:
    counts = summary["counts"]
    correlation_ns = [row["n"] for row in summary["correlations"]]
    max_n = max(correlation_ns) if correlation_ns else 0
    interpretation = (
        "CrUX overlap is too sparse for a statistically meaningful correlation analysis."
        if max_n < min_correlation_n
        else "CrUX overlap meets the configured minimum n threshold for correlation reporting."
    )

    overlap_rows = [
        {
            "sample": "input_100",
            "input_rows": counts["input_rows"],
            "final_live_pages": counts["final_live_pages"],
            "rows_compared": counts["rows_compared"],
            "crux_mobile_overlap": counts["crux_mobile_any_metric"],
            "crux_desktop_overlap": counts["crux_desktop_any_metric"],
            "local_mobile_success": counts["local_mobile_success"],
            "local_desktop_success": counts["local_desktop_success"],
            "max_correlation_n": max_n,
            "min_correlation_n": min_correlation_n,
            "interpretation": interpretation,
        }
    ]

    coverage_path = out_dir / "paper_crux_overlap.tsv"
    with coverage_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(overlap_rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(overlap_rows)

    covered_path = out_dir / "crux_covered_pages.tsv"
    covered_fields = ["ID", "REPO_ID", "url", "mobile_record_type", "desktop_record_type", "mobile_has_crux", "desktop_has_crux"]
    with covered_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=covered_fields, delimiter="\t")
        writer.writeheader()
        for row in joined_rows:
            mobile_has = has_any_metric(row, "mobile")
            desktop_has = has_any_metric(row, "desktop")
            if not mobile_has and not desktop_has:
                continue
            writer.writerow(
                {
                    "ID": row["ID"],
                    "REPO_ID": row["REPO_ID"],
                    "url": row["url"],
                    "mobile_record_type": row.get("crux_mobile_record_type", ""),
                    "desktop_record_type": row.get("crux_desktop_record_type", ""),
                    "mobile_has_crux": int(mobile_has),
                    "desktop_has_crux": int(desktop_has),
                }
            )

    tex_path = out_dir / "paper_crux_overlap.tex"
    tex_path.write_text(
        "\n".join(
            [
                r"\begin{tabular}{lrrrrr}",
                r"\toprule",
                r"Sample & Input rows & Final-live pages & Mobile CrUX overlap & Desktop CrUX overlap & Max $n$ \\",
                r"\midrule",
                (
                    f"input\\_100 & {counts['input_rows']} & {counts['final_live_pages']} & "
                    f"{counts['crux_mobile_any_metric']} & {counts['crux_desktop_any_metric']} & {max_n} \\\\"
                ),
                r"\bottomrule",
                r"\end{tabular}",
                "",
                f"% {interpretation}",
            ]
        ),
        encoding="utf-8",
    )

    return {
        "paper_overlap_tsv": str(coverage_path),
        "covered_pages_tsv": str(covered_path),
        "paper_overlap_tex": str(tex_path),
        "interpretation": interpretation,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="harness/SAMPLE/input_100.csv")
    parser.add_argument("--live-check", default="harness/out/input_100_githubio_live_check_initial_and_final.tsv")
    parser.add_argument("--baseline-dir", default="final_result_dumps/baselines/cwv_baseline_scores_p20")
    parser.add_argument("--out-dir", default="harness/out/crux_cwv_input100")
    parser.add_argument("--sleep", type=float, default=0.1)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--query-mode", choices=QUERY_MODES, default="url-then-origin")
    parser.add_argument("--min-correlation-n", type=int, default=10)
    args = parser.parse_args()

    repo_root = Path.cwd()
    load_dotenv(repo_root / ".env")
    api_key = args.api_key or os.getenv("GOOGLE_CRUX_API_KEY") or os.getenv("GOOGLE_PAGESPEED_INSIGHTS_API_KEY", "")

    csv.field_size_limit(sys.maxsize)
    input_rows = {r["ID"]: r for r in csv.DictReader(open(args.csv, newline="", encoding="utf-8"))}
    live_rows = list(csv.DictReader(open(args.live_check, newline="", encoding="utf-8"), delimiter="\t"))
    live_rows = [r for r in live_rows if is_live(r)]

    out_dir = Path(args.out_dir)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    joined_rows: list[dict[str, Any]] = []
    for row in live_rows:
        row_id = row["ID"]
        url = row.get("effective_url") or row.get("url")
        record: dict[str, Any] = {
            "ID": row_id,
            "REPO_ID": row["REPO_ID"],
            "url": url,
            "githubio_url": row.get("url", ""),
        }
        for device, form_factor in FORM_FACTORS.items():
            cache_path = cache_path_for(raw_dir, row_id, device, args.query_mode)
            payload: dict[str, Any] | None
            error = ""
            record_type = "url"
            if cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                payload = cached.get("payload")
                error = cached.get("error", "")
                record_type = cached.get("record_type", "url")
            else:
                if not api_key:
                    print(
                        f"Missing GOOGLE_CRUX_API_KEY or GOOGLE_PAGESPEED_INSIGHTS_API_KEY; no cache for {row_id} {device}",
                        file=sys.stderr,
                    )
                    return 2
                payload, error, record_type = query_crux_with_mode(api_key, form_factor, url, args.query_mode)
                cache_path.write_text(
                    json.dumps(
                        {
                            "record_type": record_type,
                            "query_mode": args.query_mode,
                            "url": url,
                            "form_factor": form_factor,
                            "error": error,
                            "payload": payload,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                time.sleep(args.sleep)

            p75s = crux_p75(payload) if payload else {"LCP": None, "CLS": None, "INP": None}
            record[f"crux_{device}_record_type"] = record_type if payload else ""
            record[f"crux_{device}_error"] = error
            for metric, value in p75s.items():
                record[f"crux_{device}_{metric}_p75"] = value

            local = local_cwv_p75(Path(args.baseline_dir) / row_id / f"{device}.json")
            for metric, value in local.items():
                record[f"local_{device}_{metric}_p75"] = value
        joined_rows.append(record)

    wide_path = out_dir / "crux_local_joined.tsv"
    fieldnames = ["ID", "REPO_ID", "url", "githubio_url"]
    for device in ("mobile", "desktop"):
        fieldnames += [f"crux_{device}_record_type", f"crux_{device}_error"]
        for metric in ("LCP", "CLS", "INP"):
            fieldnames += [f"crux_{device}_{metric}_p75", f"local_{device}_{metric}_p75"]
    with wide_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(joined_rows)

    correlations = []
    for device in ("mobile", "desktop"):
        for metric in ("LCP", "CLS", "INP"):
            pairs: list[tuple[float, float]] = []
            for row in joined_rows:
                x = parse_float(row.get(f"crux_{device}_{metric}_p75"))
                y = parse_float(row.get(f"local_{device}_{metric}_p75"))
                if x is not None and y is not None:
                    pairs.append((x, y))
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            correlations.append(
                {
                    "device": device,
                    "metric": metric,
                    "n": len(pairs),
                    "pearson": round_or_none(pearson(xs, ys)),
                    "spearman": round_or_none(spearman(xs, ys)),
                }
            )

    counts = {
        "input_rows": len(input_rows),
        "final_live_pages": len(live_rows),
        "rows_compared": len(joined_rows),
        "crux_mobile_any_metric": sum(has_any_metric(r, "mobile") for r in joined_rows),
        "crux_desktop_any_metric": sum(has_any_metric(r, "desktop") for r in joined_rows),
        "local_mobile_success": sum(has_any_metric(r, "mobile", "local") for r in joined_rows),
        "local_desktop_success": sum(has_any_metric(r, "desktop", "local") for r in joined_rows),
        "crux_record_types": {
            "mobile_url": sum(r.get("crux_mobile_record_type") == "url" for r in joined_rows),
            "mobile_origin": sum(r.get("crux_mobile_record_type") == "origin" for r in joined_rows),
            "desktop_url": sum(r.get("crux_desktop_record_type") == "url" for r in joined_rows),
            "desktop_origin": sum(r.get("crux_desktop_record_type") == "origin" for r in joined_rows),
        },
    }
    summary = {
        "query_mode": args.query_mode,
        "counts": counts,
        "correlations": correlations,
        "joined_tsv": str(wide_path),
    }
    summary["paper_outputs"] = write_paper_overlap_summary(out_dir, summary, joined_rows, args.min_correlation_n)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
