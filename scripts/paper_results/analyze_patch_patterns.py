#!/usr/bin/env python3
"""Conservative patch-pattern analysis for SWE-WEB paper results.

This script joins generated patches from ``final_result_dumps`` with the
per-site metrics produced by ``paper_writing/scripts/compute_metrics.py``.

Pattern detection is intentionally conservative:
  * only added patch lines are inspected;
  * HTML optimizations are counted only from parseable tags with required
    attributes on the same tag;
  * ambiguous textual mentions are ignored.

Outputs:
  paper_writing/data/patch_analysis/patch_pattern_per_site.csv
  paper_writing/data/patch_analysis/patch_pattern_summary.csv
  paper_writing/data/patch_analysis/patch_pattern_by_model.json
  paper_writing/data/patch_analysis/patch_pattern_report.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from statistics import mean, median
from typing import Any


ROOT = Path("/dev/shm/ayush/web-experience-benchmark")
DEFAULT_FINAL_DUMPS = ROOT / "final_result_dumps" / "main_bench_rerun_20260615"
DEFAULT_PER_SITE = ROOT / "paper_writing" / "data" / "final_per_site.csv"
DEFAULT_OUT_DIR = ROOT / "paper_writing" / "data" / "patch_analysis"
DEFAULT_SUMMARY_JSON = ROOT / "paper_writing" / "data" / "final_summary.json"

MODEL_DISPLAY = {
    "closed_oc_gpt-4.1": "GPT-4.1",
    "closed_oc_gemini-2-5-pro": "Gemini Pro",
    "closed_oc_gpt-5.1-codex": "GPT-5.1-Codex",
    "closed_oc_gpt-5": "GPT-5",
    "closed_oc_gemini-2-5-flash": "Gemini Flash",
    "closed_cc-sonnet-4.6": "Sonnet 4.6",
    "closed_cc-opus-4.6": "Opus 4.6",
    "open_gemma-4-31b-it": "Gemma 31B",
    "open_glm-4.7-flash": "GLM Flash",
    "open_minimax-m2.7": "Minimax M2.7",
    "open_qwen3.5-122b-a10b": "Qwen3.5-122B",
    "open_qwen3.5-9b": "Qwen3.5-9B",
    "open_qwen3.5-27b": "Qwen3.5-27B",
    "open_qwen3-coder-next": "Qwen3-Coder",
    "open_qwen3.5-35b-a3b": "Qwen3.5-35B",
    "open_devstral-2-123b": "Devstral 123B",
    "open_qwen3.5-397b-a17b": "Qwen3.5-397B",
}

MODEL_ORDER = [
    "closed_oc_gpt-4.1",
    "closed_oc_gemini-2-5-pro",
    "closed_oc_gpt-5.1-codex",
    "closed_oc_gpt-5",
    "closed_oc_gemini-2-5-flash",
    "closed_cc-sonnet-4.6",
    "closed_cc-opus-4.6",
    "open_gemma-4-31b-it",
    "open_glm-4.7-flash",
    "open_minimax-m2.7",
    "open_qwen3.5-122b-a10b",
    "open_qwen3.5-9b",
    "open_qwen3.5-27b",
    "open_qwen3-coder-next",
    "open_qwen3.5-35b-a3b",
    "open_devstral-2-123b",
    "open_qwen3.5-397b-a17b",
]

PATTERN_KEYS = [
    "async_css_print_onload",
    "preload_style",
    "script_defer_or_async",
    "preconnect",
    "dns_prefetch",
    "prefetch",
    "lazy_loading_attr",
    "image_width_height",
    "fetchpriority_attr",
    "font_display_css",
    "content_visibility_css",
    "webp_source",
]


@dataclass
class PatchInfo:
    exists: bool
    size_bytes: int
    files_touched: int
    added_lines: int
    removed_lines: int
    added_text: str
    scan_truncated: bool
    skipped_large: bool


class TagCollector(HTMLParser):
    """Collect start tags from a string fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag.lower(), {k.lower(): (v or "") for k, v in attrs}))


SCAN_EXTENSIONS = {
    ".astro",
    ".css",
    ".htm",
    ".html",
    ".js",
    ".jsx",
    ".less",
    ".liquid",
    ".php",
    ".sass",
    ".scss",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
}

