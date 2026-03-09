#!/usr/bin/env python3
"""
check_minified_fast.py

Lightweight minification detector.
Two signals, zero extra HTTP calls beyond the homepage fetch:

  1. Minified URL pattern  — .min.js/.min.css, content-hashed filenames, .bundle/.chunk
  2. Bundler HTML fingerprint — __webpack_require__, /_next/, /__vite__, etc. in page HTML

Usage:
    # Run against EDSSites JSONL (checks every page URL per domain):
    python check_minified_fast.py --jsonl EDSSites_CWV_joined_top50_pages_top10.jsonl --output results.jsonl

    # Tranco CSV:
    python check_minified_fast.py --input tranco_top-1m.csv --top 50000 --output results.jsonl

    # Ad-hoc domains:
    python check_minified_fast.py --domains example.com foo.io --output results.jsonl

Outputs one JSON line per URL checked:
    {"domain": "https://worldbank.org", "page_url": "https://www.worldbank.org/en/home",
     "is_minified": true, "signal": "minified_url", "status": "ok", "error": null}
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

# ── Tuning ──────────────────────────────────────────────────────────────────
DEFAULT_WORKERS  = 40
DEFAULT_TIMEOUT  = 8    # seconds per request
DEFAULT_TOP_N    = 10_000
DEFAULT_OUTPUT   = "minified_check_results.jsonl"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 MinCheck/1.0"
    )
}

# ── Signal 1: Minified asset URL patterns ───────────────────────────────────
# Matches: foo.min.js, foo.min.css, foo-min.js, foo.bundle.js,
#          foo.chunk.js, foo.vendor.js, foo.production.js
# NOTE: Content-hash patterns removed — mirroring process no longer adds hashes
_MINIFIED_URL_RE = re.compile(
    r"\.min\.(js|css)"                          # standard: .min.js / .min.css
    r"|-(min|bundle|chunk|vendor|prod)\.(js|css)"
    r"|\.(bundle|chunk|vendor|production)\.(js|css)",
    re.IGNORECASE,
)

# ── Vendored/third-party library paths (served from same origin) ─────────────
# These are pre-minified libraries, CMS infrastructure files, analytics SDKs,
# and other third-party code hosted on the site's own domain.
# Their .min.js filenames or bundled content do NOT mean the site itself ships
# minified code.
_VENDORED_LIB_RE = re.compile(
    # ── AEM / Adobe Experience Manager infrastructure ─────────────────────
    r"/etc\.clientlibs/"          # AEM clientlib proxy path
    r"|/etc/clientlibs/"          # AEM clientlib direct path
    r"|/etc/designs/"             # AEM design path (clientlib JS/CSS)
    r"|/clientlibs[\w.-]*/"       # generic AEM clientlib directories

    # ── Common JS libraries ───────────────────────────────────────────────
    r"|jquery[\w.-]*\.min\."
    r"|jquery[\w.-]*\.slim\."
    r"|/alloy\.js"                # Adobe Alloy SDK (bundled, not always .min)
    r"|alloy\.min\."
    r"|moment[\w.-]*\.min\."
    r"|lodash[\w.-]*\.min\."
    r"|underscore[\w.-]*\.min\."
    r"|backbone[\w.-]*\.min\."
    r"|handlebars[\w.-]*\.min\."
    r"|mustache[\w.-]*\.min\."
    r"|axios[\w.-]*\.min\."
    r"|rxjs[\w.-]*\.min\."
    r"|gsap[\w.-]*\.min\."
    r"|three[\w.-]*\.min\."
    r"|pixi[\w.-]*\.min\."
    r"|socket\.io[\w.-]*\.min\."
    r"|marked[\w.-]*\.min\."
    r"|highlight[\w.-]*\.min\."
    r"|prism[\w.-]*\.min\."
    r"|chart[\w.-]*\.min\."
    r"|echarts[\w.-]*\.min\."
    r"|leaflet[\w.-]*\.min\."
    r"|mapbox[\w.-]*\.min\."
    r"|swiper[\w.-]*\.min\."
    r"|slick[\w.-]*\.min\."
    r"|splide[\w.-]*\.min\."
    r"|lazysizes[\w.-]*\.min\."
    r"|video[\w.-]*\.min\."       # VideoJS and similar video players
    r"|video-js[\w.-]*\.min\."    # VideoJS explicitly
    r"|adobe-client-data-layer[\w.-]*\.min\." # Adobe Client Data Layer

    # ── UI / CSS frameworks ───────────────────────────────────────────────
    r"|bootstrap[\w.-]*\.min\."
    r"|popper[\w.-]*\.min\."
    r"|foundation[\w.-]*\.min\."
    r"|bulma[\w.-]*\.min\."
    r"|tailwind[\w.-]*\.min\."
    r"|materialize[\w.-]*\.min\."
    r"|uikit[\w.-]*\.min\."
    r"|semantic[\w.-]*\.min\."

    # ── JS frameworks (pre-built UMD/CJS bundles) ─────────────────────────
    r"|react[\w.-]*\.min\."
    r"|react-dom[\w.-]*\.min\."
    r"|preact[\w.-]*\.min\."
    r"|angular[\w.-]*\.min\."
    r"|vue[\w.-]*\.min\."
    r"|svelte[\w.-]*\.min\."
    r"|ember[\w.-]*\.min\."
    r"|mithril[\w.-]*\.min\."
    r"|alpine[\w.-]*\.min\."
    r"|htmx[\w.-]*\.min\."
    r"|d3[\w.-]*\.min\."

    # ── Analytics / tracking / marketing ──────────────────────────────────
    r"|analytics[\w.-]*\.min\."
    r"|gtm[\w.-]*\.min\."
    r"|gtag[\w.-]*\.min\."
    r"|ga[\w.-]*\.min\."
    r"|martech/"
    r"|/launch-[\w]+\.min\."      # Adobe Launch
    r"|OneTrust[\w.-]*\.min\."
    r"|cookieconsent[\w.-]*\.min\."
    r"|onetrust/"
    r"|scevent[\w.-]*\.min\."     # Adobe Scene7 events
    r"|exponea[\w.-]*\.min\."     # Exponea analytics
    r"|6si[\w.-]*\.min\."         # 6sense B2B tracking
    r"|phenomtrack[\w.-]*\.min\." # Phenom tracking
    r"|did-[\w]+\.min\."          # Device ID tracking scripts
    r"|new-relic[\w.-]*\.min\."   # New Relic monitoring
    r"|eds-new-relic[\w.-]*\.min\." # EDS New Relic wrapper
    r"|insight[\w.-]*\.min\."     # Various insight/analytics scripts
    r"|iframeResizer[\w.-]*\.min\." # iframe resizer lib
    r"|scroll-proxy[\w.-]*\.min\." # Scroll proxy lib

    # ── Payment / chat / widget SDKs ──────────────────────────────────────
    r"|stripe[\w.-]*\.min\."
    r"|paypal[\w.-]*\.min\."
    r"|recaptcha[\w.-]*\.min\."
    r"|grecaptcha"
    r"|intercom[\w.-]*\.min\."
    r"|zendesk[\w.-]*\.min\."
    r"|drift[\w.-]*\.min\."
    r"|chatbot[\w.-]*\.min\."
    r"|livechat[\w.-]*\.min\."
    r"|tawk[\w.-]*\.min\."

    # ── Polyfills ─────────────────────────────────────────────────────────
    r"|polyfill[\w.-]*\.min\."
    r"|core-js[\w.-]*\.min\."
    r"|regenerator[\w.-]*\.min\."
    r"|webcomponents[\w.-]*\.min\."
    r"|babel[\w.-]*\.min\."
    r"|es[56]-shim[\w.-]*\.min\."

    # ── Infrastructure directories ────────────────────────────────────────
    r"|/plugins/"
    r"|/vendor/"
    r"|/vendors/"
    r"|/libs/"
    r"|/lib/"
    r"|/third[_-]?party/"
    r"|/external/"
    r"|/node_modules/"
    r"|/bower_components/"
    r"|/static/lib/"
    r"|/static/vendor/"
    r"|/assets/vendor/"
    r"|/assets/lib/"
    r"|/dist/"                    # Common webpack/rollup output directory
    r"|/cdn-cgi/"                 # Cloudflare injected scripts
    r"|/_next/"                   # Next.js bundled assets (not EDS)
    r"|/_nuxt/"                   # Nuxt.js bundled assets (not EDS)
    r"|/magepack/"                # Magento magepack bundles (not EDS)

    # ── Third-party CSS / icon fonts ──────────────────────────────────────
    r"|fontawesome[\w.-]*\."      # FontAwesome (any variant)
    r"|font-awesome[\w.-]*\."
    r"|vcdk[\w.-]*\.css"          # Visual Component Design Kit

    # ── Bot-protection middleware (served on-domain but third-party authored) ──
    # PerimeterX injects /<AppId>/init.js and /<AppId>/captcha/* as relative paths.
    # AppId is 8 alphanumeric chars (e.g. 7Rns1bjJ) used as the path directory name.
    r"|/[0-9A-Za-z]{8}/init\.js"
    r"|/[0-9A-Za-z]{8}/captcha/"
    r"|/[0-9A-Za-z]{8}/xhr",
    re.IGNORECASE,
)


def _is_vendored_library(asset_url: str) -> bool:
    """Return True if the asset URL looks like a vendored/third-party library."""
    return bool(_VENDORED_LIB_RE.search(asset_url))


# ── Signal 2: Bundler fingerprints in page HTML ──────────────────────────────
# Detectable without fetching any external file — present in HTML itself.
_BUNDLER_HTML_RE = re.compile(
    r"__webpack_require__"
    r"|webpackJsonp"
    r"|__webpack_modules__"
    r"|/_next/static"          # Next.js
    r"|__NEXT_DATA__"
    r"|/_nuxt/"                # Nuxt
    r"|/__vite__"              # Vite
    r"|vite/dist/client"
    r"|/_app/immutable"        # SvelteKit
    r"|__sveltekit"
    r"|/gatsby-"               # Gatsby
    r"|___gatsby"
    r"|__remixContext"         # Remix
    r"|parcelRequire"          # Parcel
)

# Limit how much of the HTML body we read for the fingerprint scan
_HTML_SCAN_BYTES = 50_000   # first 50 KB is more than enough

# ── Signal 3: Code-density analysis ──────────────────────────────────────────
# Ported from find_non_minified_sites.py — provides ground-truth detection by
# actually downloading and analysing same-origin JS/CSS file content.
_MAX_CODE_FILES    = 8        # max JS files to fetch per page (was 3)
_MAX_CSS_FILES     = 4        # max CSS files to fetch per page
_MAX_CODE_BYTES    = 200_000  # read cap per file (was 100 KB)
_AVG_LINE_THRESH   = 500      # avg chars/line above this → minified
_WHITESPACE_THRESH = 0.10     # whitespace ratio below this → minified

_MINIFIED_SYNTAX_PATTERNS = [
    r"\{return[^\s]",   # {return( — no space after keyword
    r";var[^\s]",       # ;var — no newline before var
    r";function[^\s]",  # ;function — no newline
    r"\}else\{",        # }else{ — no spaces
    r"\)\{",            # ){ — no space before brace
    r"\}catch\(",       # }catch( — no space
    r"\}finally\{",     # }finally{ — no space
    r",function\(",     # ,function( — no space
]
_MINIFIED_SYNTAX_RE = [re.compile(p) for p in _MINIFIED_SYNTAX_PATTERNS]


def _has_minified_syntax(code: str) -> bool:
    """Check if code sample contains minified syntax patterns."""
    sample = code[:3000]
    return any(pat.search(sample) for pat in _MINIFIED_SYNTAX_RE)


def _comment_ratio(code: str) -> float:
    """Fraction of code that is comments (// and /* */)."""
    if not code:
        return 0.0
    chars = sum(len(m.group(0)) for m in re.finditer(r"(//[^\n]*|/\*.*?\*/)", code, re.DOTALL))
    return chars / len(code)


def _whitespace_ratio(code: str) -> float:
    """Fraction of code that is whitespace."""
    if not code:
        return 0.0
    return sum(1 for c in code if c in " \t\n\r") / len(code)


def _check_code_density(
    js_urls: list[str],
    page_url: str,
    timeout: int,
    mirror_dir: "Path | None" = None,
) -> tuple[str | None, list[dict]]:
    """Fetch up to _MAX_CODE_FILES same-origin JS files and analyse content.

    Reads from *mirror_dir* first (no network); falls back to live HTTP only
    if the file is not found locally.

    Returns:
        (signal_str | None, per_asset_metrics_list)
    """
    checked = 0
    asset_metrics: list[dict] = []
    signal_found: str | None = None

    for asset_url in js_urls:
        if checked >= _MAX_CODE_FILES:
            break
        if not asset_url.lower().endswith(".js"):
            continue

        code: str | None = None
        source = "live"

        # ── Try local mirror first ────────────────────────────────────────
        if mirror_dir is not None:
            # asset_url is relative like 'assets/js/foo.js' or '/assets/js/foo.js'
            rel = asset_url.lstrip("/")
            local_path = mirror_dir / rel
            if local_path.exists():
                try:
                    code = local_path.read_text(errors="replace")
                    source = "local"
                except Exception:
                    pass

        # ── Fallback to live HTTP ─────────────────────────────────────────
        if code is None:
            full_url = urljoin(page_url, asset_url)
            try:
                r = requests.get(full_url, timeout=timeout, verify=False,
                                 headers=_HEADERS, stream=True)
                if r.status_code != 200:
                    continue
                code = r.raw.read(_MAX_CODE_BYTES, decode_content=True).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                continue

        if not code or len(code) < 500:
            continue
        checked += 1

        lines = code.split("\n")
        avg_line_len = len(code) / max(len(lines), 1)
        ws_ratio = _whitespace_ratio(code)
        cm_ratio = _comment_ratio(code)

        triggered = False
        file_signal: str | None = None
        if avg_line_len > _AVG_LINE_THRESH:
            file_signal = "code_density:avg_line_length"
            triggered = True
        elif _has_minified_syntax(code) and ws_ratio < _WHITESPACE_THRESH:
            file_signal = "code_density:syntax+low_whitespace"
            triggered = True
        elif ws_ratio < 0.05 and cm_ratio == 0.0:
            file_signal = "code_density:no_whitespace_no_comments"
            triggered = True

        asset_metrics.append({
            "url":           asset_url,
            "source":        source,
            "size_bytes":    len(code),
            "lines":         len(lines),
            "avg_line_len":  round(avg_line_len, 1),
            "ws_ratio":      round(ws_ratio, 4),
            "comment_ratio": round(cm_ratio, 4),
            "triggered":     triggered,
        })

        if triggered and signal_found is None:
            signal_found = file_signal

    return signal_found, asset_metrics


def _check_css_density(
    css_urls: list[str],
    page_url: str,
    timeout: int,
    mirror_dir: "Path | None" = None,
) -> tuple[bool, list[dict]]:
    """Fetch up to _MAX_CSS_FILES same-origin CSS files and check for minification.

    Reads from *mirror_dir* first (no network); falls back to live HTTP only
    if the file is not found locally.

    Returns:
        (any_minified: bool, per_asset_metrics_list)
    """
    checked = 0
    asset_metrics: list[dict] = []
    any_minified = False

    for asset_url in css_urls:
        if checked >= _MAX_CSS_FILES:
            break
        al = asset_url.lower()
        if not al.endswith(".css"):
            continue
        url_minified = bool(re.search(r'\.min\.css', al))

        code: str | None = None
        source = "live"

        # ── Try local mirror first ────────────────────────────────────────
        if mirror_dir is not None:
            rel = asset_url.lstrip("/")
            local_path = mirror_dir / rel
            if local_path.exists():
                try:
                    code = local_path.read_text(errors="replace")
                    source = "local"
                except Exception:
                    pass

        # ── Fallback to live HTTP ─────────────────────────────────────────
        if code is None:
            full_url = urljoin(page_url, asset_url)
            try:
                r = requests.get(full_url, timeout=timeout, verify=False,
                                 headers=_HEADERS, stream=True)
                if r.status_code != 200:
                    continue
                code = r.raw.read(_MAX_CODE_BYTES, decode_content=True).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                continue

        if not code or len(code) < 200:
            continue
        checked += 1

        lines = code.split("\n")
        avg_line_len = len(code) / max(len(lines), 1)
        ws_ratio = _whitespace_ratio(code)

        # CSS has naturally lower whitespace than JS, so use stricter threshold.
        # For CSS: require BOTH high avg_line_len AND very low whitespace,
        # or explicitly minified URL pattern.
        css_whitespace_thresh = 0.06  # stricter than JS threshold
        content_minified = (avg_line_len > _AVG_LINE_THRESH) or \
                          (avg_line_len > 80 and ws_ratio < css_whitespace_thresh)
        triggered = url_minified or content_minified

        asset_metrics.append({
            "url":              asset_url,
            "source":           source,
            "size_bytes":       len(code),
            "lines":            len(lines),
            "avg_line_len":     round(avg_line_len, 1),
            "ws_ratio":         round(ws_ratio, 4),
            "url_minified":     url_minified,
            "content_minified": content_minified,
            "triggered":        triggered,
        })

        if triggered:
            any_minified = True

    return any_minified, asset_metrics


def _is_same_origin(asset_url: str, page_url: str) -> bool:
    """Return True if asset_url is same-origin relative to page_url.

    Accepts:
      - Relative URLs  (/scripts/foo.js, assets/bar.css)
      - Same-domain absolute URLs (https://www.example.com/foo.js when page is example.com)
    Rejects:
      - Third-party CDN / analytics scripts (assets.adobedtm.com, cdnjs.cloudflare.com, etc.)
    """
    parsed_asset = urlparse(asset_url)

    # Relative URL (no scheme + no netloc) → always same-origin
    if not parsed_asset.scheme and not parsed_asset.netloc:
        return True
    # Protocol-relative URL (//host/path) has netloc but no scheme
    # Absolute URL has both scheme and netloc
    asset_host = parsed_asset.netloc.lower().removeprefix("www.")
    if not asset_host:
        return True  # edge case: treat as relative

    page_host = urlparse(page_url).netloc.lower().removeprefix("www.")
    # Match if the asset host ends with the page's domain
    # e.g. asset on cdn.worldbank.org matches page on worldbank.org
    return asset_host == page_host or asset_host.endswith("." + page_host)


def check_domain(domain: str, timeout: int = DEFAULT_TIMEOUT,
                 html_content: str | None = None,
                 mirror_dir: "Path | None" = None) -> dict:
    """Apply all minification signals to a page.

    If html_content is provided, analyse it directly (local mirror mode —
    no network access required for Signals 1 & 2).  Otherwise fetch the
    live URL.

    Returns a dict with keys: domain, is_minified, signal, matched_urls,
    status, error.
    """
    result = {
        "domain":       domain,
        "is_minified":  False,
        "signal":       None,
        "matched_urls": [],
        "status":       "ok",
        "error":        None,
        # ── Rich code metrics (populated by Signal 3) ─────────────────────
        "js_files_checked":  0,
        "css_files_checked": 0,
        "js_metrics":  [],   # per-file: url, size_bytes, lines, avg_line_len, ws_ratio, etc.
        "css_metrics": [],   # per-file: url, size_bytes, lines, avg_line_len, ws_ratio, etc.
    }

    url = domain if domain.startswith("http") else f"https://{domain}"

    # ── Map rewritten relative URLs back to their true live origins ────────
    orig_url_map = {}
    if mirror_dir is not None:
        manifest_path = mirror_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                for asset in manifest_data.get("assets", []):
                    # e.g. "assets/js/insight.min.js" -> "https://snap.licdn.com/..."
                    local_p = asset.get("local_path", "")
                    orig_u = asset.get("original_url", "")
                    if local_p and orig_u:
                        orig_url_map[local_p] = orig_u
                        # Also add with leading slash to be safe
                        orig_url_map["/" + local_p] = orig_u
            except Exception:
                pass

    def _apply_signals(html_chunk: str, page_url: str) -> None:
        """Shared logic for all three signals — mutates `result`."""
        asset_re = re.compile(
            r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|css)[^"']*)["']""",
            re.IGNORECASE,
        )
        
        raw_assets = asset_re.findall(html_chunk)
        same_origin_assets = []
        for m in raw_assets:
            # Drop query params for lookup
            base_m = m.split("?")[0].split("#")[0]
            # Use original URL if we mirrored it, else use m
            true_url = orig_url_map.get(base_m, m)
            
            if _is_same_origin(true_url, page_url) and not _is_vendored_library(true_url):
                # keep the original relative path (m) for the subsequent logic,
                # so that mirror_dir / rel_path continues to work in density checks.
                same_origin_assets.append(m)

        # Signal 1: minified URL patterns
        url_matches = [m for m in same_origin_assets if _MINIFIED_URL_RE.search(m)]
        has_minified_url = len(url_matches) > 0

        # Signal 2: bundler fingerprint in HTML
        has_bundler_html = bool(_BUNDLER_HTML_RE.search(html_chunk))

        if has_minified_url and has_bundler_html:
            result["is_minified"] = True
            result["signal"]      = "both"
            result["matched_urls"] = url_matches
        elif has_minified_url:
            result["is_minified"] = True
            result["signal"]      = "minified_url"
            result["matched_urls"] = url_matches
        elif has_bundler_html:
            result["is_minified"] = True
            result["signal"]      = "bundler_fingerprint"

        # Signal 3: code-density on JS — only when signals 1+2 didn't fire
        if not result["is_minified"] and same_origin_assets:
            js_assets = [u for u in same_origin_assets if u.lower().endswith(".js")]
            density_signal, js_metrics = _check_code_density(
                js_assets, page_url, timeout, mirror_dir=mirror_dir
            )
            result["js_metrics"] = js_metrics
            result["js_files_checked"] = len(js_metrics)
            if density_signal:
                result["is_minified"] = True
                result["signal"]      = density_signal
                triggered = [m["url"] for m in js_metrics if m.get("triggered")]
                result["matched_urls"] = triggered or same_origin_assets[:_MAX_CODE_FILES]

        # CSS density check (always run for auditing, regardless of JS result)
        css_assets = [u for u in same_origin_assets if u.lower().endswith(".css")]
        if css_assets:
            css_minified, css_metrics = _check_css_density(
                css_assets, page_url, timeout, mirror_dir=mirror_dir
            )
            result["css_metrics"] = css_metrics
            result["css_files_checked"] = len(css_metrics)
            if css_minified and not result["is_minified"]:
                result["is_minified"] = True
                result["signal"]      = "css_density"
                result["matched_urls"] = [m["url"] for m in css_metrics if m.get("triggered")]

    # ── Local mirror mode: no network needed for signals 1+2 ─────────────
    if html_content is not None:
        _apply_signals(html_content[:_HTML_SCAN_BYTES], url)
        return result

    # ── Live mode: fetch the page ────────────────────────────────────────
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            verify=False,
            allow_redirects=True,
            headers=_HEADERS,
            stream=True,
        )
        resp.raise_for_status()
        html_chunk = resp.raw.read(_HTML_SCAN_BYTES, decode_content=True).decode(
            "utf-8", errors="replace"
        )
        _apply_signals(html_chunk, url)

    except requests.exceptions.Timeout:
        result["status"] = "timeout"
        result["error"]  = "timeout"
    except requests.exceptions.SSLError:
        if url.startswith("https://"):
            fallback = url.replace("https://", "http://", 1)
            try:
                resp = requests.get(fallback, timeout=timeout, verify=False,
                                    allow_redirects=True, headers=_HEADERS, stream=True)
                resp.raise_for_status()
                html_chunk = resp.raw.read(_HTML_SCAN_BYTES, decode_content=True).decode(
                    "utf-8", errors="replace"
                )
                _apply_signals(html_chunk, fallback)
            except Exception as e:
                result["status"] = "ssl_error"; result["error"] = str(e)
        else:
            result["status"] = "ssl_error"; result["error"] = "SSL error"
    except requests.exceptions.ConnectionError as e:
        result["status"] = "connection_error"; result["error"] = str(e)[:120]
    except Exception as e:
        result["status"] = "error"; result["error"] = str(e)[:120]

    return result


