"""
Regression Tool v2 — evaluates a single code patch for visual regression.

Checks:
  (i)  structural      – DOM visual-tree matching (from regression_tool_v2 module):
                         IoU-based leaf/section matching + missing/extra element detection
  (ii) gpt_visual      – GPT-4.1 screenshot comparison
  (iii) console_errors – regression if new JS errors appear after the patch

regression_tool_v2 (formerly webpage_comparison-master) provides the
structural matching engine: build_visual_tree → get_section_nodes →
run_matching(heuristic) → avg_leaf_iou / avg_section_iou / missing/extra counts.

Usage (programmatic):
    from regression_tool_v2.eval import evaluate_patch
    result = evaluate_patch(patch_file, template_info, output_dir)
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent   # scripts/regression_eval/regression_tool_v2/
_EVAL_DIR = _THIS_DIR.parent                  # scripts/regression_eval/

if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from common import (  # noqa: E402
    clone_repo,
    apply_patch,
    find_free_port,
    load_cwv_metadata,
    snapshot_site,
)

# ---------------------------------------------------------------------------
# Structural regression thresholds (mirrors eval_visual_regression_v2.py)
# ---------------------------------------------------------------------------

IOU_THRESHOLD     = 0.95   # avg leaf/section IoU below this → regression
JACCARD_THRESHOLD = 0.97   # text token similarity below this → regression


# ===========================================================================
# (i) Structural tree matching (regression_tool_v2 engine)
# ===========================================================================

def _structural_check(
    baseline_html_path: Path,
    patched_html_path: Path,
    baseline_img_path: Path,
    patched_img_path: Path,
    work_dir: Path,
) -> dict[str, Any]:
    """
    Run the heuristic tree-matching pipeline from regression_tool_v2 on the
    saved HTML files and return a regression verdict.

    Steps:
      1. prepare_html_analysis → capture element bboxes/screenshots per HTML
      2. build_visual_tree → hierarchical node dicts
      3. get_section_nodes → section-level grouping
      4. run_matching("heuristic") → leaf/section IoU scores
      5. Flag regression if avg IoU < threshold or missing/extra elements exist
    """
    try:
        import create_comparison_report as wcmp  # from regression_tool_v2/
        from dom_utils import build_visual_tree, get_section_nodes
    except ImportError as exc:
        logger.error("regression_tool_v2 not importable: %s", exc)
        return {"regression": None,
                "error": f"regression_tool_v2 import failed: {exc}"}

    orig_analysis_dir = work_dir / "orig_analysis"
    gen_analysis_dir  = work_dir / "gen_analysis"
    comparison_dir    = work_dir / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)

    # 1. Prepare element analysis (bbox capture via headless browser)
    try:
        if not orig_analysis_dir.exists() or \
                len(list(orig_analysis_dir.iterdir())) <= 3:
            wcmp.prepare_html_analysis(
                str(baseline_html_path), str(orig_analysis_dir), label="original"
            )
        if not gen_analysis_dir.exists() or \
                len(list(gen_analysis_dir.iterdir())) <= 3:
            wcmp.prepare_html_analysis(
                str(patched_html_path), str(gen_analysis_dir), label="generated"
            )
    except Exception as exc:
        logger.error("prepare_html_analysis failed: %s", exc)
        return {"regression": None, "error": f"analysis failed: {exc}"}

    # 2. Read HTML content
    orig_html = baseline_html_path.read_text(encoding="utf-8")
    gen_html  = patched_html_path.read_text(encoding="utf-8")

    # 3. Build visual trees
    try:
        orig_nodes, _ = build_visual_tree(str(orig_analysis_dir), orig_html)
        gen_nodes,  _ = build_visual_tree(str(gen_analysis_dir),  gen_html)
    except Exception as exc:
        logger.error("build_visual_tree failed: %s", exc)
        return {"regression": None, "error": f"visual tree failed: {exc}"}

    # 4. Get section nodes
    try:
        orig_b64, orig_dims = wcmp.take_full_page_screenshot_b64(
            str(baseline_html_path)
        )
        gen_b64,  gen_dims  = wcmp.take_full_page_screenshot_b64(
            str(patched_html_path)
        )
    except Exception as exc:
        logger.error("take_full_page_screenshot_b64 failed: %s", exc)
        return {"regression": None, "error": f"screenshot failed: {exc}"}

    # Save screenshots for use in the report
    _write_b64_png(orig_b64, baseline_img_path)
    _write_b64_png(gen_b64,  patched_img_path)

    try:
        orig_sections, _ = get_section_nodes(
            orig_nodes,
            orig_dims.get("width", 1280),
            orig_dims.get("height", 3000),
            thresh_height=2900,
            max_leaves=None,
        )
        gen_sections, _ = get_section_nodes(
            gen_nodes,
            gen_dims.get("width", 1280),
            gen_dims.get("height", 3000),
            thresh_height=2900,
            max_leaves=None,
        )
    except Exception as exc:
        logger.error("get_section_nodes failed: %s", exc)
        return {"regression": None, "error": f"section nodes failed: {exc}"}

    # 5. Run heuristic matching
    try:
        leaf_diffs, section_diffs, avg_leaf, avg_sec, extra_stats = wcmp.run_matching(
            "heuristic",
            orig_nodes, gen_nodes,
            orig_sections, gen_sections,
            str(orig_analysis_dir), str(gen_analysis_dir),
            str(comparison_dir),
            str(baseline_img_path), str(patched_img_path),
        )
    except Exception as exc:
        logger.error("run_matching failed: %s", exc)
        return {"regression": None, "error": f"matching failed: {exc}"}

    missing   = len(extra_stats.get("missing_leaves",         []))
    extra     = len(extra_stats.get("extra_leaves",           []))
    unmatched_orig = len(extra_stats.get("unmatched_orig_sections", []))
    unmatched_gen  = len(extra_stats.get("unmatched_gen_sections",  []))

    has_regression = (
        avg_leaf < IOU_THRESHOLD
        or avg_sec  < IOU_THRESHOLD
        or missing > 0
        or extra   > 0
        or unmatched_orig > 0
        or unmatched_gen  > 0
    )

    return {
        "regression":              has_regression,
        "avg_leaf_iou":            avg_leaf,
        "avg_section_iou":         avg_sec,
        "matched_leaf_pairs":      len(leaf_diffs),
        "matched_section_pairs":   len(section_diffs),
        "missing_leaves":          missing,
        "extra_leaves":            extra,
        "unmatched_orig_sections": unmatched_orig,
        "unmatched_gen_sections":  unmatched_gen,
        "iou_threshold":           IOU_THRESHOLD,
        "error":                   None,
    }


def _write_b64_png(b64_data: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(b64_data))


# ===========================================================================
# (ii) Jaccard text similarity
# ===========================================================================

def _extract_text_tokens(html: str) -> set[str]:
    import re
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)
    return set(re.findall(r"\w+", text.lower()))


def _jaccard_check(baseline_html: str, patched_html: str) -> dict[str, Any]:
    t1 = _extract_text_tokens(baseline_html)
    t2 = _extract_text_tokens(patched_html)
    # If either side is empty the HTML capture failed — treat as inconclusive
    # rather than flagging a false regression (sim=0.0 would always fire).
    if not t1 or not t2:
        return {"regression": None, "similarity": None, "threshold": JACCARD_THRESHOLD,
                "error": "empty html"}
    sim = len(t1 & t2) / len(t1 | t2)
    return {
        "regression": sim < JACCARD_THRESHOLD,
        "similarity": round(sim, 4),
        "threshold":  JACCARD_THRESHOLD,
    }


# ===========================================================================
# (iii) GPT-4.1 screenshot comparison
# ===========================================================================

def _gpt_screenshot_compare(
    baseline_img: Path,
    patched_img: Path,
) -> dict[str, Any]:
    """
    Ask GPT-4.1 whether the patched screenshot introduces a visual regression.
    Returns: { "regression": bool, "raw_response": str, "error": str|None }
    """
    api_key  = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    version  = os.getenv("OPENAI_API_VERSION", "2024-02-15-preview")

    if not api_key or not endpoint:
        return {"regression": None, "raw_response": None,
                "error": "Azure OpenAI credentials not set"}

    try:
        from openai import AzureOpenAI
    except ImportError:
        return {"regression": None, "raw_response": None,
                "error": "openai package not installed"}

    def _b64(path: Path) -> str:
        return base64.b64encode(path.read_bytes()).decode()

    prompt = (
        "You are a visual QA engineer. "
        "The FIRST image is the BASELINE website. "
        "The SECOND image is the same site after a code patch. "
        "Determine whether the patch introduced a VISUAL REGRESSION.\n\n"
        "Count as regression: missing content, broken layout, invisible elements, "
        "severe colour/font breakage, missing navigation.\n"
        "Do NOT count as regression: minor spacing tweaks, performance optimisations "
        "that don't change appearance, intentional design improvements.\n\n"
        "Reply with exactly one word: TRUE (regression) or FALSE (no regression)."
    )

    try:
        client = AzureOpenAI(
            api_key=api_key,
            api_version=version,
            azure_endpoint=endpoint,
        )
        response = client.chat.completions.create(
            model=os.getenv("AZURE_DEPLOYMENT", "gpt-4.1"),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{_b64(baseline_img)}",
                            "detail": "low",
                        }},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{_b64(patched_img)}",
                            "detail": "low",
                        }},
                    ],
                }
            ],
            max_tokens=5,
            temperature=0,
        )
        raw = response.choices[0].message.content.strip().upper()
        return {"regression": raw.startswith("TRUE"), "raw_response": raw, "error": None}
    except Exception as exc:
        logger.error("GPT comparison failed: %s", exc)
        return {"regression": None, "raw_response": None, "error": str(exc)}


# ===========================================================================
# (iii) Console error check
# ===========================================================================

def _console_error_check(
    baseline_errors: list[str],
    patched_errors: list[str],
) -> dict[str, Any]:
    baseline_set = set(baseline_errors)
    new_errors   = [e for e in patched_errors if e not in baseline_set]
    return {
        "regression":     len(new_errors) > 0,
        "new_errors":     new_errors,
        "baseline_count": len(baseline_errors),
        "patched_count":  len(patched_errors),
    }


# ===========================================================================
# Public API
# ===========================================================================

def evaluate_patch(
    patch_file: Path,
    template_info: dict,
    output_dir: Path,
) -> dict[str, Any]:
    """
    Evaluate *patch_file* with regression tool v2.

    Returns:
    {
        "patch": str,
        "template_id": int,
        "agent": str,
        "checks": {
            "structural":     { "regression": bool, "avg_leaf_iou": float, ... },
            "jaccard_text":   { "regression": bool, "similarity": float, ... },
            "gpt_visual":     { "regression": bool, "raw_response": str, ... },
            "console_errors": { "regression": bool, "new_errors": [...], ... },
        },
        "overall_regression": bool,
        "metadata": { "desktop": {...}, "mobile": {...} },
        "error": str|None,
    }
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    baseline_img       = output_dir / "baseline.png"
    patched_img        = output_dir / "patched.png"
    baseline_html_path = output_dir / "baseline.html"
    patched_html_path  = output_dir / "patched.html"
    structural_dir     = output_dir / "structural"

    repo_id   = template_info["repo_id"]
    commit_id = template_info["commit_id"]
    framework = template_info["framework"]

    metadata = load_cwv_metadata(patch_file)

    with tempfile.TemporaryDirectory(prefix="vr_v2_") as tmp:
        repo_dir = Path(tmp) / "repo"

        # 1. Clone baseline
        if not clone_repo(repo_id, commit_id, repo_dir):
            return _error_result(patch_file, template_info, metadata,
                                 "git clone failed")

        port = find_free_port()

        # 2. Snapshot baseline
        logger.info("[v2] Snapshotting baseline for %s ...", patch_file.name)
        snap_base = snapshot_site(repo_dir, framework, port,
                                  baseline_img, baseline_html_path)
        if not snap_base["ok"]:
            return _error_result(patch_file, template_info, metadata,
                                 "baseline snapshot failed")

        # 3. Apply patch
        apply_patch(repo_dir, patch_file)

        # 4. Snapshot patched
        logger.info("[v2] Snapshotting patched for %s ...", patch_file.name)
        snap_pat = snapshot_site(repo_dir, framework, port,
                                 patched_img, patched_html_path)
        if not snap_pat["ok"]:
            return _error_result(patch_file, template_info, metadata,
                                 "patched snapshot failed")

    # 5. Run all checks
    baseline_html = baseline_html_path.read_text(encoding="utf-8") \
        if baseline_html_path.exists() else ""
    patched_html  = patched_html_path.read_text(encoding="utf-8") \
        if patched_html_path.exists() else ""

    structural_result = _structural_check(
        baseline_html_path, patched_html_path,
        baseline_img, patched_img,
        structural_dir,
    )

    jaccard_result = _jaccard_check(baseline_html, patched_html)

    gpt_result = _gpt_screenshot_compare(baseline_img, patched_img) \
        if baseline_img.exists() and patched_img.exists() \
        else {"regression": False, "error": "screenshots missing"}

    console_result = _console_error_check(
        snap_base["console_errors"], snap_pat["console_errors"]
    )

    checks = {
        "structural":     structural_result,
        "jaccard_text":   jaccard_result,
        "gpt_visual":     gpt_result,
        "console_errors": console_result,
    }

    # overall: True if any check flagged regression (skip None/error checks)
    overall = any(
        v.get("regression") is True
        for v in checks.values()
    )

    return {
        "patch":               str(patch_file),
        "template_id":         _template_id(patch_file),
        "agent":               _agent_name(patch_file),
        "checks":              checks,
        "overall_regression":  overall,
        "metadata":            metadata,
        "error":               None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _template_id(patch_file: Path) -> int:
    try:
        return int(patch_file.stem.split("_")[0])
    except (IndexError, ValueError):
        return -1


def _agent_name(patch_file: Path) -> str:
    parts = patch_file.stem.split("_")
    return parts[-1] if len(parts) >= 3 else patch_file.parent.name


def _error_result(
    patch_file: Path,
    template_info: dict,
    metadata: dict,
    error: str,
) -> dict[str, Any]:
    return {
        "patch":               str(patch_file),
        "template_id":         _template_id(patch_file),
        "agent":               _agent_name(patch_file),
        "checks":              {},
        "overall_regression":  None,
        "metadata":            metadata,
        "error":               error,
    }
