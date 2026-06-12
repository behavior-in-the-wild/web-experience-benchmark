#!/usr/bin/env python3
"""
Visual regression validator for evaluate.sh — powered by regression_tool_v2.

Compares the patched site (already running at --url) against the unmodified
baseline (cloned fresh from GitHub) using regression_tool_v2:
  - Structural DOM/IoU matching
  - Jaccard text-token similarity
  - GPT-4.1 screenshot comparison (baseline vs patched)
  - Console error diff

Usage:
    python3 src/regression_tool/visual_validate.py \
        --url http://bore.pub:PORT/ \
        --screenshot-path /path/screenshot.png \
        --repo-id user/repo \
        --commit-id abc1234 \
        --framework Hugo \
        --output-json /path/result.json \
        [--patch-file /path/to/patch]

Environment:
    AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_DEPLOYMENT
"""

import argparse
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Locate regression_tool on sys.path ──────────────────────────────────────
_REGRESSION_TOOL_DIR = Path(__file__).resolve().parent
_SRC_DIR = _REGRESSION_TOOL_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
if str(_REGRESSION_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_REGRESSION_TOOL_DIR))

from browser_config import (  # noqa: E402
    snapshot_metadata,
)

try:
    from common import (
        clone_repo,
        find_free_port,
        release_port,
        snapshot_site,
        take_screenshot,
        fetch_html,
        capture_console_errors,
    )
    from docker_tool.resources import SlotLease
    _COMMON_OK = True
except ImportError as _e:
    print(f"[visual] WARN: regression_tool common not importable: {_e}", file=sys.stderr)
    _COMMON_OK = False

try:
    from eval import (  # noqa: E402
        _structural_check,
        _jaccard_check,
        _gpt_screenshot_compare,
        _console_error_check,
        _vote_visual_regression,
    )
    _V2_OK = True
except ImportError as _e:
    print(f"[visual] WARN: regression_tool eval not importable: {_e}", file=sys.stderr)
    _V2_OK = False

# ── Framework name normalisation ─────────────────────────────────────────────
# evaluate.sh lowercases FRAMEWORK; common.py's _FRAMEWORK_SCRIPT uses title-case.
_FRAMEWORK_NORM = {
    "express":     "Express",
    "static html": "Static HTML",
    "statichtml":  "Static HTML",
    "jekyll":      "Jekyll",
    "hugo":        "Hugo",
    "hexo":        "Hexo",
    "pelican":     "Pelican",
    "quarto":      "Quarto",
    "react":       "React",
    "vue":         "Vue",
    "next.js":     "Next.js",
    "nextjs":      "Next.js",
    "next":        "Next.js",
}


def _normalize_framework(fw: str) -> str:
    return _FRAMEWORK_NORM.get(fw.lower().strip(), fw)


# ── Patched-site snapshot ────────────────────────────────────────────────────

def _snapshot_patched(url: str, screenshot_path: Path, html_path: Path) -> dict:
    """Capture screenshot, rendered HTML, and console errors from the running patched site."""
    try:
        ss_ok = take_screenshot(url, screenshot_path)
        if not ss_ok:
            return {"ok": False, "console_errors": [], "error": "screenshot failed"}
        html = fetch_html(url)
        if not html:
            return {"ok": False, "console_errors": [], "error": "html fetch returned empty content"}
        errors = capture_console_errors(url)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(html, encoding="utf-8")
        return {
            "ok": True,
            "console_errors": errors,
            "browser_config": snapshot_metadata(),
        }
    except Exception as exc:
        print(f"[visual] Screenshot failed: {exc}", file=sys.stderr)
        return {"ok": False, "console_errors": [], "error": str(exc)}


# ── Output helpers ───────────────────────────────────────────────────────────

