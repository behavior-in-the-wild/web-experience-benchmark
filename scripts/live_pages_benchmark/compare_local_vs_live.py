#!/usr/bin/env python3
"""
compare_local_vs_live.py

For each previously mirrored page in live_assets_eds/:
  1. Spins up a temporary local HTTP server serving that page's directory
  2. Uses Playwright to visit BOTH the local mirror and the live URL
  3. Captures:
       - Console messages (errors, warnings, info) from each
       - A full-page screenshot from each
  4. Writes per-page comparison into:

     comparison_results/
       <domain_slug>/
         <page_slug>/
           live_screenshot.png
           local_screenshot.png
           comparison.json        ← structured diff of console logs + counts

  5. Writes a top-level report: comparison_results/report.json

Usage:
    python compare_local_vs_live.py \\
        --assets-dir live_assets_eds \\
        --output     comparison_results \\
        --workers    2          # parallel page pairs (each needs 2 browser pages)

Resume: pages with existing comparison.json are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import socket
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_ASSETS_DIR = "live_assets_eds"
DEFAULT_OUTPUT     = "comparison_results"
DEFAULT_WORKERS    = 2
PAGE_TIMEOUT_MS    = 30_000
NETWORKIDLE_MS     = 6_000

# Console message levels to capture
CAPTURE_LEVELS = {"error", "warning", "warn"}

# Viewports for dual-device comparison
_DESKTOP_VP = {"width": 1440, "height": 900}
_MOBILE_VP  = {"width": 375,  "height": 812}


# ── Local HTTP server ─────────────────────────────────────────────────────────

class _CORSHandler(SimpleHTTPRequestHandler):
    """HTTP handler with CORS headers, correct MIME types, and suppressed logs."""

    # Extended MIME types for modern web assets
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".woff2": "font/woff2",
        ".woff":  "font/woff",
        ".ttf":   "font/ttf",
        ".otf":   "font/otf",
        ".mjs":   "application/javascript",
        ".webp":  "image/webp",
        ".avif":  "image/avif",
        ".webm":  "video/webm",
        ".mp4":   "video/mp4",
        ".json":  "application/json",
        ".svg":   "image/svg+xml",
    }

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args): pass
    def log_error(self, *args):   pass

    def copyfile(self, source, outputfile):
        """Suppress BrokenPipeError — happens when Playwright closes connection
        mid-transfer (normal browser behaviour for large assets)."""
        try:
            super().copyfile(source, outputfile)
        except BrokenPipeError:
            pass


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalServer:
    """Serves a single directory on a random localhost port.

    Use as a context manager:
        with LocalServer(page_dir) as base_url:
            ...
    """
    def __init__(self, directory: Path):
        self.directory = directory
        self.port = _find_free_port()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> str:
        handler = lambda *a, **kw: _CORSHandler(*a, directory=str(self.directory), **kw)
        self._server = HTTPServer(("localhost", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://localhost:{self.port}/index.html"

    def __exit__(self, *_):
        if self._server:
            self._server.shutdown()


def _compute_ssim(img_path_a: Path, img_path_b: Path) -> float | None:
    """Compute the SSIM score between two screenshots.

    Returns a float 0.0-1.0 (1.0 = identical) or None if Pillow/numpy
    is not available or images can't be loaded.
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None

    try:
        a = np.array(Image.open(img_path_a).convert("L"))  # grayscale
        b = np.array(Image.open(img_path_b).convert("L"))

        # Resize to same dimensions (use smaller)
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w].astype(np.float64), b[:h, :w].astype(np.float64)

        # SSIM constants
        C1 = (0.01 * 255) ** 2
        C2 = (0.03 * 255) ** 2

        mu_a = a.mean()
        mu_b = b.mean()
        sigma_a_sq = a.var()
        sigma_b_sq = b.var()
        sigma_ab = ((a - mu_a) * (b - mu_b)).mean()

        ssim = ((2 * mu_a * mu_b + C1) * (2 * sigma_ab + C2)) / \
               ((mu_a**2 + mu_b**2 + C1) * (sigma_a_sq + sigma_b_sq + C2))
        return round(float(ssim), 4)
    except Exception:
        return None


