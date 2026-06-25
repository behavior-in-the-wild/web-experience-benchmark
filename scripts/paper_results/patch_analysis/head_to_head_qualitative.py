#!/usr/bin/env python3
"""Reproducible head-to-head qualitative patch analysis.

This script compares selected model pairs on sites where both models have:
  * a valid CWV row,
  * a nonempty patch file.

It writes a paper-facing text section plus machine-readable JSON/CSV outputs.
The analysis intentionally uses deterministic heuristics over saved artifacts;
no LLM judge is used by default.
"""

from __future__ import annotations

import csv
import argparse
import importlib.util
import json
import random
import re
import sys
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path("/dev/shm/ayush/web-experience-benchmark")
FINAL_DUMPS = ROOT / "final_result_dumps" / "main_bench_rerun_20260615"
PER_SITE_CSV = ROOT / "paper_writing" / "data" / "final_per_site.csv"
OUT_DIR = ROOT / "paper_writing" / "data" / "patch_analysis"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PATCH_ANALYSIS = ROOT / "scripts" / "paper_results" / "analyze_patch_patterns.py"
SAMPLE_SEED = 20260618
MAX_SITES_PER_PAIR = 10
PLAN_FILENAMES = ("agent.log_plan.md", "plan.md")
MAX_PATTERN_CANDIDATE_LINES = 4096
MAX_PATTERN_LINE_CHARS = 2000

MODEL_DISPLAY = {
    "open_gemma-4-31b-it": "Gemma 31B",
    "open_glm-4.7-flash": "GLM Flash",
    "closed_cc-opus-4.6": "Opus 4.6",
    "closed_cc-sonnet-4.6": "Sonnet 4.6",
    "open_minimax-m2.7": "MiniMax M2.7",
    "closed_oc_gemini-2-5-pro": "Gemini Pro",
    "closed_oc_gpt-4.1": "GPT-4.1",
}

PAIRS = [
    ("open_gemma-4-31b-it", "closed_cc-opus-4.6", "Gemma 31B vs. Opus 4.6"),
    ("open_gemma-4-31b-it", "open_glm-4.7-flash", "Gemma 31B vs. GLM Flash"),
    ("closed_cc-opus-4.6", "closed_cc-sonnet-4.6", "Opus 4.6 vs. Sonnet 4.6"),
    ("closed_oc_gemini-2-5-pro", "closed_oc_gpt-4.1", "Gemini Pro vs. GPT-4.1"),
    ("open_gemma-4-31b-it", "open_minimax-m2.7", "Gemma 31B vs. MiniMax M2.7"),
]

COMPARISON_MODELS = sorted({model for pair in PAIRS for model in pair[:2]})

PATTERN_LABELS = {
    "async_css_print_onload": "async stylesheet swap",
    "preload_style": "style preload",
    "script_defer_or_async": "deferred/asynchronous scripts",
    "preconnect": "preconnect",
    "dns_prefetch": "DNS prefetch",
    "prefetch": "prefetch",
    "lazy_loading_attr": "lazy loading",
    "image_width_height": "image dimensions",
    "fetchpriority_attr": "fetchpriority",
    "font_display_css": "font-display",
    "content_visibility_css": "content-visibility",
    "webp_source": "WebP source",
}


def load_patch_module():
    spec = importlib.util.spec_from_file_location("patch_patterns", PATCH_ANALYSIS)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import {PATCH_ANALYSIS}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["patch_patterns"] = module
    spec.loader.exec_module(module)
    return module


PATCH = load_patch_module()


def load_per_site() -> dict[tuple[str, str], dict[str, str]]:
    rows: dict[tuple[str, str], dict[str, str]] = {}
    with PER_SITE_CSV.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows[(row["model"], row["site_id"])] = row
    return rows


def task_dir(model: str, site_id: str) -> Path | None:
    matches = sorted((FINAL_DUMPS / model).glob(f"{site_id}_*"))
    return matches[0] if matches else None


def patch_path(directory: Path) -> Path | None:
    expected = directory / f"{directory.name}.patch"
    if expected.exists():
        return expected
    matches = sorted(directory.glob("*.patch"))
    return matches[0] if matches else None


def compute_regression(visual_path: Path) -> bool | None:
    if not visual_path.exists() or visual_path.stat().st_size == 0:
        return None
    return PATCH.compute_regression(visual_path)