SCAN_BASENAMES = {
    "app",
    "document",
    "index",
    "layout",
    "page",
}


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def compute_regression(visual_path: Path) -> bool | None:
    """Mirror paper_writing/scripts/compute_metrics.py regression logic."""
    data = _safe_json(visual_path)
    if not isinstance(data, dict):
        return None
    checks = data.get("checks", {})
    results = [
        checks.get("structural", {}).get("regression"),
        checks.get("jaccard_text", {}).get("regression"),
        checks.get("gpt_visual", {}).get("regression"),
        checks.get("console_errors", {}).get("regression"),
    ]
    valid = [r for r in results if r is not None]
    if not valid:
        return None
    if len(valid) == 1:
        return bool(valid[0])
    return sum(bool(r) for r in valid) >= 2


def _patch_target_path(line: str) -> str | None:
    if not line.startswith("diff --git "):
        return None
    parts = line.split()
    if len(parts) < 4:
        return None
    target = parts[3]
    if target.startswith("b/"):
        target = target[2:]
    return target


def _scan_patterns_for_path(path: str | None) -> bool:
    if path is None:
        return True
    clean = path.split("\t", 1)[0].strip()
    if not clean or clean == "/dev/null":
        return False
    p = Path(clean)
    suffix = p.suffix.lower()
    if suffix in SCAN_EXTENSIONS:
        return True
    return p.name.lower() in SCAN_BASENAMES


def parse_patch(path: Path | None, max_scan_bytes: int, max_patch_bytes: int) -> PatchInfo:
    if path is None or not path.exists():
        return PatchInfo(False, 0, 0, 0, 0, "", False, False)

    size = path.stat().st_size
    if max_patch_bytes > 0 and size > max_patch_bytes:
        return PatchInfo(True, size, 0, 0, 0, "", True, True)

    files = 0
    added = 0
    removed = 0
    added_parts: list[str] = []
    retained_chars = 0
    scan_truncated = False
    current_scan_enabled = True
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.startswith("diff --git "):
                files += 1
                current_scan_enabled = _scan_patterns_for_path(_patch_target_path(line))
            elif line.startswith("+++") or line.startswith("---"):
                if line.startswith("+++ "):
                    target = line[4:]
                    if target.startswith("b/"):
                        target = target[2:]
                    current_scan_enabled = _scan_patterns_for_path(target)
                continue
            elif line.startswith("+"):
                added += 1
                added_line = line[1:]
                if current_scan_enabled and (max_scan_bytes <= 0 or retained_chars < max_scan_bytes):
                    remaining = max_scan_bytes - retained_chars if max_scan_bytes > 0 else len(added_line) + 1
                    if len(added_line) + 1 <= remaining:
                        added_parts.append(added_line)
                        retained_chars += len(added_line) + 1
                    else:
                        added_parts.append(added_line[:max(0, remaining)])
                        retained_chars = max_scan_bytes
                        scan_truncated = True
                elif current_scan_enabled:
                    scan_truncated = True
            elif line.startswith("-"):
                removed += 1
    return PatchInfo(
        exists=True,
        size_bytes=size,
        files_touched=files,
        added_lines=added,
        removed_lines=removed,
        added_text="\n".join(added_parts),
        scan_truncated=scan_truncated,
        skipped_large=False,
    )


def find_patch(task_dir: Path) -> Path | None:
    expected = task_dir / f"{task_dir.name}.patch"
    if expected.exists():
        return expected
    matches = sorted(task_dir.glob("*.patch"))
    return matches[0] if matches else None


def iter_tag_fragments(added_text: str, max_buffer_chars: int = 4096) -> list[str]:
    """Return complete tag fragments from added lines, with bounded buffering.

    We avoid whole-patch regex searches here. A fragment is emitted only when a
    complete ``<...>`` tag is visible. Incomplete tags are ignored.
    """
    fragments: list[str] = []
    buffer = ""
    for line in added_text.splitlines():
        if "<" not in line and not buffer:
            continue
        text = f"{buffer} {line}".strip() if buffer else line.strip()
        buffer = ""

        start = text.find("<")
        while start != -1:
            if not _plausible_tag_start_context(text, start):
                start = text.find("<", start + 1)
                continue
            end = text.find(">", start + 1)
            if end == -1:
                candidate = text[start:]
                buffer = candidate if len(candidate) <= max_buffer_chars else ""
                break
            candidate = text[start:end + 1]
            if len(candidate) <= max_buffer_chars:
                fragments.append(candidate)
            start = text.find("<", end + 1)
    return fragments


