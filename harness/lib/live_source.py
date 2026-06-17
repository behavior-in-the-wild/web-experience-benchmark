#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


def _clean(value: object) -> str:
    if value is None:
        return " "
    text = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return text if text else " "


def _domain_slug(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or parsed.path).rstrip("/")
    return host[4:] if host.startswith("www.") else host


def _page_slug(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "home"
    path = re.sub(r"[^\w.\-/]", "_", path).replace("/", "__")
    if parsed.query:
        path += "__" + hashlib.md5(parsed.query.encode()).hexdigest()[:6]
    return (path[:100] or "home")


def _mirror_dir(url: str, mirrors_root: Path, explicit: str = "") -> str:
    if explicit:
        return explicit
    if not url:
        return ""
    domain = _domain_slug(url)
    primary = f"{domain}/{_page_slug(url)}"
    if (mirrors_root / primary).is_dir():
        return primary
    if (mirrors_root / domain).is_dir():
        return domain
    return primary


def _metric_json(metrics: dict[str, object]) -> str:
    return json.dumps(
        {
            "lcp": metrics.get("lcp"),
            "cls": metrics.get("cls"),
            "inp": metrics.get("inp"),
            "ttfb": metrics.get("ttfb"),
        },
        separators=(",", ":"),
    )


def _row_from_live_json(obj: dict[str, object], mirrors_root: Path) -> list[str]:
    analysis = obj.get("analysis_result") if isinstance(obj.get("analysis_result"), dict) else {}
    input_obj = obj.get("input") if isinstance(obj.get("input"), dict) else {}
    baseline = obj.get("baseline") if isinstance(obj.get("baseline"), dict) else {}
    desktop = baseline.get("desktop") if isinstance(baseline.get("desktop"), dict) else {}
    mobile = baseline.get("mobile") if isinstance(baseline.get("mobile"), dict) else {}

    page_url = (
        obj.get("page_url")
        or input_obj.get("url")
        or analysis.get("url")
        or obj.get("url")
        or ""
    )
    domain = obj.get("domain") or (_domain_slug(str(page_url)) if page_url else "")
    row_id = obj.get("id") or obj.get("row_id") or obj.get("row_number") or domain
    mirror = _mirror_dir(str(page_url), mirrors_root, str(obj.get("mirror_dir") or ""))
    framework = "Static HTML"

    cwv_mobile = _metric_json(mobile)
    cwv_desktop = _metric_json(desktop)

    return [
        _clean(row_id),
        _clean(domain),
        _clean(framework),
        " ",
        " ",
        "host_files/host_static_mirror.sh",
        _clean(cwv_mobile),
        _clean(cwv_desktop),
        "null",
        "null",
        "null",
        "null",
        "null",
        "null",
        " ",
        " ",
        _clean(page_url),
        _clean(mirror),
        _clean(domain),
    ]


def emit_tsv(jsonl: Path, mirrors_root: Path, limit: int | None) -> None:
    written = 0
    with jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            print("\t".join(_row_from_live_json(obj, mirrors_root)))
            written += 1
            if limit is not None and written >= limit:
                break


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert live-bench JSONL rows to evaluate.sh TSV rows.")
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--mirrors-root", required=True)
    parser.add_argument("--limit", default="")
    args = parser.parse_args()
    limit = int(args.limit) if args.limit else None
    emit_tsv(Path(args.jsonl), Path(args.mirrors_root), limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