def plan_path(directory: Path) -> Path | None:
    for name in PLAN_FILENAMES:
        path = directory / name
        if path.exists() and path.stat().st_size > 0:
            return path
    for path in sorted(directory.glob("*plan*.md")):
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def eligible(
    model: str,
    site_id: str,
    per_site: dict[tuple[str, str], dict[str, str]],
    *,
    require_plan: bool = False,
) -> bool:
    directory = task_dir(model, site_id)
    if directory is None or (model, site_id) not in per_site:
        return False
    patch = patch_path(directory)
    if patch is None or patch.stat().st_size == 0:
        return False
    if require_plan and plan_path(directory) is None:
        return False
    return True


def file_summary(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    current: str | None = None
    added = 0
    removed = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                out.append({"file": current, "added": added, "removed": removed})
            parts = line.split()
            current = parts[3][2:] if len(parts) > 3 and parts[3].startswith("b/") else parts[-1]
            added = 0
            removed = 0
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if current is not None:
        out.append({"file": current, "added": added, "removed": removed})
    return out


def patch_profile(path: Path, max_snippets: int = 4) -> dict[str, Any]:
    keywords = (
        "preconnect",
        "dns-prefetch",
        "prefetch",
        "preload",
        "stylesheet",
        "loading=",
        "fetchpriority",
        "font-display",
        "content-visibility",
        "defer",
        "async",
        "width=",
        "height=",
        "webp",
        "media=\"print",
        "media='print",
        "onload",
        "settimeout",
        "display",
    )
    files: list[dict[str, Any]] = []
    pattern_hits = {key: 0 for key in PATCH.PATTERN_KEYS}
    pattern_batch: list[str] = []
    candidate_lines_seen = 0
    snippets: list[str] = []
    current: str | None = None
    added = 0
    removed = 0

    def flush_patterns() -> None:
        if not pattern_batch:
            return
        detected = PATCH.detect_patterns("\n".join(pattern_batch))
        for key, value in detected.items():
            if value:
                pattern_hits[key] = 1
        pattern_batch.clear()

    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if line.startswith("diff --git "):
                if current is not None:
                    files.append({"file": current, "added": added, "removed": removed})
                parts = line.split()
                current = parts[3][2:] if len(parts) > 3 and parts[3].startswith("b/") else parts[-1]
                added = 0
                removed = 0
            elif line.startswith("+") and not line.startswith("+++"):
                added += 1
                added_line = line[1:]
                lower = added_line.lower()
                if any(key in lower for key in keywords):
                    compact_source = added_line[:MAX_PATTERN_LINE_CHARS]
                    compact = re.sub(r"\s+", " ", compact_source.strip())
                    if candidate_lines_seen < MAX_PATTERN_CANDIDATE_LINES and not all(pattern_hits.values()):
                        pattern_batch.append(compact)
                        candidate_lines_seen += 1
                        if len(pattern_batch) >= 512:
                            flush_patterns()
                    if len(snippets) < max_snippets:
                        snippets.append(compact[:220])
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1

    if current is not None:
        files.append({"file": current, "added": added, "removed": removed})
    flush_patterns()

    return {
        "files": files,
        "patterns": pattern_hits,
        "snippets": snippets,
    }


def plan_excerpt(path: Path | None, max_items: int = 4) -> list[str]:
    if path is None:
        return []
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = raw.strip().strip("-* ")
        if not text or text.startswith("#"):
            continue
        text = re.sub(r"\s+", " ", text)
        lines.append(text)
        if len(lines) >= max_items:
            break
    return lines


def strategy_tags(patterns: dict[str, int], files: list[dict[str, Any]], plan: list[str]) -> list[str]:
    tags: list[str] = []
    active = {key for key, value in patterns.items() if value}
    if {"preconnect", "dns_prefetch", "prefetch"} & active:
        tags.append("connection warming")
    if {"preload_style", "async_css_print_onload"} & active:
        tags.append("stylesheet scheduling")
    if "script_defer_or_async" in active:
        tags.append("script scheduling")
    if {"lazy_loading_attr", "image_width_height", "fetchpriority_attr", "webp_source"} & active:
        tags.append("image handling")
    if {"font_display_css", "content_visibility_css"} & active:
        tags.append("CSS rendering hints")
    plan_text = " ".join(plan).lower()
    if any(term in plan_text for term in ("loading screen", "settimeout", "animation", "display: none")):
        tags.append("animation/loading-gate diagnosis")
    if len(files) <= 1:
        tags.append("single-file patch")
    elif len(files) >= 4:
        tags.append("multi-file patch")
    return tags or ["localized edit"]


def plan_theme(plan: list[str]) -> str:
    if not plan:
        return "plan unavailable; strategy inferred from patch"
    text = " ".join(plan).lower()
    if any(term in text for term in ("loading screen", "settimeout", "animation", "display: none")):
        return "loading gate or animation delay"
    if any(term in text for term in ("render-blocking", "stylesheet", "css chain", "css files")):
        return "render-blocking stylesheet path"
    if any(term in text for term in ("lcp element", "`img`", " image", "logo", "hero")):
        return "image/LCP element handling"
    if any(term in text for term in ("font", "google font", "webfont")):
        return "font loading"
    if any(term in text for term in ("script", "javascript", "main thread", "blocking")):
        return "script loading or main-thread work"
    if any(term in text for term in ("cls", "layout shift", "width", "height")):
        return "layout stability"
    return "metric-level optimization"


def artifact(model: str, site_id: str, per_site: dict[tuple[str, str], dict[str, str]]) -> dict[str, Any]:
    directory = task_dir(model, site_id)
    if directory is None:
        raise RuntimeError(f"Missing directory for {model} {site_id}")
    patch = patch_path(directory)
    if patch is None:
        raise RuntimeError(f"Missing patch for {model} {site_id}")

    profile = patch_profile(patch)
    patterns = profile["patterns"]
    files = profile["files"]
    row = per_site[(model, site_id)]
    plan_file = plan_path(directory)
    plan = plan_excerpt(plan_file)
    visual = compute_regression(directory / "visual.json")
    return {
        "model": model,
        "model_display": MODEL_DISPLAY.get(model, model),
        "site_id": site_id,
        "task": directory.name,
        "patch_path": str(patch.relative_to(ROOT)),
        "patch_bytes": patch.stat().st_size,
        "files_touched": len(files),
        "added_lines": sum(item["added"] for item in files),
        "removed_lines": sum(item["removed"] for item in files),
        "file_summary": files,
        "patterns": [PATTERN_LABELS[key] for key, value in patterns.items() if value],
        "strategy_tags": strategy_tags(patterns, files, plan),
        "plan_theme": plan_theme(plan),
        "plan_available": plan_file is not None,
        "plan_path": str(plan_file.relative_to(ROOT)) if plan_file is not None else "",
        "plan_excerpt": plan,
        "snippets": profile["snippets"],
        "is_pareto": int(row["is_pareto"]),
        "is_degraded": int(row["is_degraded"]),
        "health_delta": float(row["health_delta"]),
        "net_threshold_crossing": float(row["net_threshold_crossing"]),
        "visual_regression": visual,
    }


def tex_escape(value: Any) -> str:
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def outcome_label(item: dict[str, Any]) -> str:
    if item["is_pareto"]:
        return "Pareto"
    if item["is_degraded"]:
        return "Degraded"
    return "Neutral"


def visual_label(item: dict[str, Any]) -> str:
    if item["visual_regression"] is True:
        return "VR"
    if item["visual_regression"] is False:
        return "clean"
    return "n/a"


def tactic_label(item: dict[str, Any], max_tags: int = 3) -> str:
    tags = [tag for tag in item["strategy_tags"] if tag not in {"single-file patch", "multi-file patch"}]
    if not tags:
        tags = item["strategy_tags"]
    label = ", ".join(tags[:max_tags])
    if len(tags) > max_tags:
        label += ", ..."
    return label


def patch_shape(item: dict[str, Any]) -> str:
    return f"{item['files_touched']}f/{item['added_lines']}+"


def pair_stats(pair: dict[str, Any]) -> dict[str, Any]:
    records = pair["records"]
    out: dict[str, Any] = {"n": len(records)}
    for side in ("a", "b"):
        items = [record[side] for record in records]
        out[side] = {
            "pareto": sum(item["is_pareto"] for item in items),
            "degraded": sum(item["is_degraded"] for item in items),
            "health": mean(item["health_delta"] for item in items) if items else 0.0,
            "lines": mean(item["added_lines"] for item in items) if items else 0.0,
            "files": mean(item["files_touched"] for item in items) if items else 0.0,
            "visual_reg": sum(1 for item in items if item["visual_regression"] is True),
            "large": sum(1 for item in items if item["files_touched"] >= 25 or item["added_lines"] >= 500),
        }
    return out


def pair_summary_table(pair_outputs: list[dict[str, Any]]) -> list[str]:
    lines = [
        r"\begin{table*}[t]",
        r"\centering",
        r"\small",
        r"\setlength{\tabcolsep}{4pt}",
        r"\begin{adjustbox}{max width=\linewidth}",
        r"\begin{tabular}{llrrrrrrrr}",
        r"\toprule",
        r"Pair & Model & Pareto & Degraded & Mean $H\Delta$ & Avg. +Lines & Avg. Files & Vis. Reg. & Large Patches & Common Sites \\",
        r"\midrule",
    ]
    for pair in pair_outputs:
        stats = pair_stats(pair)
        for side, label in (("a", pair["model_a_display"]), ("b", pair["model_b_display"])):
            item = stats[side]
            lines.append(
                f"{tex_escape(pair['label']) if side == 'a' else ''} & {tex_escape(label)} & "
                f"{item['pareto']}/{stats['n']} & {item['degraded']}/{stats['n']} & "
                f"{item['health']:+.2f} & {item['lines']:.1f} & {item['files']:.1f} & "
                f"{item['visual_reg']}/{stats['n']} & {item['large']}/{stats['n']} & "
                f"{len(pair['eligible_sites'])} \\\\"
            )
        lines.append(r"\midrule")
    lines[-1] = r"\bottomrule"
    lines.extend([
        r"\end{tabular}",
        r"\end{adjustbox}",
        r"\caption{Common-site qualitative sample summary. The same ten sites are used for every comparison, sampled from sites where all compared models have a valid CWV row and a nonempty patch. Large patches touch at least 25 files or add at least 500 lines.}",
        r"\label{tab:common_site_summary}",
        r"\end{table*}",
        "",
    ])
    return lines


def evidence_table(pair: dict[str, Any]) -> list[str]:
    safe_label = re.sub(r"[^a-z0-9]+", "_", pair["label"].lower()).strip("_")
    a = pair["model_a_display"]
    b = pair["model_b_display"]
    lines = [
        r"\begin{table*}[t]",
        r"\centering",
        r"\scriptsize",
        r"\setlength{\tabcolsep}{3pt}",
        r"\begin{adjustbox}{max width=\linewidth}",
        r"\begin{tabular}{lllllll}",
        r"\toprule",
        rf"Site & {tex_escape(a)} outcome & {tex_escape(a)} patch & {tex_escape(a)} tactic & {tex_escape(b)} outcome & {tex_escape(b)} patch & {tex_escape(b)} tactic \\",
        r"\midrule",
    ]
    for record in pair["records"]:
        left = record["a"]
        right = record["b"]
        lines.append(
            f"{tex_escape(record['site_id'])} & "
            f"{outcome_label(left)}; {left['health_delta']:+.2f}; {visual_label(left)} & "
            f"{patch_shape(left)} & {tex_escape(tactic_label(left))} & "
            f"{outcome_label(right)}; {right['health_delta']:+.2f}; {visual_label(right)} & "
            f"{patch_shape(right)} & {tex_escape(tactic_label(right))} \\\\"
        )
    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{adjustbox}",
        rf"\caption{{Sampled site-level evidence for {tex_escape(pair['label'])}. Outcome cells report CWV class, health-score change, and visual label; patch cells report files touched and added lines.}}",
        rf"\label{{tab:common_site_{safe_label}}}",
        r"\end{table*}",
        "",
    ])
    return lines


