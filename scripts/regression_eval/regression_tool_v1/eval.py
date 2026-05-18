"""
Regression Tool v1 — evaluates a single code patch for visual regression.

Checks (all four must pass for no regression):
  (i)  gpt_visual       – GPT-4.1 screenshot comparison
  (ii) dom_lsh          – Locality-Sensitive Hash (SimHash) of canonicalized DOM;
                          regression if Hamming distance > DOM_LSH_THRESHOLD
  (iii) jaccard_text    – Jaccard similarity of extracted page text tokens;
                          regression if similarity < JACCARD_THRESHOLD (0.97)
  (iv) console_errors   – regression if new JS errors appear after the patch

Usage (programmatic):
    from regression_tool_v1.eval import evaluate_patch
    result = evaluate_patch(patch_file, template_info, output_dir)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

JACCARD_THRESHOLD = 0.97     # below → content regression
DOM_LSH_THRESHOLD = 8        # Hamming bits above → DOM regression (out of 64)

# ---------------------------------------------------------------------------
# Path setup so we can import common from the parent package
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_EVAL_DIR = _THIS_DIR.parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from common import (  # noqa: E402
    clone_repo,
    apply_patch,
    find_free_port,
    load_cwv_metadata,
    snapshot_site,
)


# ===========================================================================
# (i) GPT-4.1 screenshot comparison
# ===========================================================================

def _gpt_screenshot_compare(
    baseline_img: Path,
    patched_img: Path,
) -> dict[str, Any]:
    """
    Ask GPT-4.1 whether the patched screenshot introduces a visual regression.

    Returns: { "regression": bool|None, "raw_response": str, "error": str|None }
    """
    api_key  = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    version  = os.getenv("OPENAI_API_VERSION", "2024-12-01-preview")

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
        "The SECOND image is the same site after a code patch was applied. "
        "Determine whether the patch introduced a VISUAL REGRESSION — a visible "
        "problem that negatively impacts user experience.\n\n"
        "Count as regression: missing content, broken layout, invisible elements, "
        "severe colour/font breakage, missing navigation.\n"
        "Do NOT count as regression: minor spacing tweaks, load-time optimisations "
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
# (ii) DOM LSH — SimHash of canonicalized DOM
# ===========================================================================

def _canonicalize_dom(html: str) -> str:
    """
    Reduce HTML to a canonical token stream: tag names + sorted class names.
    Strips text content, IDs, and non-class attributes so that purely
    cosmetic changes (e.g. added inline styles) don't inflate the distance.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Fallback: strip all tags and use raw words
        return re.sub(r"<[^>]+>", " ", html)

    soup = BeautifulSoup(html, "html.parser")
    tokens: list[str] = []
    for tag in soup.find_all():
        tok = tag.name
        classes = tag.get("class") or []
        if classes:
            tok += "." + ".".join(sorted(classes))
        tokens.append(tok)
    return " ".join(tokens)


def _simhash(text: str, bits: int = 64) -> int:
    """Compute a 64-bit SimHash fingerprint for *text*."""
    tokens = text.split()
    if not tokens:
        return 0

    v = [0] * bits
    for token in tokens:
        h = int(hashlib.md5(token.encode("utf-8", errors="replace")).hexdigest(), 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1

    fp = 0
    for i in range(bits):
        if v[i] > 0:
            fp |= 1 << i
    return fp


def _hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _dom_lsh_check(baseline_html: str, patched_html: str) -> dict[str, Any]:
    """
    Returns: { "regression": bool, "hamming_distance": int, "threshold": int }
    """
    canon_base    = _canonicalize_dom(baseline_html)
    canon_patched = _canonicalize_dom(patched_html)
    h_base    = _simhash(canon_base)
    h_patched = _simhash(canon_patched)
    dist = _hamming_distance(h_base, h_patched)
    return {
        "regression":       dist > DOM_LSH_THRESHOLD,
        "hamming_distance": dist,
        "threshold":        DOM_LSH_THRESHOLD,
    }


# ===========================================================================
# (iii) Jaccard text similarity
# ===========================================================================

def _extract_text_tokens(html: str) -> set[str]:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # Remove script/style noise
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)

    return set(re.findall(r"\w+", text.lower()))