# ── Playwright helpers ────────────────────────────────────────────────────────

# Thread-local storage: each comparison worker gets its own Playwright + browser.
# Same fix as fetch_live_assets.py — sharing sync_playwright across threads
# causes: greenlet.error: Cannot switch to a different thread
_tls = threading.local()


def _get_browser():
    if not getattr(_tls, "browser", None):
        from playwright.sync_api import sync_playwright
        _tls.pw      = sync_playwright().start()
        _tls.browser = _tls.pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
    return _tls.browser


def _visit_and_capture(url: str, screenshot_path: Path,
                       viewport: dict | None = None) -> dict:
    """
    Open url in a new browser context, collect console messages,
    take a full-page screenshot.

    Returns:
        {
            "url": str,
            "status": "ok" | "error",
            "error": str | None,
            "console_messages": [{"level": str, "text": str}, ...],
            "console_errors":   [str],           # just error texts
            "console_warnings": [str],
            "screenshot":       str (relative path) | None,
        }
    """
    vp = viewport or _DESKTOP_VP
    result = {
        "url":              url,
        "status":           "ok",
        "error":            None,
        "console_messages": [],
        "console_errors":   [],
        "console_warnings": [],
        "screenshot":       None,
    }

    browser = _get_browser()
    context = browser.new_context(
        viewport=vp,
        ignore_https_errors=True,
    )
    page = context.new_page()

    def _on_console(msg):
        level = msg.type.lower()  # "error","warning","log","info",...
        text  = msg.text
        result["console_messages"].append({"level": level, "text": text})
        if level in ("error",):
            result["console_errors"].append(text)
        if level in ("warning", "warn"):
            result["console_warnings"].append(text)

    def _on_pageerror(err):
        result["console_errors"].append(f"[uncaught] {err}")
        result["console_messages"].append({"level": "error", "text": f"[uncaught] {err}"})

    page.on("console",   _on_console)
    page.on("pageerror", _on_pageerror)

    try:
        page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_MS)
        except Exception:
            pass

        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
        result["screenshot"] = screenshot_path.name

    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)
    finally:
        try:
            page.close()
            context.close()
        except Exception:
            pass

    return result


# ── Comparison logic ──────────────────────────────────────────────────────────

# Errors that are expected/unavoidable on any local mirror and should not
# count as "broken mirror" signals.  These are inherent to serving a
# mirrored page on localhost — the rendering, layout, CSS, and real page JS
# are completely unaffected.
_EXPECTED_MIRROR_ERRORS = (
    # ── HTTP server limitations ───────────────────────────────────────────
    "501",                              # python http.server doesn't support POST
    "Unsupported method",
    "Failed to load resource",          # generic fetch failure (analytics, beacons)

    # ── CORS (always fails on localhost) ──────────────────────────────────
    "has been blocked by CORS",
    "Access-Control-Allow-Origin",
    "blocked by CORS policy",
    "CORS request did not succeed",
    "Cross-Origin Read Blocking",

    # ── Adobe analytics ecosystem ─────────────────────────────────────────
    "_satellite",                       # Adobe Launch tag manager
    "adobedtm.com",                     # Adobe DTM CDN
    "launch-",                          # Adobe Launch script names
    "at.js",                            # Adobe Target
    "demdex",                           # Adobe Audience Manager
    "omtrdc",                           # Adobe Analytics tracking
    "alloy",                            # Adobe Alloy (Web SDK)
    "populateFooter",                   # AEM analytics helpers
    "populateHeader",

    # ── Google analytics / tag manager ────────────────────────────────────
    "google-analytics.com",
    "googletagmanager.com",
    "gtag",
    "gtm.js",
    "analytics.js",
    "ga.js",

    # ── Cookie consent / privacy ──────────────────────────────────────────
    "OneTrust",
    "onetrust.com",
    "cookielaw.org",
    "cookieconsent",

    # ── Qualtrics / survey ────────────────────────────────────────────────
    "qualtrics.com",
    "siteintercept",

    # ── Other third-party ─────────────────────────────────────────────────
    "hotjar.com",
    "clarity.ms",
    "vwo.com",
    "newrelic.com",
    "nr-data.net",
    "cdn.segment.com",
    "sentry.io",

    # ── Common mirror-only failures ───────────────────────────────────────
    "net::ERR_",                        # Chrome network error codes
    "NS_ERROR_",                        # Firefox network error codes
    "WebSocket",                        # WS connections can't work on mirror
    "ERR_CONNECTION_REFUSED",
    "ERR_NAME_NOT_RESOLVED",
    "TypeError: NetworkError",
)