def dominant_tactics(items: list[dict[str, Any]], limit: int = 2) -> str:
    counts: dict[str, int] = {}
    for item in items:
        for tag in item["strategy_tags"]:
            if tag in {"single-file patch", "multi-file patch"}:
                continue
            counts[tag] = counts.get(tag, 0) + 1
    if not counts:
        return "localized edits"
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return " and ".join(f"{tag} ({count}/{len(items)})" for tag, count in ranked)


def pair_paragraph(pair: dict[str, Any]) -> str:
    stats = pair_stats(pair)
    a = pair["model_a_display"]
    b = pair["model_b_display"]
    a_stats = stats["a"]
    b_stats = stats["b"]
    a_items = [record["a"] for record in pair["records"]]
    b_items = [record["b"] for record in pair["records"]]

    if a_stats["pareto"] > b_stats["pareto"]:
        pareto_clause = f"{a} converts more sampled patches to Pareto outcomes"
    elif b_stats["pareto"] > a_stats["pareto"]:
        pareto_clause = f"{b} converts more sampled patches to Pareto outcomes"
    else:
        pareto_clause = "the two models have the same sampled Pareto count"

    if a_stats["degraded"] < b_stats["degraded"]:
        risk_clause = f"{a} also has fewer degraded patches"
    elif b_stats["degraded"] < a_stats["degraded"]:
        risk_clause = f"{b} also has fewer degraded patches"
    else:
        risk_clause = "the degraded-patch count is tied"

    if a_stats["lines"] <= b_stats["lines"] * 0.5:
        size_clause = f"{a}'s edits are much smaller on average"
    elif b_stats["lines"] <= a_stats["lines"] * 0.5:
        size_clause = f"{b}'s edits are much smaller on average"
    else:
        size_clause = "The two models use similarly sized edits on average"

    return (
        f"In the {pair['label']} sample, {pareto_clause} "
        f"({a_stats['pareto']}/{stats['n']} vs. {b_stats['pareto']}/{stats['n']}), and {risk_clause} "
        f"({a_stats['degraded']}/{stats['n']} vs. {b_stats['degraded']}/{stats['n']}). "
        f"{size_clause}: {a} averages {a_stats['lines']:.1f} added lines across {a_stats['files']:.1f} files, "
        f"while {b} averages {b_stats['lines']:.1f} added lines across {b_stats['files']:.1f} files. "
        f"The dominant detected tactics are {dominant_tactics(a_items)} for {a} and "
        f"{dominant_tactics(b_items)} for {b}; the site-level evidence in the table shows where these "
        f"tactics coincide with visual regressions, neutral outcomes, or large rewrites."
    )