def _load_domains_from_csv(path: str, top_n: int) -> list[tuple[int, str]]:
    domains = []
    with open(path) as f:
        for rank, row in enumerate(csv.reader(f), 1):
            if rank > top_n:
                break
            if row:
                domains.append((rank, row[1] if len(row) > 1 else row[0]))
    return domains


def _mirror_domain_slug(domain_url: str) -> str:
    """Match fetch_live_assets.py _domain_slug exactly."""
    parsed = urlparse(domain_url)
    host = (parsed.netloc or parsed.path).rstrip("/")
    if host.startswith("www."):
        host = host[4:]
    return host


def _mirror_page_slug(page_url: str) -> str:
    """Match fetch_live_assets.py _page_slug exactly."""
    parsed = urlparse(page_url)
    path = parsed.path.strip("/") or "home"
    path = re.sub(r"[^\w.\-/]", "_", path).replace("/", "__")
    if parsed.query:
        path += "__" + hashlib.md5(parsed.query.encode()).hexdigest()[:6]
    return path[:100] or "home"


def _load_from_jsonl(path: str) -> list[tuple[str, str]]:
    """Load (domain, page_url) pairs from EDSSites JSONL.

    Returns a flat list of (domain, page_url) — one entry per page URL
    across all domains.
    """
    pairs: list[tuple[str, str]] = []
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
            for page in pages_raw:
                url = page.get("url", "")
                if url:
                    pairs.append((domain, url))
    return pairs


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--jsonl",   help="EDSSites JSONL file — checks every page URL per domain")
    src.add_argument("--input",   help="Tranco CSV file (rank,domain)")
    src.add_argument("--domains", nargs="+", metavar="DOMAIN", help="Domain(s) to check directly")
    p.add_argument("--top",      type=int, default=DEFAULT_TOP_N, help="Top N rows from Tranco CSV")
    p.add_argument("--output",   default=DEFAULT_OUTPUT,           help="Output JSONL path")
    p.add_argument("--workers",  type=int, default=DEFAULT_WORKERS, help="Parallel workers")
    p.add_argument("--timeout",  type=int, default=DEFAULT_TIMEOUT, help="Request timeout (s)")
    p.add_argument("--resume",   action="store_true",               help="Skip already-written URLs")
    p.add_argument("--mirrors",  default=None,
                   help="Path to mirrors root (e.g. live_assets_eds). "
                        "When set, checks local index.html instead of live pages")
    args = p.parse_args()

    # ── Build task list ───────────────────────────────────────────────────────
    # Each task is (domain_label, page_url).  For CSV/domains mode we use the
    # domain as its own page_url (homepage check only).
    if args.jsonl:
        # [(domain, page_url), ...] — one entry per subpage
        tasks: list[tuple[str, str]] = _load_from_jsonl(args.jsonl)
        logger.info("Loaded %d page URLs from %s", len(tasks), args.jsonl)
    elif args.input:
        csv_domains = _load_domains_from_csv(args.input, args.top)
        tasks = [(d, d) for _, d in csv_domains]
    else:
        tasks = [(d, d) for d in args.domains]

    # ── Resume: skip already-written page_urls ────────────────────────────────
    done: set[str] = set()
    if args.resume and Path(args.output).exists():
        with open(args.output) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    # Support both old (domain key) and new (page_url key) outputs
                    done.add(obj.get("page_url") or obj.get("domain", ""))
                except Exception:
                    pass
        logger.info("Resuming: skipping %d already processed URLs", len(done))
    tasks = [(dom, url) for dom, url in tasks if url not in done]

    logger.info("Checking %d URLs with %d workers (timeout=%ds)",
                len(tasks), args.workers, args.timeout)

    total = minified = non_minified = errors = 0
    mode = "a" if (args.resume and Path(args.output).exists()) else "w"

    with open(args.output, mode) as fout:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            # When --mirrors is set, load local index.html for each page
            mirrors_root = Path(args.mirrors) if args.mirrors else None

            futures = {}
            for domain, page_url in tasks:
                html_content = None
                if mirrors_root:
                    d_slug = _mirror_domain_slug(domain)
                    p_slug = _mirror_page_slug(page_url)
                    index_path = mirrors_root / d_slug / p_slug / "index.html"
                    if index_path.exists():
                        try:
                            html_content = index_path.read_text(
                                encoding="utf-8", errors="replace"
                            )
                        except Exception:
                            pass
                    else:
                        logger.debug("Mirror not found: %s", index_path)

                fut = ex.submit(check_domain, page_url, args.timeout,
                                html_content=html_content,
                                mirror_dir=(mirrors_root / d_slug / p_slug)
                                           if mirrors_root else None)
                futures[fut] = (domain, page_url)
            try:
                from tqdm import tqdm
                fut_iter = tqdm(as_completed(futures), total=len(futures), unit="url")
            except ImportError:
                fut_iter = as_completed(futures)

            for fut in fut_iter:
                domain, page_url = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    result = {"domain": page_url, "is_minified": False,
                              "signal": None, "matched_urls": [],
                              "status": "error", "error": str(e)}

                # Enrich with domain label + explicit page_url
                result["domain"]   = domain
                result["page_url"] = page_url

                fout.write(json.dumps(result) + "\n")
                fout.flush()

                total += 1
                if result["status"] not in ("ok", "timeout", "connection_error"):
                    errors += 1
                elif result["is_minified"]:
                    minified += 1
                else:
                    non_minified += 1

    logger.info("Done. %d total | %d minified | %d non-minified | %d errors",
                total, minified, non_minified, errors)
    logger.info("Results written to %s", args.output)


if __name__ == "__main__":
    main()