def _is_expected_mirror_error(msg: str) -> bool:
    return any(tok in msg for tok in _EXPECTED_MIRROR_ERRORS)

def _diff_console(live: dict, local: dict) -> dict:
    """Produce a structured diff of console outputs."""
    live_errors   = set(live["console_errors"])
    local_errors  = set(local["console_errors"])
    live_warns    = set(live["console_warnings"])
    local_warns   = set(local["console_warnings"])

    new_errors = sorted(local_errors - live_errors)
    # Filter expected-on-mirrors noise so the gate is signal-only
    new_errors_filtered = [e for e in new_errors if not _is_expected_mirror_error(e)]

    return {
        # Errors only in local (introduced by mirroring)  — filtered list
        "new_errors_local":        new_errors_filtered,
        # Unfiltered for debugging
        "new_errors_local_raw":    sorted(local_errors - live_errors),
        # Errors only in live (fixed or gated behind auth in local)
        "errors_only_live":        sorted(live_errors - local_errors),
        # Errors common to both
        "errors_both":             sorted(live_errors & local_errors),

        "new_warnings_local":      sorted(local_warns - live_warns),
        "warnings_only_live":      sorted(live_warns - local_warns),
        "warnings_both":           sorted(live_warns & local_warns),

        # Counts
        "live_error_count":        len(live_errors),
        "local_error_count":       len(local_errors),
        "live_warning_count":      len(live_warns),
        "local_warning_count":     len(local_warns),
    }


def compare_page(
    live_url:   str,
    page_dir:   Path,
    output_dir: Path,
    force:      bool = False,
) -> dict:
    """Full comparison for one page at BOTH viewports."""

    output_dir.mkdir(parents=True, exist_ok=True)
    comp_path = output_dir / "comparison.json"

    # Resume check (skip unless --force)
    if comp_path.exists() and not force:
        logger.info("Skip (done): %s", live_url)
        return json.loads(comp_path.read_text())

    comparison = {
        "live_url":    live_url,
        "local_dir":   str(page_dir),
        "devices":     {},
    }

    for device_name, vp in [("desktop", _DESKTOP_VP), ("mobile", _MOBILE_VP)]:
        live_ss  = output_dir / f"live_screenshot_{device_name}.png"
        local_ss = output_dir / f"local_screenshot_{device_name}.png"

        # Capture live
        logger.info("  [LIVE %s]  %s", device_name, live_url)
        live_result = _visit_and_capture(live_url, live_ss, viewport=vp)

        # Capture local
        logger.info("  [LOCAL %s] %s", device_name, page_dir)
        with LocalServer(page_dir) as local_url:
            local_result = _visit_and_capture(local_url, local_ss, viewport=vp)

        console_diff = _diff_console(live_result, local_result)

        ssim_score = None
        if live_ss.exists() and local_ss.exists():
            ssim_score = _compute_ssim(live_ss, local_ss)

        comparison["devices"][device_name] = {
            "viewport":          vp,
            "live_status":       live_result["status"],
            "local_status":      local_result["status"],
            "live_error":        live_result.get("error"),
            "local_error":       local_result.get("error"),
            "ssim":              ssim_score,
            "console_diff":      console_diff,
            "live_screenshot":   str(live_ss)  if live_ss.exists()  else None,
            "local_screenshot":  str(local_ss) if local_ss.exists() else None,
        }

        logger.info(
            "  ✓ %s [%s] | live_errs=%d  local_errs=%d  new_local=%d  ssim=%s",
            live_url, device_name,
            console_diff["live_error_count"],
            console_diff["local_error_count"],
            len(console_diff["new_errors_local"]),
            ssim_score,
        )

    comp_path.write_text(json.dumps(comparison, indent=2))
    return comparison