def build_outputs(
    max_sites_per_pair: int = MAX_SITES_PER_PAIR,
    require_plan: bool = False,
    progress: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    per_site = load_per_site()
    all_sites = sorted({site for _, site in per_site})
    rng = random.Random(SAMPLE_SEED)
    pair_outputs: list[dict[str, Any]] = []
    artifact_cache: dict[tuple[str, str], dict[str, Any]] = {}
    common_sites = [
        site for site in all_sites
        if all(eligible(model, site, per_site, require_plan=require_plan) for model in COMPARISON_MODELS)
    ]
    if len(common_sites) > max_sites_per_pair:
        common_sample = sorted(rng.sample(common_sites, max_sites_per_pair), key=lambda s: int(s) if s.isdigit() else s)
    else:
        common_sample = common_sites

    for model_a, model_b, label in PAIRS:
        eligible_sites = [
            site for site in all_sites
            if eligible(model_a, site, per_site, require_plan=require_plan)
            and eligible(model_b, site, per_site, require_plan=require_plan)
        ]
        sampled_sites = common_sample
        if progress:
            print(
                f"{label}: pair_eligible={len(eligible_sites)} common_eligible={len(common_sites)} sampled="
                f"{','.join(sampled_sites) if sampled_sites else 'none'}",
                flush=True,
            )
        records = []
        for site in sampled_sites:
            if progress:
                for model in (model_a, model_b):
                    directory = task_dir(model, site)
                    patch = patch_path(directory) if directory is not None else None
                    size = patch.stat().st_size if patch is not None else 0
                    print(
                        f"  site={site} model={MODEL_DISPLAY.get(model, model)} "
                        f"patch_bytes={size}",
                        flush=True,
                    )
            for model in (model_a, model_b):
                key = (model, site)
                if key not in artifact_cache:
                    artifact_cache[key] = artifact(model, site, per_site)
            records.append({
                "site_id": site,
                "a": artifact_cache[(model_a, site)],
                "b": artifact_cache[(model_b, site)],
            })
        pair_outputs.append({
            "label": label,
            "model_a": model_a,
            "model_a_display": MODEL_DISPLAY.get(model_a, model_a),
            "model_b": model_b,
            "model_b_display": MODEL_DISPLAY.get(model_b, model_b),
            "eligible_sites": eligible_sites,
            "common_eligible_sites": common_sites,
            "sampled_sites": sampled_sites,
            "records": records,
        })

    total_artifacts = sum(len(pair["records"]) * 2 for pair in pair_outputs)
    plan_artifacts = sum(
        int(record[side]["plan_available"])
        for pair in pair_outputs
        for record in pair["records"]
        for side in ("a", "b")
    )

    lines = [
        r"\section{Qualitative Common-Site Patch Analysis}",
        r"\label{sec:qual-common-site}",
        "",
        "We compare selected model pairs on the same ten sites, sampled from the intersection where every compared model has a nonempty patch and CWV measurements. Sampling is deterministic with seed "
        f"{SAMPLE_SEED}; the common eligible pool contains {len(common_sites)} sites. "
        "The analysis uses only saved artifacts: diffs, CWV outcomes, and saved plans or visual labels where available. "
        "The aim is not to relabel every patch manually, but to make the aggregate ranking legible under a fixed site set.",
        f"The shared site sample is {', '.join(common_sample) if common_sample else 'empty'}.",
        f"Saved plans are present for {plan_artifacts}/{total_artifacts} sampled model-site artifacts; for the remaining artifacts, strategy descriptions are inferred from the patch itself.",
        "",
    ]
    if require_plan:
        lines.append("This run additionally requires both models to have a saved plan for the sampled site.")
        lines.append("")
    lines.extend(pair_summary_table(pair_outputs))
    for pair in pair_outputs:
        lines.append(rf"\paragraph{{{pair['label']}.}}")
        lines.append(
            f"The pair has {len(pair['eligible_sites'])} pairwise eligible sites, and is evaluated on the shared ten-site sample."
        )
        lines.append(pair_paragraph(pair))
        lines.extend(evidence_table(pair))
        lines.append("")

    lines.append(r"\paragraph{Summary.}")
    lines.append(
        "The qualitative comparison supports the aggregate pattern: safer patches tend to be narrower, "
        "more explicit about the page's bottleneck, and less likely to combine broad script, stylesheet, "
        "and image rewrites in one change. Conservative models are not always the smallest editors; rather, "
        "they tend to avoid changing layout-critical resources unless the patch or plan identifies the specific LCP "
        "or loading gate being targeted. The riskiest patches often add plausible performance idioms while also "
        "touching animation, loading, or layout code, creating CWV regressions even when some LCP-related work is present."
    )
    return pair_outputs, "\n".join(lines) + "\n"


def write_csv_rows(pair_outputs: list[dict[str, Any]]) -> None:
    rows: list[dict[str, Any]] = []
    for pair in pair_outputs:
        for record in pair["records"]:
            for side in ("a", "b"):
                item = record[side]
                rows.append({
                    "pair": pair["label"],
                    "site_id": record["site_id"],
                    "model": item["model"],
                    "model_display": item["model_display"],
                    "is_pareto": item["is_pareto"],
                    "is_degraded": item["is_degraded"],
                    "health_delta": item["health_delta"],
                    "net_threshold_crossing": item["net_threshold_crossing"],
                    "visual_regression": item["visual_regression"],
                    "patch_bytes": item["patch_bytes"],
                    "files_touched": item["files_touched"],
                    "added_lines": item["added_lines"],
                    "removed_lines": item["removed_lines"],
                    "patterns": "; ".join(item["patterns"]),
                    "strategy_tags": "; ".join(item["strategy_tags"]),
                    "plan_theme": item["plan_theme"],
                    "plan_available": item["plan_available"],
                    "plan_path": item["plan_path"],
                    "patch_path": item["patch_path"],
                })
    for path in (
        OUT_DIR / "common_site_qualitative_rows.csv",
        OUT_DIR / "head_to_head_qualitative_rows.csv",
    ):
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-sites-per-pair", type=int, default=MAX_SITES_PER_PAIR)
    parser.add_argument("--require-plan", action="store_true")
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pair_outputs, text = build_outputs(args.max_sites_per_pair, args.require_plan, args.progress)
    payload = json.dumps(pair_outputs, indent=2)
    for name in ("common_site_qualitative", "head_to_head_qualitative"):
        (OUT_DIR / f"{name}.json").write_text(payload, encoding="utf-8")
        (OUT_DIR / f"{name}.tex").write_text(text, encoding="utf-8")
        (ROOT / "scripts" / "paper_results" / "patch_analysis" / f"{name}.tex").write_text(text, encoding="utf-8")
    write_csv_rows(pair_outputs)
    print(text)
    print(f"Wrote outputs to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
