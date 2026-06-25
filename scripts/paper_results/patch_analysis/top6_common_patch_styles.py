#!/usr/bin/env python3
"""Top-6 common-site qualitative patch style analysis.

The script fixes a shared sample of templates across the top six model-agent
pairs by Overall Score. It reads actual patch files and CWV rows, then emits
paper-facing tables plus JSON/CSV artifacts.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path("/dev/shm/ayush/web-experience-benchmark")
FINAL_DUMPS = ROOT / "final_result_dumps" / "main_bench_rerun_20260615"
PER_SITE_CSV = ROOT / "paper_writing" / "data" / "final_per_site.csv"
SUMMARY_JSON = ROOT / "paper_writing" / "data" / "final_summary.json"
OUT_DIR = ROOT / "paper_writing" / "data" / "patch_analysis"
SCRIPT_OUT_DIR = ROOT / "scripts" / "paper_results" / "patch_analysis"
PATCH_ANALYSIS = ROOT / "scripts" / "paper_results" / "analyze_patch_patterns.py"

SAMPLE_SEED = 20260618
TOP_K = 6
SAMPLE_N = 20
MAX_PATTERN_CANDIDATE_LINES = 4096
MAX_PATTERN_LINE_CHARS = 2000

MODEL_DISPLAY = {
    "closed_cc-opus-4.6": "Opus 4.6",
    "closed_cc-sonnet-4.6": "Sonnet 4.6",
    "closed_oc_gemini-2-5-pro": "Gemini Pro",
    "open_gemma-4-31b-it": "Gemma 31B",
    "open_glm-4.7-flash": "GLM Flash",
    "open_minimax-m2.7": "MiniMax M2.7",
}

PATTERN_LABELS = {
    "async_css_print_onload": "async CSS",
    "preload_style": "CSS preload",
    "script_defer_or_async": "script defer/async",
    "preconnect": "preconnect",
    "dns_prefetch": "DNS prefetch",
    "prefetch": "prefetch",
    "lazy_loading_attr": "lazy media",
    "image_width_height": "image dimensions",
    "fetchpriority_attr": "fetchpriority",
    "font_display_css": "font-display",
    "content_visibility_css": "content-visibility",
    "webp_source": "WebP",
}

METRICS = [
    "lcp_mobile",
    "lcp_desktop",
    "inp_mobile",
    "inp_desktop",
    "cls_mobile",
    "cls_desktop",
]

METRIC_LABEL = {
    "lcp_mobile": "LCP-m",
    "lcp_desktop": "LCP-d",
    "inp_mobile": "INP-m",
    "inp_desktop": "INP-d",
    "cls_mobile": "CLS-m",
    "cls_desktop": "CLS-d",
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


def load_per_site() -> dict[tuple[str, str], dict[str, str]]:
    with PER_SITE_CSV.open(newline="", encoding="utf-8") as handle:
        return {(row["model"], row["site_id"]): row for row in csv.DictReader(handle)}


def top_models() -> list[str]:
    data = json.loads(SUMMARY_JSON.read_text())["per_model"]
    ranked = [
        (float(values["overall_score"]), model)
        for model, values in data.items()
        if values.get("overall_score") is not None and model in MODEL_DISPLAY
    ]
    return [model for _, model in sorted(ranked, reverse=True)[:TOP_K]]


def task_dir(model: str, site_id: str) -> Path | None:
    matches = sorted((FINAL_DUMPS / model).glob(f"{site_id}_*"))
    return matches[0] if matches else None


def patch_path(directory: Path | None) -> Path | None:
    if directory is None:
        return None
    expected = directory / f"{directory.name}.patch"
    if expected.exists():
        return expected
    matches = sorted(directory.glob("*.patch"))
    return matches[0] if matches else None


def eligible(model: str, site_id: str, rows: dict[tuple[str, str], dict[str, str]]) -> bool:
    patch = patch_path(task_dir(model, site_id))
    return (model, site_id) in rows and patch is not None and patch.stat().st_size > 0


def patch_profile(path: Path) -> dict[str, Any]:
    keywords = (
        "preconnect", "dns-prefetch", "prefetch", "preload", "stylesheet",
        "loading=", "fetchpriority", "font-display", "content-visibility",
        "defer", "async", "width=", "height=", "webp", "media=\"print",
        "media='print", "onload", "settimeout", "display",
    )
    files: list[dict[str, Any]] = []
    snippets: list[str] = []
    pattern_hits = {key: 0 for key in PATCH.PATTERN_KEYS}
    pattern_batch: list[str] = []
    candidate_lines_seen = 0
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
                continue
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
                added_line = line[1:]
                lower = added_line.lower()
                if any(key in lower for key in keywords):
                    compact = re.sub(r"\s+", " ", added_line[:MAX_PATTERN_LINE_CHARS].strip())
                    if len(snippets) < 3:
                        snippets.append(compact[:180])
                    if candidate_lines_seen < MAX_PATTERN_CANDIDATE_LINES and not all(pattern_hits.values()):
                        pattern_batch.append(compact)
                        candidate_lines_seen += 1
                        if len(pattern_batch) >= 512:
                            flush_patterns()
                continue
            if line.startswith("-") and not line.startswith("---"):
                removed += 1
    if current is not None:
        files.append({"file": current, "added": added, "removed": removed})
    flush_patterns()
    return {
        "files": files,
        "patterns": [PATTERN_LABELS[key] for key, value in pattern_hits.items() if value],
        "snippets": snippets,
    }


def file_bucket(path: str) -> str:
    lower = path.lower()
    if lower.endswith((".css", ".scss", ".sass", ".less")):
        return "style"
    if lower.endswith((".js", ".ts", ".jsx", ".tsx")):
        return "script"
    if lower.endswith((".html", ".htm", ".php", ".astro", ".vue", ".svelte", ".liquid")):
        return "markup"
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return "asset"
    if lower.endswith((".map", ".json", ".lock")):
        return "generated"
    return "other"


def metric_summary(row: dict[str, str]) -> dict[str, Any]:
    deltas = {metric: float(row[f"{metric}_h_delta"]) for metric in METRICS}
    best = max(deltas.items(), key=lambda kv: kv[1])
    worst = min(deltas.items(), key=lambda kv: kv[1])
    negatives = [(metric, value) for metric, value in deltas.items() if value < 0]
    positives = [(metric, value) for metric, value in deltas.items() if value > 0]
    return {
        "best_metric": best[0],
        "best_delta": best[1],
        "worst_metric": worst[0],
        "worst_delta": worst[1],
        "negative_metrics": negatives,
        "positive_metrics": positives,
    }


def artifact(model: str, site_id: str, rows: dict[tuple[str, str], dict[str, str]]) -> dict[str, Any]:
    directory = task_dir(model, site_id)
    patch = patch_path(directory)
    if patch is None:
        raise RuntimeError(f"Missing patch for {model} {site_id}")
    profile = patch_profile(patch)
    files = profile["files"]
    buckets = Counter(file_bucket(item["file"]) for item in files)
    row = rows[(model, site_id)]
    metrics = metric_summary(row)
    visual = PATCH.compute_regression(directory / "visual.json") if directory else None
    return {
        "model": model,
        "model_display": MODEL_DISPLAY[model],
        "site_id": site_id,
        "patch_path": str(patch.relative_to(ROOT)),
        "patch_bytes": patch.stat().st_size,
        "files_touched": len(files),
        "added_lines": sum(item["added"] for item in files),
        "removed_lines": sum(item["removed"] for item in files),
        "file_buckets": dict(buckets),
        "top_files": sorted(files, key=lambda item: item["added"] + item["removed"], reverse=True)[:3],
        "patterns": profile["patterns"],
        "snippets": profile["snippets"],
        "is_pareto": int(row["is_pareto"]),
        "is_degraded": int(row["is_degraded"]),
        "health_delta": float(row["health_delta"]),
        "net_threshold_crossing": float(row["net_threshold_crossing"]),
        "visual_regression": visual,
        **metrics,
    }


def outcome(item: dict[str, Any]) -> str:
    if item["is_pareto"]:
        return "P"
    if item["is_degraded"]:
        return "D"
    return "N"


def visual(item: dict[str, Any]) -> str:
    if item["visual_regression"] is True:
        return "VR"
    if item["visual_regression"] is False:
        return "clean"
    return "n/a"


def outcome_reading(item: dict[str, Any]) -> str:
    cwv = "Pareto" if item["is_pareto"] else ("Degraded" if item["is_degraded"] else "Neutral")
    visual_part = (
        "visual reg."
        if item["visual_regression"] is True
        else ("visual clean" if item["visual_regression"] is False else "visual n/a")
    )
    health = "site health up" if item["health_delta"] > 0 else ("site health down" if item["health_delta"] < 0 else "site health flat")
    net = (
        "net tier up"
        if item["net_threshold_crossing"] > 0
        else ("net tier down" if item["net_threshold_crossing"] < 0 else "net tier flat")
    )
    best = f"best: {METRIC_LABEL[item['best_metric']]}"
    worst = f"worst: {METRIC_LABEL[item['worst_metric']]}" if item["worst_delta"] < 0 else "no metric loss"
    return f"{cwv}; {visual_part}; {health}; {net}; {best}; {worst}"


def outcome_tex(item: dict[str, Any]) -> str:
    cwv = r"\textbf{Pareto}" if item["is_pareto"] else (r"\textbf{Degraded}" if item["is_degraded"] else "Neutral")
    visual_part = (
        r"\textbf{visual reg.}"
        if item["visual_regression"] is True
        else ("visual clean" if item["visual_regression"] is False else "visual n/a")
    )
    health = "site health up" if item["health_delta"] > 0 else ("site health down" if item["health_delta"] < 0 else "site health flat")
    net = (
        "net tier up"
        if item["net_threshold_crossing"] > 0
        else ("net tier down" if item["net_threshold_crossing"] < 0 else "net tier flat")
    )
    best = f"best: {METRIC_LABEL[item['best_metric']]}"
    worst = f"worst: {METRIC_LABEL[item['worst_metric']]}" if item["worst_delta"] < 0 else "no metric loss"
    return f"{cwv}; {visual_part}; {health}; {net}; {best}; {worst}"


def pattern_short(item: dict[str, Any]) -> str:
    if not item["patterns"]:
        return "localized"
    return ", ".join(item["patterns"][:3]) + (", ..." if len(item["patterns"]) > 3 else "")


def tradeoff_short(item: dict[str, Any]) -> str:
    best = f"{METRIC_LABEL[item['best_metric']]} {item['best_delta']:+.1f}"
    worst = f"{METRIC_LABEL[item['worst_metric']]} {item['worst_delta']:+.1f}"
    return best if item["worst_delta"] >= 0 else f"{best}; {worst}"


def scope_label(item: dict[str, Any]) -> str:
    buckets = item["file_buckets"]
    if item["files_touched"] >= 100 or item["added_lines"] >= 1000:
        return "site-wide generated rewrite"
    if item["files_touched"] >= 10:
        return "broad generated-page sweep"
    if item["files_touched"] >= 4:
        return "multi-entry edit"
    if item["added_lines"] <= 5:
        return "tiny targeted edit"
    if buckets.get("script", 0):
        return "localized script/loading edit"
    if buckets.get("style", 0):
        return "localized style/layout edit"
    return "localized markup edit"


def risk_label(item: dict[str, Any]) -> str:
    patterns = set(item["patterns"])
    if {"async CSS", "CSS preload"} & patterns and {"script defer/async"} & patterns:
        return "risks late CSS and reordered script work"
    if {"async CSS", "CSS preload"} & patterns:
        return "risks late stylesheet application"
    if "script defer/async" in patterns:
        return "risks dependency or interaction timing"
    if {"image dimensions", "fetchpriority", "lazy media", "WebP"} & patterns:
        return "mostly image-priority/layout risk"
    if item["files_touched"] >= 25:
        return "high audit risk from broad rewrite"
    return "low visible blast radius"


def action_label(item: dict[str, Any]) -> str:
    if not item["patterns"]:
        return "no repeated CWV idiom detected"
    return ", ".join(item["patterns"][:3]) + (", ..." if len(item["patterns"]) > 3 else "")


def top_file_label(item: dict[str, Any]) -> str:
    names = [Path(file["file"]).name for file in item["top_files"][:2]]
    return ", ".join(names) if names else "patch file"


def model_patch_reading(item: dict[str, Any]) -> str:
    return (
        f"{scope_label(item)} in {top_file_label(item)}; "
        f"{action_label(item)}; {risk_label(item)}"
    )


def template_grid_tables(sample: list[str], models: list[str],
                         artifacts: dict[str, list[dict[str, Any]]],
                         per_table: int = 1) -> list[str]:
    lines: list[str] = []
    for chunk_index, site in enumerate(sample, start=1):
        idx = sample.index(site)
        lines.extend([
            r"\begin{table*}[p]",
            r"\centering",
            r"\small",
            r"\setlength{\tabcolsep}{5pt}",
            r"\renewcommand{\arraystretch}{1.15}",
            r"\begin{adjustbox}{max width=\linewidth}",
            r"\begin{tabular}{llp{0.62\linewidth}}",
            r"\toprule",
            rf"\multicolumn{{3}}{{l}}{{\textbf{{Template {tex_escape(site)}}}}} \\",
            r"\midrule",
            r"Model & Outcome & Patch reading \\",
            r"\midrule",
        ])
        for model in models:
            item = artifacts[model][idx]
            lines.append(
                f"{tex_escape(MODEL_DISPLAY[model])} & {outcome_tex(item)} & "
                f"{tex_escape(model_patch_reading(item))} \\\\"
            )
        lines.extend([
            r"\bottomrule",
            r"\end{tabular}",
            r"\end{adjustbox}",
            rf"\caption{{Qualitative patch reading for template {tex_escape(site)} across the top models. Entries are derived from actual patch diffs rather than CWV scores.}}",
            rf"\label{{tab:top6_common_template_reading_{chunk_index}}}",
            r"\end{table*}",
            "",
        ])
    return lines


STYLE_READINGS = {
    "open_gemma-4-31b-it": (
        "Small local HTML/CSS edits; image dimensions, preloads, script deferral.",
        "The bottleneck is a single visible image, stylesheet, or script path.",
        "The page needs coordinated layout or dependency changes; resource hints are too shallow.",
        "Often succeeds with direct image and script fixes; struggles when interaction timing is the real issue.",
    ),
    "open_glm-4.7-flash": (
        "Formulaic performance recipes across markup entry points.",
        "Generic preconnect/preload/defer rules match the actual bottleneck.",
        "Stylesheet order, script dependencies, or layout stability are sensitive.",
        "Tends to repeat the same loading recipe even when the page needs dependency-aware changes.",
    ),
    "closed_cc-opus-4.6": (
        "Architectural rewrites of loading, event timing, fonts, and generated pages.",
        "A site-wide generated template really needs broad resource restructuring.",
        "The rewrite changes generated content, first-paint ordering, or interaction timing.",
        "Can make effective broad rewrites, but the resulting patches are harder to audit for content and behavior.",
    ),
    "closed_cc-sonnet-4.6": (
        "Structural edits with more behavior changes: generated pages, widgets, CSS/JS timing.",
        "The page benefits from coordinated image, font, and script scheduling.",
        "Behavioral rewrites shift interaction work or late layout.",
        "Often reasons about page structure, but sometimes changes widgets or generated pages more than necessary.",
    ),
    "open_minimax-m2.7": (
        "Mixed style: tiny resource-hint patches plus medium page rewrites.",
        "A small image/script hint is enough and avoids structural CSS deferral.",
        "Global image/layout rules alter LCP candidates or CLS.",
        "Alternates between restrained hints and broad layout rules, so the qualitative risk is uneven.",
    ),
    "closed_oc_gemini-2-5-pro": (
        "Compact attribute-oriented edits: dimensions, fetchpriority, deferred CSS/scripts.",
        "The fix is local to above-fold images or one blocking script.",
        "Deferring layout-critical CSS creates late layout movement.",
        "Usually easy to audit, but CSS deferral can make visible structure arrive too late.",
    ),
}


def build_outputs() -> tuple[list[str], list[str], dict[str, Any]]:
    rows = load_per_site()
    models = top_models()
    all_sites = sorted({site for _, site in rows})
    common_sites = [site for site in all_sites if all(eligible(model, site, rows) for model in models)]
    sample = sorted(random.Random(SAMPLE_SEED).sample(common_sites, SAMPLE_N), key=lambda s: int(s) if s.isdigit() else s)
    artifacts = {
        model: [artifact(model, site, rows) for site in sample]
        for model in models
    }
    payload = {"models": models, "sample_sites": sample, "common_sites": common_sites, "artifacts": artifacts}
    lines = render_tex(models, sample, common_sites, artifacts)
    csv_rows = []
    for model in models:
        for item in artifacts[model]:
            csv_rows.append({
                "model": model,
                "model_display": item["model_display"],
                "site_id": item["site_id"],
                "outcome": outcome(item),
                "health_delta": item["health_delta"],
                "best_metric": item["best_metric"],
                "best_delta": item["best_delta"],
                "worst_metric": item["worst_metric"],
                "worst_delta": item["worst_delta"],
                "files_touched": item["files_touched"],
                "added_lines": item["added_lines"],
                "removed_lines": item["removed_lines"],
                "patterns": "; ".join(item["patterns"]),
                "top_files": "; ".join(f"{f['file']} (+{f['added']}/-{f['removed']})" for f in item["top_files"]),
                "patch_path": item["patch_path"],
            })
    return lines, csv_rows, payload


def model_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    bucket_counts = Counter()
    pattern_counts = Counter()
    for item in items:
        bucket_counts.update(item["file_buckets"])
        pattern_counts.update(item["patterns"])
    return {
        "pareto": sum(item["is_pareto"] for item in items),
        "degraded": sum(item["is_degraded"] for item in items),
        "visual_reg": sum(1 for item in items if item["visual_regression"] is True),
        "health": mean(item["health_delta"] for item in items),
        "files": mean(item["files_touched"] for item in items),
        "lines": mean(item["added_lines"] for item in items),
        "large": sum(1 for item in items if item["files_touched"] >= 25 or item["added_lines"] >= 500),
        "buckets": bucket_counts,
        "patterns": pattern_counts,
    }


def render_tex(models: list[str], sample: list[str], common_sites: list[str],
               artifacts: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = [
        r"\section{Common-Template Patch Style Analysis}",
        r"\label{sec:common-template-patch-style}",
        "",
        "We inspect the top model-agent pairs on the same sampled templates. The quantitative ranking and CWV outcomes are reported earlier; "
        "this section only reads the patches themselves. For each template, we compare the six diffs side by side and describe what each model "
        "actually changed: resource hints, stylesheet timing, script order, image handling, generated-page rewrites, or behavior-bearing code.",
        "",
        r"\paragraph{Model coding styles.}",
        "The models differ less in whether they know common CWV optimizations than in how they choose where to apply them. Gemma most often follows "
        "a critical-path strategy: it identifies the page-level resource that appears to block rendering and changes that edge with limited surrounding "
        "movement. Gemini Pro is more asset-priority oriented, frequently adjusting image attributes, fetch priority, lazy loading, and isolated "
        "stylesheet or script tags. GLM Flash behaves like a checklist optimizer, combining preconnects, stylesheet preloads, deferred scripts, and "
        "image hints across several entry points. MiniMax uses similar recipes but is more willing to add layout-level rules, which can help when the "
        "bottleneck is structural but can also move visual elements. Opus and Sonnet more often reason at the generated-site or application-structure "
        "level, changing template output, shared CSS, widgets, and script timing rather than only annotating existing tags.",
        "",
        r"\paragraph{Failure modes in the diffs.}",
        "The same high-level optimization can fail for different coding reasons. Async stylesheets can make layout-critical CSS arrive late. Deferred "
        "scripts can reorder dependencies or interaction handlers. Global image and layout rules can change which element becomes visually important. "
        "Generated-page rewrites can be effective when they preserve template behavior, but they become difficult to audit when they touch many pages "
        "or minified one-line documents. The per-template tables below record these differences directly from the patch content.",
        "",
        r"Tables~\ref{tab:top6_common_template_reading_1}--\ref{tab:top6_common_template_reading_20} should therefore be read as paired evidence: "
        "the outcome column identifies whether the patch improved, degraded, or visually regressed the page, while the patch-reading column explains "
        "the mechanism visible in the diff. Across the sample, the strongest patches tend to be narrow interventions on the actual bottleneck, such as "
        "one stylesheet edge, one image-priority decision, or one script-loading dependency. The weaker patches are usually not random failures; they "
        "often apply a plausible CWV recipe in the wrong place, or apply it too globally, so an LCP or loading improvement is offset by a CLS, visual, "
        "or interaction regression.",
        "",
        "The tables also show why model ranking cannot be explained by a single optimization pattern. In templates such as 2408 and 60, broad generated-page "
        "edits separate structural models from models that keep the change local. In templates such as 3316 and 2977, the decisive difference is not patch "
        "size but whether image priority, slider behavior, and script timing are preserved together. The common qualitative pattern is that conservative "
        "models are easier to audit but sometimes miss cross-file causes, recipe-driven models find many obvious loading fixes but risk late CSS or reordered "
        "scripts, and structural models can repair harder generated sites while carrying the largest regression surface.",
        "",
        "A useful way to summarize the template-wise tables is by optimization intent. Some patches attack render discovery by making CSS, fonts, and "
        "connections visible earlier; these succeed when they leave above-the-fold styling stable. Some patches attack hero-element selection by changing "
        "image dimensions, formats, or priority; these succeed only when they preserve the visual hierarchy that the page already relies on. Other patches "
        "attack main-thread scheduling by deferring scripts or moving handlers; these are beneficial when the scripts are independent, but risky when they "
        "initialize sliders, menus, or layout measurements. The best qualitative patches combine the intended CWV fix with a preservation rule: improve one "
        "resource path while keeping layout, dependency order, and the user-visible first viewport unchanged.",
        "",
        r"\paragraph{Gemma's CLS-preserving strategy.}",
        "Gemma's CLS advantage does not appear to come from a novel optimization primitive; the same primitives are available to every model. Its distinctive "
        "behavior is the way it composes them. In the template-wise readings, Gemma often stabilizes the layout box before changing the resource schedule: it "
        "adds explicit dimensions or minimum heights around unstable above-the-fold regions, gives important media a fixed footprint before prioritizing it, "
        "and defers non-critical scripts without broadly delaying layout-critical CSS. This differs from more recipe-driven patches that first add async CSS, "
        "preconnects, preloads, lazy loading, or fetch priority and only indirectly address whether the first viewport still has a stable geometry.",
        "",
        "This matters because CLS is usually caused by late geometry changes rather than slow loading alone. Images without dimensions, generated headers, "
        "sliders, menus, injected sidebars, and font or stylesheet timing can all move visible content after first paint. Gemma's better patches therefore "
        "look less aggressive than some competitors' patches, but they preserve the page's initial visual frame: reserve the hero or navigation space, keep "
        "critical styling stable, and move only the script or asset work that is unlikely to re-layout the first viewport. The qualitative lesson is that "
        "CLS-safe optimization is not just faster resource discovery; it is resource discovery constrained by layout preservation.",
        "",
    ]
    lines.extend(template_grid_tables(sample, models, artifacts))
    return lines


def write_outputs(lines: list[str], csv_rows: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPT_OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    for path in (
        OUT_DIR / "top6_common_patch_style_analysis.tex",
        OUT_DIR / "common_site_qualitative.tex",
        SCRIPT_OUT_DIR / "top6_common_patch_style_analysis.tex",
        SCRIPT_OUT_DIR / "common_site_qualitative.tex",
    ):
        path.write_text(text, encoding="utf-8")
    (OUT_DIR / "top6_common_patch_style_analysis.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with (OUT_DIR / "top6_common_patch_style_analysis_rows.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    lines, csv_rows, payload = build_outputs()
    write_outputs(lines, csv_rows, payload)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