# ── Orchestrator ──────────────────────────────────────────────────────────────

def _discover_pages(assets_dir: Path) -> list[dict]:
    """Walk assets_dir to find all (live_url, page_dir) pairs via manifest.json."""
    pages = []
    for manifest_path in sorted(assets_dir.rglob("manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
            live_url = manifest.get("page_url")
            if live_url:
                pages.append({
                    "live_url": live_url,
                    "page_dir": manifest_path.parent,
                    "domain_slug": manifest_path.parent.parent.name,
                    "page_slug":   manifest_path.parent.name,
                })
        except Exception as e:
            logger.warning("Bad manifest %s: %s", manifest_path, e)
    return pages


def run(assets_dir: str, output_root: str, workers: int,
        force: bool = False) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pages = _discover_pages(Path(assets_dir))
    logger.info("Found %d pages under %s", len(pages), assets_dir)

    if not pages:
        logger.error("No manifest.json files found. Run fetch_live_assets.py first.")
        return

    try:
        from tqdm import tqdm
        wrap = lambda it, n: tqdm(it, total=n, unit="page")
    except ImportError:
        wrap = lambda it, n: it

    all_results = []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for p in pages:
            out_dir = Path(output_root) / p["domain_slug"] / p["page_slug"]
            fut = ex.submit(compare_page, p["live_url"], p["page_dir"], out_dir,
                           force=force)
            futures[fut] = p

        for fut in wrap(as_completed(futures), len(futures)):
            p = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = {
                    "live_url": p["live_url"],
                    "error": str(e),
                }
                logger.error("FAIL %s: %s", p["live_url"], e)
            result["domain_slug"] = p["domain_slug"]
            result["page_slug"]   = p["page_slug"]
            all_results.append(result)

    # ── Top-level report ──────────────────────────────────────────────────────
    report_path = Path(output_root) / "report.json"

    # Summary stats
    ok_pages        = [r for r in all_results if
                       any(d.get("live_status") == "ok"
                           for d in r.get("devices", {}).values())]
    new_err_total   = sum(
        len(d.get("console_diff", {}).get("new_errors_local", []))
        for r in ok_pages for d in r.get("devices", {}).values()
    )
    fixed_err_total = sum(
        len(d.get("console_diff", {}).get("errors_only_live", []))
        for r in ok_pages for d in r.get("devices", {}).values()
    )

    report = {
        "total_pages":            len(all_results),
        "live_ok":                sum(1 for r in all_results if r.get("live_status") == "ok"),
        "local_ok":               sum(1 for r in all_results if r.get("local_status") == "ok"),
        "total_new_errors_local": new_err_total,
        "total_fixed_errors":     fixed_err_total,
        "pages":                  all_results,
    }
    report_path.write_text(json.dumps(report, indent=2))

    logger.info(
        "Done. %d pages | %d new local errors | %d errors fixed in local | report → %s",
        len(all_results), new_err_total, fixed_err_total, report_path,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--assets-dir", default=DEFAULT_ASSETS_DIR,
                   help="Root dir from fetch_live_assets.py (default: live_assets_eds)")
    p.add_argument("--output",     default=DEFAULT_OUTPUT,
                   help="Output dir for comparisons (default: comparison_results)")
    p.add_argument("--workers",    type=int, default=DEFAULT_WORKERS,
                   help="Parallel page pairs (default 2; each uses 2 browser contexts)")
    p.add_argument("--page",       default=None,
                   help="Optional: compare only one page_dir path (for testing)")
    p.add_argument("--force",      action="store_true",
                   help="Re-run comparisons even if comparison.json exists")
    args = p.parse_args()

    Path(args.output).mkdir(parents=True, exist_ok=True)

    if args.page:
        page_dir = Path(args.page)
        manifest = json.loads((page_dir / "manifest.json").read_text())
        live_url = manifest["page_url"]
        out_dir  = Path(args.output) / page_dir.parent.name / page_dir.name
        compare_page(live_url, page_dir, out_dir, force=args.force)
    else:
        run(args.assets_dir, args.output, args.workers, force=args.force)


if __name__ == "__main__":
    main()
