#!/usr/bin/env python3
"""
build_input_jsonl.py

Joins EDSSites_CWV_joined_top50_pages_top10.jsonl (CrUX field data) with
the live_assets_eds/ mirror directory tree to produce:

    harness/SAMPLE/live_input.jsonl

Each output line is a JSON object:
    {
      "id":          "<domain_slug>__<page_slug>",
      "domain":      "https://worldbank.org",
      "page_url":    "https://www.worldbank.org/ext/en/home",
      "mirror_dir":  "worldbank.org/ext__en__home",   # relative to MIRRORS_ROOT
      "baseline": {
          "desktop": { "lcp": ..., "cls": ..., "inp": ..., "ttfb": ... },
          "mobile":  { "lcp": ..., "cls": ..., "inp": ..., "ttfb": ... }
      },
      "mirror_meta": {
          "total_assets": 42,
          "total_bytes":  524288,
          "asset_breakdown": {"js": 5, "css": 3, "img": 20, ...}
      }
    }

Usage:
    python3 scripts/live_pages_benchmark/build_input_jsonl.py \\
        --jsonl  EDSSites_CWV_joined_top50_pages_top10.jsonl \\
        --mirrors live_assets_eds \\
        --output  harness/SAMPLE/live_input.jsonl \\
        [--minified-jsonl .pipeline_work/minified_check.jsonl] \\
        [--comparison-dir comparison_results] \\
        [--max-new-errors 5] \\
        [--limit N]

Resume: existing output is overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


# ── slug helpers (must match fetch_live_assets.py exactly) ───────────────────

def _domain_slug(domain_url: str) -> str:
    parsed = urlparse(domain_url)
    host = (parsed.netloc or parsed.path).rstrip("/")
    # Remove "www." prefix only as an exact string (not char-by-char like lstrip)
    if host.startswith("www."):
        host = host[4:]
    return host or _slugify(domain_url)


def _page_slug(page_url: str) -> str:
    parsed = urlparse(page_url)
    path = parsed.path.strip("/") or "home"
    path = re.sub(r"[^\w.\-/]", "_", path).replace("/", "__")
    if parsed.query:
        query_hash = hashlib.md5(parsed.query.encode()).hexdigest()[:6]
        path += f"__{query_hash}"
    return path[:100] or "home"


def _slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^\w.\-]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_.")
    return text[:max_len]


def _extract_baseline(metrics: list[dict]) -> dict:
    """Extract LCP/CLS/INP/TTFB for desktop and mobile from a metrics array."""
    result: dict[str, dict] = {}
    for m in metrics:
        device = m.get("deviceType", "").lower()
        if device not in ("desktop", "mobile"):
            continue
        result[device] = {
            "lcp":  m.get("lcp"),
            "cls":  m.get("cls"),
            "inp":  m.get("inp"),
            "ttfb": m.get("ttfb"),
        }
    return result


def _mirror_metadata(mirror_abs: Path) -> dict | None:
    """Read manifest.json and compute metadata about the mirror."""
    manifest_path = mirror_abs / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
        assets = manifest.get("assets", [])

        # Asset breakdown by subdirectory type
        breakdown: dict[str, int] = {}
        for a in assets:
            local_path = a.get("local_path", "")
            # e.g. "assets/js/foo.js" → "js"
            parts = local_path.split("/")
            asset_type = parts[1] if len(parts) >= 2 else "other"
            breakdown[asset_type] = breakdown.get(asset_type, 0) + 1

        return {
            "total_assets": manifest.get("total_assets", len(assets)),
            "total_bytes":  manifest.get("total_bytes",
                                         sum(a.get("bytes", 0) for a in assets)),
            "asset_breakdown": breakdown,
            "mirror_timestamp": manifest.get("mirror_timestamp"),
            "browser": manifest.get("browser"),
        }
    except Exception:
        return None


def _canonical_url(url: str) -> str:
    """Normalize URL for deduplication."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{host}{path}"


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[warn] Skipping line {lineno}: {e}", file=sys.stderr)
    return rows