def _plausible_tag_start_context(text: str, start: int) -> bool:
    """Only accept tag starts that look like markup, not quoted text.

    This intentionally rejects ambiguous inline strings such as
    ``const s = "<img loading='lazy'>"``. That can create false negatives for
    generated markup embedded in strings, but avoids false positives from prose,
    comments, fixtures, and JavaScript string literals.
    """
    if start + 1 >= len(text):
        return False
    if text[start + 1] in {"/", "!", "?"}:
        return False
    if not (text[start + 1].isalpha() or text[start + 1] in {"_", ":"}):
        return False

    prefix = text[:start].strip()
    if not prefix:
        return True
    if prefix.endswith(("{", "(", "[", ">", ":", "=", ",")):
        return True
    if prefix in {"return", "=>"}:
        return True
    if prefix.endswith(("return", "=>")) and not any(q in prefix for q in ("'", '"', "`")):
        return True
    return False


def parse_added_tags(added_text: str) -> list[tuple[str, dict[str, str]]]:
    fragments = iter_tag_fragments(added_text)
    if not fragments:
        return []
    parser = TagCollector()
    for fragment in fragments:
        try:
            parser.feed(fragment)
        except Exception:
            continue
    return parser.tags


def _rel_values(attrs: dict[str, str]) -> set[str]:
    return {part.strip().lower() for part in attrs.get("rel", "").split() if part.strip()}


def _has_numeric_attr(attrs: dict[str, str], key: str) -> bool:
    value = attrs.get(key, "").strip()
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", value))


def _attr_assigns_this_media_all(onload: str) -> bool:
    return bool(re.search(r"(?:^|[;\s])this\.media\s*=\s*(['\"])all\1(?:\s*;|\s*$)", onload, re.I))


def _declaration_segments(line: str) -> list[str]:
    """Return declaration-like snippets from one line, excluding comments/strings."""
    line = re.sub(r"/\*.*?\*/", "", line)
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "*", "#")):
        return []

    segments: list[str] = []
    for part in stripped.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        before_colon = part.split(":", 1)[0]
        if any(q in before_colon for q in ("'", '"', "`")):
            continue
        if "{" in before_colon:
            before_colon = before_colon.rsplit("{", 1)[1]
            part = before_colon + ":" + part.split(":", 1)[1]
        segments.append(part.strip())
    return segments


def _css_decl_matches(segment: str, prop: str, allowed_values: set[str]) -> bool:
    name, value = segment.split(":", 1)
    if name.strip().lower() != prop:
        return False
    value = value.strip().rstrip("}").strip().lower()
    if not re.fullmatch(r"[a-z-]+(?:\s*!important)?", value):
        return False
    value = value.replace("!important", "").strip()
    return value in allowed_values


def detect_patterns(added_text: str) -> dict[str, int]:
    """Return conservative binary pattern indicators for added patch content."""
    tags = parse_added_tags(added_text)
    found = {key: 0 for key in PATTERN_KEYS}

    for tag, attrs in tags:
        rels = _rel_values(attrs)

        if tag == "link":
            media = attrs.get("media", "").strip().lower()
            onload = attrs.get("onload", "").lower()
            if (
                "stylesheet" in rels
                and media == "print"
                and _attr_assigns_this_media_all(onload)
            ):
                found["async_css_print_onload"] = 1

            if "preload" in rels and attrs.get("as", "").strip().lower() == "style":
                found["preload_style"] = 1
            if "preconnect" in rels:
                found["preconnect"] = 1
            if "dns-prefetch" in rels:
                found["dns_prefetch"] = 1
            if "prefetch" in rels:
                found["prefetch"] = 1

        if tag == "script" and ("defer" in attrs or "async" in attrs):
            found["script_defer_or_async"] = 1

        if tag in {"img", "iframe"} and attrs.get("loading", "").strip().lower() == "lazy":
            found["lazy_loading_attr"] = 1

        if tag == "img" and _has_numeric_attr(attrs, "width") and _has_numeric_attr(attrs, "height"):
            found["image_width_height"] = 1

        if tag in {"img", "source", "link"} and "fetchpriority" in attrs:
            found["fetchpriority_attr"] = 1

        if tag == "source" and attrs.get("type", "").strip().lower() == "image/webp":
            found["webp_source"] = 1
        if tag in {"img", "source"}:
            srcish = " ".join([attrs.get("src", ""), attrs.get("srcset", "")]).lower()
            if re.search(r"\.webp(?:\s|,|$|\?)", srcish):
                found["webp_source"] = 1

    # CSS property detections are limited to complete declaration snippets.
    # Ambiguous mentions inside strings/comments are ignored; this prefers
    # false negatives over false positives.
    for line in added_text.splitlines():
        lower_line = line.lower()
        if (
            (found["font_display_css"] or "font-display" not in lower_line)
            and (found["content_visibility_css"] or "content-visibility" not in lower_line)
        ):
            continue
        for segment in _declaration_segments(line):
            if not found["font_display_css"] and _css_decl_matches(
                segment,
                "font-display",
                {"swap", "fallback", "optional", "block", "auto"},
            ):
                found["font_display_css"] = 1
            if not found["content_visibility_css"] and _css_decl_matches(
                segment,
                "content-visibility",
                {"auto", "hidden", "visible"},
            ):
                found["content_visibility_css"] = 1
        if found["font_display_css"] and found["content_visibility_css"]:
            break

    return found


