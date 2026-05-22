#!/usr/bin/env python3
"""
Visual regression validator for evaluate.sh — powered by regression_tool_v2.

Compares the patched site (already running at --url) against the unmodified
baseline (cloned fresh from GitHub) using regression_tool_v2:
  - Structural DOM/IoU matching
  - Jaccard text-token similarity
  - GPT-4.1 screenshot comparison (baseline vs patched)
  - Console error diff

Falls back to the original single-image GPT validity check if v2 modules
are not importable or the baseline clone fails.

Usage:
    python3 visual_validate.py \
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
import base64
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

# ── Locate regression_eval on sys.path ──────────────────────────────────────
_HARNESS_DIR      = Path(__file__).resolve().parent
_REGRESSION_DIR   = _HARNESS_DIR.parent / "scripts" / "regression_eval"
_REGRESSION_V2DIR = _REGRESSION_DIR / "regression_tool_v2"
for _p in (_REGRESSION_DIR, _REGRESSION_V2DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

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
    _COMMON_OK = True
except ImportError as _e:
    print(f"[visual] WARN: regression_eval common not importable: {_e}", file=sys.stderr)
    _COMMON_OK = False

try:
    from regression_tool_v2.eval import (  # noqa: E402
        _structural_check,
        _jaccard_check,
        _gpt_screenshot_compare,
        _console_error_check,
    )
    _V2_OK = True
except ImportError as _e:
    print(f"[visual] WARN: regression_tool_v2 not importable: {_e}", file=sys.stderr)
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
    if _COMMON_OK:
        ss_ok  = take_screenshot(url, screenshot_path)
        html   = fetch_html(url)
        errors = capture_console_errors(url)
        if html:
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(html, encoding="utf-8")
        return {"ok": ss_ok, "console_errors": errors}

    # Minimal fallback when common is unavailable
    try:
        from playwright.sync_api import sync_playwright
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, timeout=30_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                page.screenshot(path=str(screenshot_path), full_page=True)
            finally:
                browser.close()
        return {"ok": True, "console_errors": []}
    except Exception as exc:
        print(f"[visual] Screenshot failed: {exc}", file=sys.stderr)
        return {"ok": False, "console_errors": []}


# ── GPT single-image validity (fallback) ─────────────────────────────────────

def _gpt_validity_check(screenshot_path: Path, repo_id: str) -> dict:
    """Original v1 behaviour: ask GPT if the page looks like a valid website."""
    api_key  = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not api_key or not endpoint:
        return {"regression": None, "error": "Azure credentials not set"}
    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(
            api_key=api_key,
            api_version="2024-02-15-preview",
            azure_endpoint=endpoint,
        )
        b64 = base64.b64encode(screenshot_path.read_bytes()).decode()
        resp = client.chat.completions.create(
            model=os.getenv("AZURE_DEPLOYMENT", "gpt-4.1"),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "Does this screenshot show a properly rendered, valid website? "
                        "It should NOT be blank, a directory listing, a 404, or broken code. "
                        'Reply JSON: {"is_valid": boolean, "reason": string}'
                    )},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{b64}"
                    }},
                ],
            }],
            response_format={"type": "json_object"},
        )
        r = json.loads(resp.choices[0].message.content)
        return {
            "regression": not r.get("is_valid", True),
            "reason":     r.get("reason", ""),
        }
    except Exception as exc:
        return {"regression": None, "error": str(exc)}


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
    parser.add_argument("--patch-file",      type=Path, default=None,
                        help="Patch file path (informational only)")
    parser.add_argument("--output-json",     required=True, type=Path,
                        help="Output path for the regression result JSON")
    args = parser.parse_args()

    framework = _normalize_framework(args.framework)

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
        return _error_result(args.output_json, "patched site screenshot failed")

    # ── 2. Clone + snapshot baseline ─────────────────────────────────────────
    snap_baseline = {"ok": False, "console_errors": []}
    if _COMMON_OK:
        print(f"[visual] Cloning baseline {args.repo_id} ...")
        with tempfile.TemporaryDirectory(prefix="vv2_baseline_") as tmp:
            repo_dir = Path(tmp) / "repo"
            if clone_repo(args.repo_id, args.commit_id, repo_dir):
                port = find_free_port()
                try:
                    print(f"[visual] Snapshotting baseline on port {port} ...")
                    snap_baseline = snapshot_site(
                        repo_dir, framework, port,
                        baseline_img, baseline_html_path,
                    )
                finally:
                    release_port(port)
            else:
                print("[visual] WARN: baseline clone failed — skipping structural checks",
                      file=sys.stderr)
    else:
        print("[visual] WARN: common not available — skipping baseline clone",
              file=sys.stderr)

    # ── 3. Run checks ─────────────────────────────────────────────────────────
    checks: dict = {}

    if _V2_OK and snap_baseline["ok"]:
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
            print(f"[visual] WARN: structural check timed out — skipping ({_e})", file=sys.stderr)
            checks["structural"] = {"regression": None, "error": "timeout"}
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, _old_handler)

        # Jaccard text similarity
        baseline_html = baseline_html_path.read_text(encoding="utf-8") \
            if baseline_html_path.exists() else ""
        patched_html  = patched_html_path.read_text(encoding="utf-8") \
            if patched_html_path.exists() else ""
        checks["jaccard_text"] = _jaccard_check(baseline_html, patched_html)

        # GPT visual comparison (baseline vs patched)
        if baseline_img.exists() and patched_img.exists():
            print("[visual] Running GPT screenshot comparison ...")
            checks["gpt_visual"] = _gpt_screenshot_compare(baseline_img, patched_img)
        else:
            checks["gpt_visual"] = {"regression": None, "error": "screenshots missing"}

        # Console error diff
        checks["console_errors"] = _console_error_check(
            snap_baseline["console_errors"],
            snap_patched["console_errors"],
        )

    else:
        # v2 unavailable or baseline failed — fall back to single-image GPT validity
        print("[visual] Falling back to single-image GPT validity check ...")
        checks["gpt_validity"] = _gpt_validity_check(patched_img, args.repo_id)

    # ── 4. Overall verdict ────────────────────────────────────────────────────
    overall_regression = any(v.get("regression") is True for v in checks.values())

    result = {
        "is_valid":           not overall_regression,
        "overall_regression": overall_regression,
        "checks":             checks,
        "repo_id":            args.repo_id,
        "framework":          framework,
        "url":                args.url,
    }
    _write_result(args.output_json, result)

    status = "REGRESSION DETECTED" if overall_regression else "OK (no regression)"
    print(f"[visual] {status}")
    print(f"[visual] Result saved: {args.output_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