def build(
    jsonl_path: str,
    mirrors_root: str,
    output_path: str,
    limit: int | None,
    minified_jsonl: str | None = None,
    comparison_dir: str | None = None,
    max_new_errors: int = 5,
) -> None:
    mirrors = Path(mirrors_root)
    entries = load_jsonl(jsonl_path)
    print(f"[info] Loaded {len(entries)} domains from {jsonl_path}", file=sys.stderr)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # ── Load minification results ─────────────────────────────────────────
    minified_pages: set[str] = set()
    if minified_jsonl and Path(minified_jsonl).exists():
        with open(minified_jsonl) as f:
            for line in f:
                row = json.loads(line.strip())
                if row.get("is_minified"):
                    minified_pages.add(row.get("page_url", ""))
    print(f"[info] Minified pages to exclude: {len(minified_pages)}", file=sys.stderr)

    # ── Load comparison results ───────────────────────────────────────────
    broken_dirs: set[str] = set()
    if comparison_dir and Path(comparison_dir).exists():
        for p in Path(comparison_dir).rglob("comparison.json"):
            d = json.loads(p.read_text())
            # Support both new format (devices dict) and old flat format
            devices = d.get("devices", {})
            if devices:
                # New format: check each device
                for dev_data in devices.values():
                    new_errs = len(dev_data.get("console_diff", {}).get("new_errors_local", []))
                    if new_errs > max_new_errors:
                        rel = p.parent.relative_to(Path(comparison_dir)).as_posix()
                        broken_dirs.add(rel)
                        break
            else:
                # Old flat format fallback
                new_errs = len(d.get("console_diff", {}).get("new_errors_local", []))
                if new_errs > max_new_errors:
                    rel = p.parent.relative_to(Path(comparison_dir)).as_posix()
                    broken_dirs.add(rel)
    print(f"[info] Broken mirrors to exclude: {len(broken_dirs)}", file=sys.stderr)

    # ── Main join ─────────────────────────────────────────────────────────
    written = 0
    excluded_min = excluded_broken = excluded_both = 0
    excluded_no_mirror = excluded_no_metrics = excluded_dup = 0
    excluded_log: list[tuple[str, str]] = []
    seen_canonical: set[str] = set()

    with open(output, "w", encoding="utf-8") as out:
        for entry in entries:
            if limit is not None and written >= limit:
                break

            domain = entry.get("domain", "")
            pages_raw = entry.get("cwv_top10_pages", [])
            if isinstance(pages_raw, str):
                try:
                    pages_raw = json.loads(pages_raw)
                except Exception:
                    pages_raw = []

            d_slug = _domain_slug(domain)

            for page in pages_raw:
                if limit is not None and written >= limit:
                    break

                page_url = page.get("url", "")
                if not page_url:
                    continue

                # Deduplication by canonical URL
                canon = _canonical_url(page_url)
                if canon in seen_canonical:
                    excluded_dup += 1
                    continue
                seen_canonical.add(canon)

                p_slug = _page_slug(page_url)
                mirror_rel = f"{d_slug}/{p_slug}"
                mirror_abs = mirrors / mirror_rel

                # Gate 1: mirror must exist
                if not (mirror_abs / "index.html").exists():
                    excluded_no_mirror += 1
                    continue

                is_minified = page_url in minified_pages
                is_broken   = mirror_rel in broken_dirs

                # Gate 2+3: minified or broken
                if is_minified or is_broken:
                    if is_minified and is_broken:
                        excluded_both += 1
                        excluded_log.append(("minified+broken", page_url))
                    elif is_minified:
                        excluded_min += 1
                        excluded_log.append(("minified", page_url))
                    else:
                        excluded_broken += 1
                        excluded_log.append(("broken", mirror_rel))
                    continue

                # Gate 4: must have CWV metrics
                baseline = _extract_baseline(page.get("metrics", []))
                if not baseline:
                    excluded_no_metrics += 1
                    continue

                # Mirror metadata
                meta = _mirror_metadata(mirror_abs)

                record = {
                    "id":         f"{d_slug}__{p_slug}",
                    "domain":     domain,
                    "page_url":   page_url,
                    "mirror_dir": mirror_rel,
                    "baseline":   baseline,
                }
                if meta:
                    record["mirror_meta"] = meta
                out.write(json.dumps(record) + "\n")
                written += 1

    # ── Print exclusion log ───────────────────────────────────────────────
    for tag, label in excluded_log:
        print(f"  [exclude/{tag}] {label}", file=sys.stderr)
    print(
        f"[info] Written={written}  excluded: no_mirror={excluded_no_mirror} "
        f"minified={excluded_min} broken={excluded_broken} "
        f"both={excluded_both} no_metrics={excluded_no_metrics} "
        f"duplicates={excluded_dup}",
        file=sys.stderr,
    )


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--jsonl",          required=True,
                   help="EDSSites JSONL file with CrUX data")
    p.add_argument("--mirrors",        required=True,
                   help="Root of live_assets_eds/ mirror tree")
    p.add_argument("--output",         required=True,
                   help="Output JSONL path for harness/SAMPLE/live_input.jsonl")
    p.add_argument("--minified-jsonl", default=None,
                   help="Minification check output (from check_minified_fast.py)")
    p.add_argument("--comparison-dir", default=None,
                   help="Comparison results dir (from compare_local_vs_live.py)")
    p.add_argument("--max-new-errors", type=int, default=5,
                   help="Max new console errors before marking mirror as broken")
    p.add_argument("--limit",          type=int, default=None,
                   help="Max pages to emit (for testing)")
    args = p.parse_args()

    build(
        args.jsonl,
        args.mirrors,
        args.output,
        args.limit,
        minified_jsonl=args.minified_jsonl,
        comparison_dir=args.comparison_dir,
        max_new_errors=args.max_new_errors,
    )


if __name__ == "__main__":
    main()
