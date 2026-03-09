#!/usr/bin/env python3
"""
fetch_live_assets.py

For each domain + page listed in EDSSites_CWV_joined_top50_pages_top10.jsonl,
uses Playwright to:
  1. Load the fully rendered page (waits for networkidle)
  2. Intercepts every network response and saves the body locally
  3. Rewrites all asset URLs in the rendered HTML to relative local paths
  4. Saves index.html + all assets into:

     live_assets_eds/
       <domain_slug>/
         <page_slug>/
           index.html          ← rendered DOM with rewritten paths
           assets/
             js/  css/  img/  fonts/  other/
           manifest.json       ← map of original URL → local path + metadata

Usage:
    python fetch_live_assets.py \\
        --input EDSSites_CWV_joined_top50_pages_top10.jsonl \\
        --output live_assets_eds \\
        --workers 3          # parallel pages (keep low — CPU-bound Playwright)

Resume: already-completed page dirs are skipped automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
DEFAULT_INPUT      = "EDSSites_CWV_joined_top50_pages_top10.jsonl"
DEFAULT_OUTPUT     = "live_assets_eds"
DEFAULT_WORKERS    = 3       # parallel Playwright pages; keep ≤ 5
PAGE_TIMEOUT_MS    = 30_000  # 30s page load timeout
NETWORKIDLE_MS     = 5_000   # wait up to 5s for network quiet after load
SCROLL_PAUSE_MS    = 2_000   # extra networkidle wait after scrolling
MAX_ASSET_BYTES    = 50 * 1024 * 1024   # 50 MB cap per asset (safety)

# Viewports to scroll through during capture
_DESKTOP_VP = {"width": 1440, "height": 900}
_MOBILE_VP  = {"width": 375,  "height": 812}

# Mime type → subdirectory under assets/
_MIME_DIR = {
    "javascript":  "js",
    "css":         "css",
    "html":        "html",
    "json":        "json",
    "xml":         "xml",
    "png":         "img",
    "jpeg":        "img",
    "jpg":         "img",
    "gif":         "img",
    "webp":        "img",
    "svg+xml":     "img",
    "svg":         "img",
    "ico":         "img",
    "woff":        "fonts",
    "woff2":       "fonts",
    "ttf":         "fonts",
    "otf":         "fonts",
    "eot":         "fonts",
    "mp4":         "media",
    "webm":        "media",
    "ogg":         "media",
    "mp3":         "media",
    "pdf":         "other",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 80) -> str:
    """Convert a URL / domain to a safe directory name."""
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^\w.\-]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_.")
    return text[:max_len]


def _domain_slug(domain_url: str) -> str:
    """e.g. 'https://worldbank.org' → 'worldbank.org'"""
    parsed = urlparse(domain_url)
    host = (parsed.netloc or parsed.path).rstrip("/")
    # Remove "www." prefix only as an exact string (not char-by-char like lstrip)
    if host.startswith("www."):
        host = host[4:]
    return host or _slugify(domain_url)


def _page_slug(page_url: str) -> str:
    """Turn a full URL into a safe folder name preserving path context."""
    parsed = urlparse(page_url)
    path = parsed.path.strip("/") or "home"
    path = re.sub(r"[^\w.\-/]", "_", path).replace("/", "__")
    if parsed.query:
        query_hash = hashlib.md5(parsed.query.encode()).hexdigest()[:6]
        path += f"__{query_hash}"
    return path[:100] or "home"


def _asset_subdir(content_type: str) -> str:
    """Map a Content-Type header to an asset subdirectory."""
    ct = content_type.split(";")[0].strip().lower()
    # strip 'application/', 'text/', 'image/', etc.
    subtype = ct.split("/")[-1]
    # also try text/javascript → js
    if "javascript" in ct:
        return "js"
    if "css" in ct:
        return "css"
    if "html" in ct:
        return "html"
    if "font" in ct or subtype in ("woff", "woff2", "ttf", "otf", "eot"):
        return "fonts"
    if "image" in ct or subtype in ("png", "jpeg", "jpg", "gif", "webp", "svg+xml", "ico"):
        return "img"
    if "video" in ct or "audio" in ct:
        return "media"
    return _MIME_DIR.get(subtype, "other")


def _safe_filename(url: str, content_type: str) -> str:
    """Derive a filename from a URL, keeping the original name when possible."""
    parsed = urlparse(url)
    name = Path(parsed.path).name or "index"
    # Strip query/fragment from name
    name = name.split("?")[0].split("#")[0]
    if not name or name == "/":
        name = "index"
    # Ensure the extension matches the actual content type
    ext_map = {
        "js": ".js", "css": ".css", "html": ".html",
        "woff": ".woff", "woff2": ".woff2", "ttf": ".ttf",
        "png": ".png", "jpeg": ".jpg", "gif": ".gif",
        "webp": ".webp", "svg+xml": ".svg", "ico": ".ico",
        "json": ".json", "xml": ".xml",
    }
    subtype = content_type.split(";")[0].split("/")[-1].strip().lower()
    if "javascript" in content_type.lower():
        subtype = "js"
    if "css" in content_type.lower():
        subtype = "css"
    wanted_ext = ext_map.get(subtype, "")
    if wanted_ext and not name.lower().endswith(wanted_ext):
        name = name + wanted_ext

    # Unique suffix: short hash of full URL to avoid collisions
    url_hash = hashlib.md5(url.encode()).hexdigest()[:6]
    stem = Path(name).stem
    suffix = Path(name).suffix or ".bin"
    return f"{stem}_{url_hash}{suffix}"


def _rewrite_html(html: str, url_map: dict[str, str]) -> str:
    """Replace all captured original URLs with their local relative paths.

    url_map: { original_url: relative_local_path }
    Sorted longest-first to avoid partial replacements.
    """
    for orig, local in sorted(url_map.items(), key=lambda x: -len(x[0])):
        html = html.replace(orig, local)
    return html


_CSS_URL_RE = re.compile(
    r"""url\(\s*(['"]?)(?!data:)([^'"()\s]+)\1\s*\)"""
    r"""|@import\s+['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


def _rewrite_css(css_text: str, css_orig_url: str, expanded_map: dict[str, str]) -> str:
    """
    Rewrite url() and @import references inside a CSS file so they point to
    locally saved assets — mirroring Chrome's Cmd+S behaviour.

    css_orig_url: the original URL of this CSS file (used to resolve relative refs).
    expanded_map: { url_variant → relative_path_from_page_dir } (full, root-abs, scheme-rel).
    """
    css_base = css_orig_url

    def _replace(m: re.Match) -> str:
        # Group 2 = url() target, group 3 = @import target
        ref = m.group(2) or m.group(3)
        if not ref:
            return m.group(0)

        # Resolve the reference to an absolute URL
        abs_url = urljoin(css_base, ref)

        # Try to find a local path in expanded_map (full URL, root-abs, scheme-rel)
        parsed  = urlparse(abs_url)
        root_ab = parsed.path + ("?" + parsed.query if parsed.query else "")
        scm_rel = "//" + parsed.netloc + parsed.path + ("?" + parsed.query if parsed.query else "")

        local_rel: str | None = (
            expanded_map.get(abs_url)
            or expanded_map.get(root_ab)
            or expanded_map.get(scm_rel)
        )
        if not local_rel:
            return m.group(0)  # keep original if we don't have the asset

        # local_rel is relative from page_dir.  We need it relative from the
        # CSS file's location (which is page_dir/assets/css/foo.css → ../../ prefix).
        # Use posixpath to compute the relative jump.
        import posixpath
        css_local_rel = expanded_map.get(abs_url) or expanded_map.get(root_ab) or local_rel
        # css_local_rel is like "assets/fonts/foo.woff2"
        # The CSS file itself is at something like "assets/css/bar.css"
        # Relative path from assets/css/ to assets/fonts/ is "../fonts/"
        css_dir_in_page = posixpath.dirname(
            expanded_map.get(css_base)
            or expanded_map.get("//" + urlparse(css_base).netloc + urlparse(css_base).path)
            or ""
        )
        if css_dir_in_page:
            try:
                local_rel = posixpath.relpath(css_local_rel, css_dir_in_page)
            except ValueError:
                local_rel = css_local_rel  # Windows fallback (shouldn't happen on Linux)

        # Reconstruct the original CSS token with the new path
        if m.group(2) is not None and m.group(3) is None:  # url() form
            quote = m.group(1) or ""
            return f"url({quote}{local_rel}{quote})"
        else:  # @import form
            quote = '"'
            return f"@import {quote}{local_rel}{quote}"

    return _CSS_URL_RE.sub(_replace, css_text)

# ── Analytics stub script ────────────────────────────────────────────────────
# Injected into every mirrored page's <head> so that analytics globals that are
# always unavailable on a local mirror don't crash inline scripts and flood the
# console with cascade JS errors (_satellite, fbq, gapi, etc.).
#
# The stubs are no-ops: they accept any call/property access silently.
# Layout, CSS, and real page JS are completely unaffected.
_ANALYTICS_STUB = """\
<script id="_mirror_analytics_stubs_">
/* Local-mirror stubs: silence analytics globals that are unreachable offline */
(function(){
  var noop = function(){};
  var proxy = function(name){
    return new Proxy(noop, {
      get: function(t,k){ return k === 'toString' ? function(){return name;} : proxy(name+'.'+k); },
      apply: function(){ return proxy(name+'()'); },
      set: function(){ return true; }
    });
  };
  var globals = [
    '_satellite','fbq','_fbq','gtag','ga','dataLayer',
    'google_tag_manager','google_tag_data','gapi',
    'Munchkin','_hsq','hbspt','_vwo_code','clarity',
    'Optanon','OptanonWrapper','OneTrust','ClickTaleExclude',
    'ttq','pintrk','uetq','snaptr','sas','ire','qp'
  ];
  globals.forEach(function(g){
    if(!window[g]) window[g] = proxy(g);
  });
  // dataLayer must be an array (GTM checks Array.isArray)
  if(!Array.isArray(window.dataLayer)) window.dataLayer = [];
})();
</script>
"""

# Thread-local storage: each worker thread gets its own Playwright + browser.
# Sharing a single sync_playwright browser across threads causes:
#   greenlet.error: Cannot switch to a different thread
# because sync_playwright wraps every call in a greenlet bound to its origin thread.
_tls = threading.local()


def _get_browser():
    """Return this thread's own Playwright browser, creating it on first call."""
    if not getattr(_tls, "browser", None):
        from playwright.sync_api import sync_playwright
        _tls.pw      = sync_playwright().start()
        _tls.browser = _tls.pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
    return _tls.browser


def fetch_page(
    page_url: str,
    page_dir: Path,
) -> dict:
    """
    Fetch a single page with Playwright, save all intercepted assets locally,
    rewrite the rendered HTML, and write index.html + manifest.json.

    Returns a summary dict.
    """
    page_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = page_dir / "assets"

    intercepted: dict[str, bytes] = {}          # url → raw bytes
    content_types: dict[str, str] = {}          # url → content-type
    failed_urls: list[str]        = []           # urls that failed to load
    lock = threading.Lock()
    _t_start = time.monotonic()                  # timing: page load start

    browser = _get_browser()
    context = browser.new_context(
        viewport=_DESKTOP_VP,
        ignore_https_errors=True,
        java_script_enabled=True,
    )
    page = context.new_page()

    def _on_response(response):
        try:
            url = response.url
            ct  = response.headers.get("content-type", "application/octet-stream")
            # Skip streaming media and very large responses
            if "video/" in ct or "audio/" in ct:
                return
            body = response.body()
            if len(body) > MAX_ASSET_BYTES:
                return
            with lock:
                intercepted[url] = body
                content_types[url] = ct
        except Exception:
            pass  # response may have been aborted

    def _on_request_failed(request):
        try:
            url = request.url
            # Only track static assets (not API/XHR calls — those fail due to CORS)
            if any(ext in url.lower() for ext in ('.css','.js','.woff','.woff2','.ttf','.png','.jpg','.svg','.webp')):
                with lock:
                    failed_urls.append(url)
        except Exception:
            pass

    page.on("response",       _on_response)
    page.on("requestfailed",  _on_request_failed)

    nav_error = None
    _t_loaded = None
    try:
        page.goto(page_url, timeout=PAGE_TIMEOUT_MS, wait_until="load")
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_MS)
        except Exception:
            pass  # networkidle timeout is acceptable

        # ─── Scroll to bottom to trigger intersection-observer lazy loads ────────
        # Many pages defer images, web-fonts, and JS chunks until they enter the
        # viewport.  Scrolling lets the response interceptor capture them all.
        try:
            page_height = page.evaluate("document.body.scrollHeight")
            step = 800
            for y in range(0, page_height, step):
                page.evaluate(f"window.scrollTo(0, {y})")
                page.wait_for_timeout(80)          # ~80ms per scroll step
            page.evaluate("window.scrollTo(0, 0)")  # scroll back to top for screenshot
            # Wait for any lazy-triggered network activity to settle
            try:
                page.wait_for_load_state("networkidle", timeout=SCROLL_PAUSE_MS)
            except Exception:
                pass
        except Exception:
            pass  # scroll failure is non-fatal

        # ─── Mobile viewport scroll pass ─────────────────────────────────────────
        # Resize to mobile and scroll again to trigger mobile-only lazy loads
        # (different intersection-observer thresholds, responsive breakpoints).
        # The response interceptor keeps capturing into the same asset pool.
        try:
            page.set_viewport_size(_MOBILE_VP)
            page.wait_for_timeout(500)  # brief settle for responsive CSS reflow
            mobile_height = page.evaluate("document.body.scrollHeight")
            for y in range(0, mobile_height, 400):
                page.evaluate(f"window.scrollTo(0, {y})")
                page.wait_for_timeout(80)
            page.evaluate("window.scrollTo(0, 0)")
            try:
                page.wait_for_load_state("networkidle", timeout=SCROLL_PAUSE_MS)
            except Exception:
                pass
        except Exception:
            pass  # mobile scroll failure is non-fatal

        # ─── Reset to desktop for final HTML capture ─────────────────────────────
        try:
            page.set_viewport_size(_DESKTOP_VP)
            page.wait_for_timeout(200)
        except Exception:
            pass

        rendered_html = page.content()
        _t_loaded = time.monotonic()              # timing: page fully loaded
    except Exception as e:
        nav_error = str(e)
        rendered_html = ""
    finally:
        try:
            page.close()
            context.close()
        except Exception:
            pass

    if failed_urls:
        logger.debug("  %d static assets failed to load for %s: %s",
                     len(failed_urls), page_url,
                     ", ".join(failed_urls[:5]))

    if nav_error:
        (page_dir / "error.txt").write_text(nav_error)
        return {"url": page_url, "status": "error", "error": nav_error, "assets_saved": 0}

    # ── Save assets and build url → local path map ──────────────────────────
    url_map: dict[str, str] = {}   # original URL → relative path from page_dir
    manifest: list[dict] = []

    for orig_url, body in intercepted.items():
        # Skip the page itself (HTML we already have from page.content())
        ct = content_types.get(orig_url, "application/octet-stream")
        if "html" in ct and orig_url.rstrip("/") == page_url.rstrip("/"):
            continue

        subdir  = _asset_subdir(ct)
        subdir_path = assets_dir / subdir
        subdir_path.mkdir(parents=True, exist_ok=True)

        filename = _safe_filename(orig_url, ct)
        local_path = subdir_path / filename

        # Write asset
        try:
            local_path.write_bytes(body)
        except Exception as e:
            logger.debug("Could not write %s: %s", local_path, e)
            continue

        # Relative path from page_dir/index.html
        rel = local_path.relative_to(page_dir).as_posix()
        url_map[orig_url] = rel

        manifest.append({
            "original_url":  orig_url,
            "local_path":    rel,
            "content_type":  ct,
            "bytes":         len(body),
            "sha256":        hashlib.sha256(body).hexdigest(),
        })

    # ── Rewrite HTML ────────────────────────────────────────────────────────
    # Expand url_map to also cover root-absolute (/path/...) and scheme-relative
    # (//host/path) forms — the browser interceptor keys on full URLs but
    # the rendered HTML often uses /path or //host forms, causing 404s.
    expanded_map: dict[str, str] = {}
    for orig_url, rel in url_map.items():
        from urllib.parse import urlparse as _up
        parsed = _up(orig_url)
        # root-absolute: /path?query
        root_abs = parsed.path + ("?" + parsed.query if parsed.query else "")
        if root_abs and root_abs != "/":
            expanded_map.setdefault(root_abs, rel)
        # scheme-relative: //host/path
        scheme_rel = "//" + parsed.netloc + parsed.path + ("?" + parsed.query if parsed.query else "")
        expanded_map.setdefault(scheme_rel, rel)
        # full URL (longest, added last so setdefault keeps existing entries)
        expanded_map.setdefault(orig_url, rel)
    # ── Rewrite HTML & inject analytics stubs ───────────────────────────────
    rewritten = _rewrite_html(rendered_html, expanded_map)

    # Inject analytics stubs right before </head> (or at top of <body> fallback)
    # This silences _satellite/fbq/gapi cascade errors without affecting layout.
    if "</head>" in rewritten:
        rewritten = rewritten.replace("</head>", _ANALYTICS_STUB + "</head>", 1)
    elif "<body" in rewritten:
        # No </head> — insert before first <body> tag
        rewritten = rewritten.replace("<body", _ANALYTICS_STUB + "<body", 1)

    (page_dir / "index.html").write_text(rewritten, encoding="utf-8", errors="replace")

    # ── Post-process CSS: rewrite url() / @import ────────────────────────────
    # Chrome Cmd+S does this recursively — we do the same so fonts, bg images,
    # and icon sprites referenced from CSS load correctly when served locally.
    for entry in manifest:
        ct = entry.get("content_type", "")
        if "css" not in ct.lower():
            continue
        css_local = page_dir / entry["local_path"]
        orig_url  = entry["original_url"]
        try:
            css_text    = css_local.read_text(encoding="utf-8", errors="replace")
            css_rewritten = _rewrite_css(css_text, orig_url, expanded_map)
            css_local.write_text(css_rewritten, encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug("CSS rewrite failed for %s: %s", orig_url, exc)

    # ── Write manifest ───────────────────────────────────────────────────────
    _t_end = time.monotonic()
    load_duration_ms = round((_t_loaded - _t_start) * 1000) if _t_loaded else None
    total_duration_ms = round((_t_end - _t_start) * 1000)

    # Capture browser version for reproducibility
    browser_version = None
    try:
        browser_version = browser.version
    except Exception:
        pass

    (page_dir / "manifest.json").write_text(
        json.dumps({
            "page_url": page_url,
            "mirror_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "browser": f"Chromium {browser_version}" if browser_version else "Chromium (unknown)",
            "viewports": [_DESKTOP_VP, _MOBILE_VP],
            "page_load_ms": load_duration_ms,
            "total_duration_ms": total_duration_ms,
            "total_assets": len(manifest),
            "total_bytes": sum(e["bytes"] for e in manifest),
            "failed_asset_urls": failed_urls[:20],
            "assets": manifest,
        }, indent=2),
        encoding="utf-8",
    )

    logger.info("  ✓ %s  →  %d assets", page_url, len(manifest))
    return {
        "url":          page_url,
        "status":       "ok",
        "error":        None,
        "assets_saved": len(manifest),
    }


# ── Orchestrator ─────────────────────────────────────────────────────────────

def load_input(path: str) -> list[dict]:
    """Parse the JSONL and return [{domain, pages:[url,...]}]."""
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            domain = row.get("domain", "")
            pages_raw = row.get("cwv_top10_pages", [])
            if isinstance(pages_raw, str):
                pages_raw = json.loads(pages_raw)
            pages = [p["url"] for p in pages_raw if p.get("url")]
            if domain and pages:
                entries.append({"domain": domain, "pages": pages})
    return entries


def run(input_file: str, output_dir: str, workers: int) -> None:
    entries = load_input(input_file)
    logger.info("Loaded %d domains from %s", len(entries), input_file)

    tasks: list[tuple[str, str, Path]] = []   # (domain, page_url, page_dir)
    for entry in entries:
        d_slug = _domain_slug(entry["domain"])
        for page_url in entry["pages"]:
            p_slug = _page_slug(page_url)
            page_dir = Path(output_dir) / d_slug / p_slug
            # Skip already completed pages
            if (page_dir / "index.html").exists():
                logger.info("Skip (done): %s", page_url)
                continue
            tasks.append((entry["domain"], page_url, page_dir))

    logger.info("%d pages to fetch  (%d workers)", len(tasks), workers)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        try:
            from tqdm import tqdm
            fut_iter_wrap = lambda it, total: tqdm(it, total=total, unit="page")
        except ImportError:
            fut_iter_wrap = lambda it, total: it

        futures = {
            ex.submit(fetch_page, page_url, page_dir): (domain, page_url)
            for domain, page_url, page_dir in tasks
        }
        for fut in fut_iter_wrap(as_completed(futures), len(futures)):
            domain, page_url = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"url": page_url, "status": "error", "error": str(e), "assets_saved": 0}
                logger.error("FAIL %s: %s", page_url, e)
            res["domain"] = domain
            results.append(res)

    ok    = sum(1 for r in results if r["status"] == "ok")
    fails = len(results) - ok
    total_assets = sum(r.get("assets_saved", 0) for r in results)
    logger.info("Done. %d ok  |  %d failed  |  %d total assets saved", ok, fails, total_assets)

    # Write a top-level summary
    summary_path = Path(output_dir) / "fetch_summary.json"
    summary_path.write_text(json.dumps(results, indent=2))
    logger.info("Summary → %s", summary_path)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input",   default=DEFAULT_INPUT,   help="Input JSONL file")
    p.add_argument("--output",  default=DEFAULT_OUTPUT,  help="Output root directory")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help="Parallel Playwright pages (default 3, keep ≤ 5)")
    args = p.parse_args()

    Path(args.output).mkdir(parents=True, exist_ok=True)
    run(args.input, args.output, args.workers)


if __name__ == "__main__":
    main()