def _jaccard_check(baseline_html: str, patched_html: str) -> dict[str, Any]:
    """
    Returns: { "regression": bool, "similarity": float, "threshold": float }
    """
    t1 = _extract_text_tokens(baseline_html)
    t2 = _extract_text_tokens(patched_html)

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
# (iv) Console error check
# ===========================================================================

def _console_error_check(
    baseline_errors: list[str],
    patched_errors: list[str],
) -> dict[str, Any]:
    """Flag regression if the patch introduces new console errors."""
    baseline_set = set(baseline_errors)
    new_errors   = [e for e in patched_errors if e not in baseline_set]
    return {
        "regression":       len(new_errors) > 0,
        "new_errors":       new_errors,
        "baseline_count":   len(baseline_errors),
        "patched_count":    len(patched_errors),
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
    Evaluate *patch_file* with regression tool v1.

    Args:
        patch_file:    Path to the .patch file.
        template_info: Dict with keys: repo_id, commit_id, framework.
        output_dir:    Directory to write screenshots and intermediate files.

    Returns a result dict:
    {
        "patch": str,
        "template_id": int,
        "agent": str,
        "checks": {
            "gpt_visual":     { "regression": bool, ... },
            "dom_lsh":        { "regression": bool, "hamming_distance": int, ... },
            "jaccard_text":   { "regression": bool, "similarity": float, ... },
            "console_errors": { "regression": bool, "new_errors": [...], ... },
        },
        "overall_regression": bool,
        "metadata": { "desktop": {...}, "mobile": {...} },
        "error": str|None,
    }
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    baseline_img  = output_dir / "baseline.png"
    patched_img   = output_dir / "patched.png"
    baseline_html_path = output_dir / "baseline.html"
    patched_html_path  = output_dir / "patched.html"

    repo_id    = template_info["repo_id"]
    commit_id  = template_info["commit_id"]
    framework  = template_info["framework"]

    metadata = load_cwv_metadata(patch_file)

    with tempfile.TemporaryDirectory(prefix="vr_v1_") as tmp:
        repo_dir = Path(tmp) / "repo"

        # 1. Clone at baseline
        if not clone_repo(repo_id, commit_id, repo_dir):
            return _error_result(patch_file, template_info, metadata,
                                 "git clone failed")

        port = find_free_port()

        # 2. Snapshot baseline
        logger.info("[v1] Snapshotting baseline for %s ...", patch_file.name)
        snap_base = snapshot_site(repo_dir, framework, port,
                                  baseline_img, baseline_html_path)
        if not snap_base["ok"]:
            return _error_result(patch_file, template_info, metadata,
                                 "baseline snapshot failed")

        # 3. Apply patch
        apply_patch(repo_dir, patch_file)

        # 4. Snapshot patched
        logger.info("[v1] Snapshotting patched for %s ...", patch_file.name)
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

    gpt_result    = _gpt_screenshot_compare(baseline_img, patched_img) \
        if baseline_img.exists() and patched_img.exists() \
        else {"regression": False, "error": "screenshots missing"}

    dom_result    = _dom_lsh_check(baseline_html, patched_html)
    jaccard_result = _jaccard_check(baseline_html, patched_html)
    console_result = _console_error_check(
        snap_base["console_errors"], snap_pat["console_errors"]
    )

    checks = {
        "gpt_visual":     gpt_result,
        "dom_lsh":        dom_result,
        "jaccard_text":   jaccard_result,
        "console_errors": console_result,
    }
    overall = any(v.get("regression") is True for v in checks.values())

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
    # e.g. "101_template_claudecode" → "claudecode"
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