def load_per_site(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows[(row["model"], row["site_id"])] = row
    return rows


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def build_rows(final_dumps: Path, per_site_csv: Path, max_scan_bytes: int,
               max_patch_bytes: int, progress: bool = False) -> list[dict[str, Any]]:
    metric_rows = load_per_site(per_site_csv)
    rows: list[dict[str, Any]] = []
    work_items: list[tuple[Path, Path, str, dict[str, Any]]] = []

    for model_dir in sorted(final_dumps.iterdir()):
        if not model_dir.is_dir():
            continue
        model = model_dir.name
        for task_dir in sorted(model_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            site_id = task_dir.name.split("_", 1)[0]
            metrics = metric_rows.get((model, site_id))
            if metrics is None:
                continue
            work_items.append((model_dir, task_dir, site_id, metrics))

    total = len(work_items)
    for index, (model_dir, task_dir, site_id, metrics) in enumerate(work_items, start=1):
        model = model_dir.name
        patch_path = find_patch(task_dir)
        if progress:
            patch_label = patch_path.name if patch_path else "no patch"
            print(
                f"[{index}/{total}] {MODEL_DISPLAY.get(model, model)} "
                f"site={site_id} task={task_dir.name} patch={patch_label}",
                flush=True,
            )
        patch = parse_patch(
            patch_path,
            max_scan_bytes=max_scan_bytes,
            max_patch_bytes=max_patch_bytes,
        )
        patterns = detect_patterns(patch.added_text)
        reg = compute_regression(task_dir / "visual.json")
        health_delta = _to_float(metrics.get("health_delta"))

        row: dict[str, Any] = {
            "model": model,
            "model_display": MODEL_DISPLAY.get(model, model),
            "site_id": site_id,
            "task": task_dir.name,
            "patch_path": str(patch_path) if patch_path else "",
            "patch_exists": int(patch.exists),
            "patch_size_bytes": patch.size_bytes,
            "files_touched": patch.files_touched,
            "added_lines": patch.added_lines,
            "removed_lines": patch.removed_lines,
            "scan_truncated": int(patch.scan_truncated),
            "patch_skipped_large": int(patch.skipped_large),
            "visual_regression": "" if reg is None else int(reg),
            "is_pareto": int(metrics["is_pareto"]),
            "is_degraded": int(metrics["is_degraded"]),
            "health_delta": health_delta,
            "net_threshold_crossing": _to_float(metrics.get("net_threshold_crossing")),
            "cls_mobile_h_delta": _to_float(metrics.get("cls_mobile_h_delta")),
            "cls_desktop_h_delta": _to_float(metrics.get("cls_desktop_h_delta")),
            "lcp_mobile_h_delta": _to_float(metrics.get("lcp_mobile_h_delta")),
            "lcp_desktop_h_delta": _to_float(metrics.get("lcp_desktop_h_delta")),
        }
        row.update(patterns)
        rows.append(row)
    return rows


def pct(rows: list[dict[str, Any]], key: str) -> float | None:
    if not rows:
        return None
    return 100.0 * mean(int(r[key]) for r in rows)


def avg(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [_to_float(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return mean(vals) if vals else None


def med(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [_to_float(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    return median(vals) if vals else None


def load_overall_scores(path: Path) -> dict[str, float]:
    data = _safe_json(path)
    if not isinstance(data, dict):
        return {}
    per_model = data.get("per_model")
    if not isinstance(per_model, dict):
        return {}
    scores: dict[str, float] = {}
    for model, summary in per_model.items():
        if not isinstance(summary, dict):
            continue
        score = _to_float(summary.get("overall_score"))
        if score is not None:
            scores[model] = score
    return scores


def summarize_model(rows: list[dict[str, Any]],
                    overall_scores: dict[str, float] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    overall_scores = overall_scores or {}
    for row in rows:
        by_model[row["model"]].append(row)

    for model in MODEL_ORDER:
        model_rows = by_model.get(model, [])
        if not model_rows:
            continue
        parsed_rows = [r for r in model_rows if not int(r["patch_skipped_large"])]
        summary: dict[str, Any] = {
            "model": model,
            "model_display": MODEL_DISPLAY.get(model, model),
            "n": len(model_rows),
            "pareto_pct": pct(model_rows, "is_pareto"),
            "degraded_pct": pct(model_rows, "is_degraded"),
            "visual_regression_pct": 100.0
            * mean(int(r["visual_regression"]) for r in model_rows if r["visual_regression"] != "")
            if any(r["visual_regression"] != "" for r in model_rows)
            else None,
            "mean_health_delta": avg(model_rows, "health_delta"),
            "overall_score": overall_scores.get(model),
            "patch_skipped_large_pct": pct(model_rows, "patch_skipped_large"),
            "scan_truncated_pct": pct(model_rows, "scan_truncated"),
            "median_files_touched": med(parsed_rows, "files_touched"),
            "median_added_lines": med(parsed_rows, "added_lines"),
            "median_removed_lines": med(parsed_rows, "removed_lines"),
        }
        for key in PATTERN_KEYS:
            matching = [r for r in model_rows if int(r[key])]
            nonmatching = [r for r in model_rows if not int(r[key])]
            summary[f"{key}_pct"] = pct(model_rows, key)
            summary[f"{key}_n"] = len(matching)
            summary[f"{key}_pareto_pct"] = pct(matching, "is_pareto")
            summary[f"{key}_degraded_pct"] = pct(matching, "is_degraded")
            summary[f"no_{key}_pareto_pct"] = pct(nonmatching, "is_pareto")
            summary[f"no_{key}_degraded_pct"] = pct(nonmatching, "is_degraded")
        out.append(summary)
    out.sort(key=lambda row: (-(row["overall_score"] if row["overall_score"] is not None else -1), row["model_display"]))
    return out


def summarize_pattern_global(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in PATTERN_KEYS:
        matching = [r for r in rows if int(r[key])]
        nonmatching = [r for r in rows if not int(r[key])]
        out.append({
            "pattern": key,
            "n_with_pattern": len(matching),
            "pattern_pct": pct(rows, key),
            "pareto_with_pattern_pct": pct(matching, "is_pareto"),
            "degraded_with_pattern_pct": pct(matching, "is_degraded"),
            "health_with_pattern": avg(matching, "health_delta"),
            "pareto_without_pattern_pct": pct(nonmatching, "is_pareto"),
            "degraded_without_pattern_pct": pct(nonmatching, "is_degraded"),
            "health_without_pattern": avg(nonmatching, "health_delta"),
        })
    return out


def summarize_pattern_by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)

    for model in MODEL_ORDER:
        model_rows = by_model.get(model, [])
        if not model_rows:
            continue
        for key in PATTERN_KEYS:
            matching = [r for r in model_rows if int(r[key])]
            nonmatching = [r for r in model_rows if not int(r[key])]
            out.append({
                "model": model,
                "model_display": MODEL_DISPLAY.get(model, model),
                "pattern": key,
                "n_model": len(model_rows),
                "n_with_pattern": len(matching),
                "pattern_pct": pct(model_rows, key),
                "pareto_with_pattern_pct": pct(matching, "is_pareto"),
                "degraded_with_pattern_pct": pct(matching, "is_degraded"),
                "health_with_pattern": avg(matching, "health_delta"),
                "pareto_without_pattern_pct": pct(nonmatching, "is_pareto"),
                "degraded_without_pattern_pct": pct(nonmatching, "is_degraded"),
                "health_without_pattern": avg(nonmatching, "health_delta"),
            })
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value is None or value == "":
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def build_report(model_summary: list[dict[str, Any]],
                 global_patterns: list[dict[str, Any]]) -> str:
    lines = [
        "# Patch Pattern Analysis",
        "",
        "Source: `final_result_dumps/main_bench_rerun_20260615` joined to `paper_writing/data/final_per_site.csv`.",
        "",
        "Pattern detections are conservative and based only on added patch lines. HTML patterns require parseable tags with required attributes on the same tag.",
        "",
        "## Model Summary",
        "",
        "| Model | Overall Score | n | Pareto | Degraded | Vis. Reg. | Large skipped | Median files | Median +lines | Async CSS | Defer/async JS | Lazy loading | Img dims |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in model_summary:
        lines.append(
            "| {model} | {score} | {n} | {pareto} | {degraded} | {vis} | {large} | {files} | {added} | {async_css} | {script} | {lazy} | {dims} |".format(
                model=row["model_display"],
                score=fmt(row.get("overall_score")),
                n=row["n"],
                pareto=fmt(row["pareto_pct"], "%"),
                degraded=fmt(row["degraded_pct"], "%"),
                vis=fmt(row["visual_regression_pct"], "%"),
                large=fmt(row["patch_skipped_large_pct"], "%"),
                files=fmt(row["median_files_touched"]),
                added=fmt(row["median_added_lines"], digits=0),
                async_css=fmt(row["async_css_print_onload_pct"], "%"),
                script=fmt(row["script_defer_or_async_pct"], "%"),
                lazy=fmt(row["lazy_loading_attr_pct"], "%"),
                dims=fmt(row["image_width_height_pct"], "%"),
            )
        )

    lines += [
        "",
        "## Global Pattern Split",
        "",
        "| Pattern | n | Pattern rate | Pareto with | Degraded with | Health with | Pareto without | Degraded without | Health without |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in global_patterns:
        lines.append(
            "| {pattern} | {n} | {rate} | {pw} | {dw} | {hw} | {pwo} | {dwo} | {hwo} |".format(
                pattern=row["pattern"],
                n=row["n_with_pattern"],
                rate=fmt(row["pattern_pct"], "%"),
                pw=fmt(row["pareto_with_pattern_pct"], "%"),
                dw=fmt(row["degraded_with_pattern_pct"], "%"),
                hw=fmt(row["health_with_pattern"], digits=2),
                pwo=fmt(row["pareto_without_pattern_pct"], "%"),
                dwo=fmt(row["degraded_without_pattern_pct"], "%"),
                hwo=fmt(row["health_without_pattern"], digits=2),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--final-dumps", type=Path, default=DEFAULT_FINAL_DUMPS)
    parser.add_argument("--per-site-csv", type=Path, default=DEFAULT_PER_SITE)
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--max-scan-bytes",
        type=int,
        default=0,
        help=(
            "Maximum added-line bytes retained per patch for pattern detection. "
            "The default 0 scans all eligible added lines. A nonzero cap can "
            "cause false negatives, not false positives; patch metadata is "
            "still counted over the full file."
        ),
    )
    parser.add_argument(
        "--max-patch-bytes",
        type=int,
        default=0,
        help=(
            "Skip line/pattern parsing for patches larger than this many bytes. "
            "The default 0 parses every patch fully."
        ),
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print the current model/task/sample while scanning patches.",
    )
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows = build_rows(
        args.final_dumps,
        args.per_site_csv,
        args.max_scan_bytes,
        args.max_patch_bytes,
        progress=args.progress,
    )
    overall_scores = load_overall_scores(args.summary_json)
    model_summary = summarize_model(rows, overall_scores)
    global_patterns = summarize_pattern_global(rows)
    model_pattern_splits = summarize_pattern_by_model(rows)

    write_csv(args.out_dir / "patch_pattern_per_site.csv", rows)
    write_csv(args.out_dir / "patch_pattern_summary.csv", model_summary)
    write_csv(args.out_dir / "patch_pattern_by_model.csv", model_pattern_splits)
    (args.out_dir / "patch_pattern_by_model.json").write_text(
        json.dumps({
            "final_dumps": str(args.final_dumps),
            "per_site_csv": str(args.per_site_csv),
            "summary_json": str(args.summary_json),
            "max_scan_bytes": args.max_scan_bytes,
            "max_patch_bytes": args.max_patch_bytes,
            "patterns": PATTERN_KEYS,
            "model_summary": model_summary,
            "global_patterns": global_patterns,
            "model_pattern_splits": model_pattern_splits,
        }, indent=2),
        encoding="utf-8",
    )
    report = build_report(model_summary, global_patterns)
    (args.out_dir / "patch_pattern_report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Rows written to: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