def _write_result(output_json: Path, result: dict) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _error_result(output_json: Path, msg: str) -> int:
    _write_result(output_json, {
        "is_valid": False,
        "overall_regression": None,
        "status": "invalid_eval",
        "error": msg,
    })
    print(f"[visual] ERROR: {msg}", file=sys.stderr)
    return 1


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Visual regression check: patched site vs baseline (v2)."
    )
    parser.add_argument("--url",             required=True,
                        help="Patched site URL (already running, e.g. bore.pub)")
    parser.add_argument("--screenshot-path", required=True, type=Path,
                        help="Output path for the patched screenshot PNG")
    parser.add_argument("--repo-id",         required=True,
                        help="GitHub repo (user/repo) for baseline clone")
    parser.add_argument("--commit-id",       default="",
                        help="Pinned commit SHA for the baseline clone")
    parser.add_argument("--framework",       default="Static HTML",
                        help="Framework name from the CSV (case-insensitive)")
    parser.add_argument("--host-file-path",  default="",
                        help="Harness host_files script path used for baseline hosting")
    parser.add_argument("--slot-json",       default="",
                        help="Optional serialized Docker resource slot for baseline hosting")
    parser.add_argument("--patch-file",      type=Path, default=None,
                        help="Patch file path (informational only)")
    parser.add_argument("--output-json",     required=True, type=Path,
                        help="Output path for the regression result JSON")
    args = parser.parse_args()

    framework = _normalize_framework(args.framework)
    if not _COMMON_OK:
        return _error_result(args.output_json, "regression_tool common import failed")
    if not _V2_OK:
        return _error_result(args.output_json, "regression_tool eval import failed")

    slot = None
    if args.slot_json:
        try:
            slot = SlotLease(**json.loads(args.slot_json))
        except Exception as exc:
            return _error_result(args.output_json, f"invalid slot-json: {exc}")

    work_dir           = args.output_json.parent / (args.output_json.stem + "_v2_work")
    patched_img        = args.screenshot_path
    patched_html_path  = work_dir / "patched.html"
    baseline_img       = work_dir / "baseline.png"
    baseline_html_path = work_dir / "baseline.html"
    structural_dir     = work_dir / "structural"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Snapshot patched site (already running) ───────────────────────────
    print(f"[visual] Snapshotting patched site: {args.url} ...")
    snap_patched = _snapshot_patched(args.url, patched_img, patched_html_path)
    if not snap_patched["ok"]:
        return _error_result(
            args.output_json,
            f"patched site snapshot failed: {snap_patched.get('error', 'unknown error')}",
        )

    # ── 2. Clone + snapshot baseline ─────────────────────────────────────────
    snap_baseline = {"ok": False, "console_errors": []}
    print(f"[visual] Cloning baseline {args.repo_id} ...")
    with tempfile.TemporaryDirectory(prefix="vv2_baseline_") as tmp:
        repo_dir = Path(tmp) / "repo"
        if not clone_repo(
            args.repo_id,
            args.commit_id,
            repo_dir,
            work_dir / "baseline_clone_meta.json",
        ):
            return _error_result(args.output_json, "baseline clone failed")
        port = find_free_port()
        try:
            print(f"[visual] Snapshotting baseline on port {port} ...")
            snap_baseline = snapshot_site(
                repo_dir, framework, port,
                baseline_img, baseline_html_path,
                host_file_path=args.host_file_path,
                slot=slot,
            )
        finally:
            release_port(port)
    if not snap_baseline["ok"]:
        return _error_result(
            args.output_json,
            f"baseline snapshot failed: {snap_baseline.get('error', 'unknown error')}",
        )

    # ── 3. Run checks ─────────────────────────────────────────────────────────
    checks: dict = {}

    # Structural DOM/IoU
    print("[visual] Running structural DOM check ...")
    def _structural_timeout(signum, frame):
        raise RuntimeError("structural check timed out after 300s")
    _old_handler = signal.signal(signal.SIGALRM, _structural_timeout)
    signal.alarm(300)
    try:
        checks["structural"] = _structural_check(
            baseline_html_path, patched_html_path,
            baseline_img, patched_img,
            structural_dir,
        )
    except RuntimeError as _e:
        return _error_result(args.output_json, str(_e))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, _old_handler)
    if checks["structural"].get("regression") is None:
        return _error_result(
            args.output_json,
            f"structural check failed: {checks['structural'].get('error', 'unknown error')}",
        )

    # Jaccard text similarity
    if not baseline_html_path.exists() or not patched_html_path.exists():
        return _error_result(args.output_json, "baseline or patched HTML snapshot missing")
    baseline_html = baseline_html_path.read_text(encoding="utf-8")
    patched_html = patched_html_path.read_text(encoding="utf-8")
    checks["jaccard_text"] = _jaccard_check(baseline_html, patched_html)
    if checks["jaccard_text"].get("regression") is None:
        return _error_result(
            args.output_json,
            f"jaccard check failed: {checks['jaccard_text'].get('error', 'unknown error')}",
        )

    # GPT visual comparison (baseline vs patched)
    if not baseline_img.exists() or not patched_img.exists():
        return _error_result(args.output_json, "baseline or patched screenshot missing")
    print("[visual] Running GPT screenshot comparison ...")
    checks["gpt_visual"] = _gpt_screenshot_compare(
        baseline_img,
        patched_img,
        baseline_html_path=baseline_html_path,
        patched_html_path=patched_html_path,
        output_dir=work_dir / "gpt_visual_tiles",
    )
    if checks["gpt_visual"].get("regression") is None:
        return _error_result(
            args.output_json,
            f"GPT visual check failed: {checks['gpt_visual'].get('error', 'unknown error')}",
        )

    # Console error diff
    checks["console_errors"] = _console_error_check(
        snap_baseline["console_errors"],
        snap_patched["console_errors"],
    )

    # ── 4. Overall verdict ────────────────────────────────────────────────────
    vote = _vote_visual_regression(checks)
    overall_regression = vote["overall_regression"]

    result = {
        "is_valid":           not overall_regression,
        "overall_regression": overall_regression,
        "status":             "valid",
        "vote":               vote,
        "checks":             checks,
        "repo_id":            args.repo_id,
        "framework":          framework,
        "url":                args.url,
        "browser_config":     snapshot_metadata(),
    }
    _write_result(args.output_json, result)

    status = "REGRESSION DETECTED" if overall_regression else "OK (no regression)"
    print(f"[visual] {status}")
    print(f"[visual] Result saved: {args.output_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
