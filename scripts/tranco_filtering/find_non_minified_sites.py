#!/usr/bin/env python3
"""
Non-Minified Site Finder

Scans Tranco top sites to find websites that don't use minified code.
Each domain gets a full analysis (homepage + sampled subsites from sitemap),
producing a single JSONL record per domain.

Usage:
    python find_non_minified_sites.py --top 10000 --output results.jsonl --workers 20
    python find_non_minified_sites.py --top 500 --max-subsites 10 --output results.jsonl
"""

import argparse
import csv
import json
import logging
import random
import re
import time
import warnings
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
import tldextract
from bs4 import BeautifulSoup
from tqdm import tqdm
from urllib3.exceptions import InsecureRequestWarning

warnings.filterwarnings("ignore", category=InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Non-Minified-Scanner/2.0"
)
_HEADERS = {"User-Agent": _USER_AGENT}

# ══════════════════════════════════════════════════════════════════════════
# CONFIG — DETERMINISTIC
# These are stable engineering limits and infrastructure constants.
# They don't affect scoring accuracy and don't need calibration.
# ══════════════════════════════════════════════════════════════════════════

TRANCO_LIST_URL              = "https://tranco-list.eu/top-1m.csv.zip"

# ── Per-page analysis coverage ────────────────────────────────────────────
MAX_INLINE_SCRIPTS           = 20    # max inline <script> tags to analyze per page
MIN_INLINE_SCRIPT_BYTES      = 200   # skip trivially small inline scripts
MAX_EXTERNAL_JS              = 10    # max own-origin JS files to fetch per page
MAX_EXTERNAL_CSS             = 5     # max own-origin CSS files to fetch per page
MAX_RESOURCE_BYTES           = 500_000  # read cap per external file (500 KB)

# ── Sitemap crawl ─────────────────────────────────────────────────────────
SITEMAP_URL_LIMIT            = 500   # max page URLs extracted per domain
# Note: only 1 level deep — child sitemaps are NOT followed.

# ── Domain processing ─────────────────────────────────────────────────────
DEFAULT_MAX_SUBSITES         = 20    # subsites to analyze per domain
SUBSITE_WORKERS              = 10    # parallel threads for subsite analysis

# ── Orchestrator ──────────────────────────────────────────────────────────
DEFAULT_TOP_N                = 10_000
DEFAULT_WORKERS              = 20
DEFAULT_TIMEOUT              = 10    # seconds per HTTP request
DEFAULT_OUTPUT               = "non_minified_sites.jsonl"
DEFAULT_CACHE_DIR            = "./tranco_cache"
DOMAIN_POLL_SLEEP            = 0.02  # politeness sleep between domain completions



# ══════════════════════════════════════════════════════════════════════════
# FRAMEWORK DETECTION
# ══════════════════════════════════════════════════════════════════════════

# Each entry: (display_name, [list_of_regex_patterns_OR_html_strings])
# Patterns are checked against the combined HTML + inline JS text.

_UI_FRAMEWORKS: List[Tuple[str, List[str]]] = [
    ("React",       [r"React\.createElement", r"data-reactroot", r"__react\w", r"_reactFiber"]),
    ("Vue 3",       [r"_createVNode", r"Vue\.createApp"]),
    ("Vue 2",       [r"new Vue\(", r"__vue__"]),
    ("Angular",     [r"ng-version", r"platformBrowserDynamic", r"@angular/"]),
    ("Svelte",      [r"__svelte", r'class="svelte-', r"svelte/internal"]),
    ("Lit",         [r"customElements\.define", r"LitElement", r"lit-html"]),
    ("Alpine.js",   [r"x-data=", r"Alpine\.start", r"x-init="]),
    ("jQuery",      [r"jQuery", r"\$\.fn\.jquery", r"jquery\.js", r"jquery\.min\.js"]),
    ("HTMX",        [r"hx-get=", r"hx-post=", r"htmx\.config", r"htmx\.min\.js"]),
    ("Stimulus",    [r"data-controller=", r"Stimulus\.Application"]),
    ("Preact",      [r"preact", r"h\(Fragment"]),
    ("Solid.js",    [r"solidjs", r"solid-js"]),
    ("Ember",       [r"Ember\.Application", r"ember\.js", r'data-ember-action']),
    ("Backbone",    [r"Backbone\.View", r"backbone\.js"]),
    ("Mithril",     [r"m\.render\(", r"mithril\.js"]),
    ("Qwik",        [r"qwik", r"q:container"]),
]

_META_FRAMEWORKS: List[Tuple[str, List[str]]] = [
    ("Next.js",     [r"__NEXT_DATA__", r"/_next/static"]),
    ("Nuxt.js",     [r"__NUXT__", r"/_nuxt/"]),
    ("SvelteKit",   [r"__sveltekit", r"/_app/immutable"]),
    ("Astro",       [r"astro-island", r"astro:page-load", r"@astrojs"]),
    ("Remix",       [r"__remixContext", r"remix-"]),
    ("Gatsby",      [r"___gatsby", r"/gatsby-"]),
    ("Eleventy",    [r'name="generator" content="Eleventy']),
    ("Hugo",        [r'name="generator" content="Hugo']),
    ("Jekyll",      [r'name="generator" content="Jekyll']),
    ("Docusaurus",  [r"docusaurus", r"__docusaurus"]),
    ("VitePress",   [r"vitepress", r"__vitepress"]),
]

_BUNDLERS: List[Tuple[str, List[str]]] = [
    ("Webpack",     [r"__webpack_require__", r"webpackJsonp", r"__webpack_modules__"]),
    ("Vite",        [r'type="module".*/@vite/', r"__vite__", r"vite/dist/client"]),
    ("Parcel",      [r"parcelRequire"]),
    ("Rollup",      [r"/\* rollup \*/", r"rollup\.js"]),
    ("esbuild",     [r"// esbuild", r"esbuild"]),
    ("Turbopack",   [r"__turbopack"]),
    ("Browserify",  [r"_dereq_\(", r"browserify"]),
    ("RequireJS",   [r"require\.config\(", r"requirejs"]),
]

_CMS: List[Tuple[str, List[str]]] = [
    ("WordPress",     [r"/wp-content/", r"/wp-includes/", r"wp-json"]),
    ("Drupal",        [r"Drupal\.settings", r"/sites/default/files/"]),
    ("Ghost",         [r"ghost-url", r"content\.ghost\.io"]),
    ("Webflow",       [r"webflow\.com/css", r'data-wf-']),
    ("Squarespace",   [r"squarespace\.com", r"\"sqsp\""]),
    ("Wix",           [r"wix\.com", r"wixstatic\.com"]),
    ("Shopify",       [r"cdn\.shopify\.com", r"Shopify\.theme"]),
    ("Contentful",    [r"contentful\.com", r"ctfl-"]),
    ("Sanity",        [r"sanity\.io", r"_sanity"]),
    ("Strapi",        [r"strapi"]),
    ("Joomla",        [r"/joomla/", r"joomla\.js"]),
    ("Magento",       [r"Mage\.Cookies", r"/skin/frontend/"]),
    ("BigCommerce",   [r"bigcommerce\.com", r"bc-sf-filter"]),
]

_CSS_FRAMEWORKS: List[Tuple[str, List[str]]] = [
    ("Tailwind",    [r"tailwindcss", r'class="[^"]*\b(?:flex|grid|p-\d|m-\d|text-\w+)\b']),
    ("Bootstrap",   [r"bootstrap\.min\.css", r"bootstrap\.css", r'class="[^"]*\bbtn\b']),
    ("Bulma",       [r"bulma\.io", r"bulma\.css", r'class="columns"']),
    ("Foundation",  [r"foundation\.min\.css", r"foundation\.css"]),
    ("UIkit",       [r"uikit\.min\.css", r"uikit\.css"]),
    ("Materialize", [r"materialize\.css", r"materialize\.min\.css"]),
    ("Chakra UI",   [r"chakra-ui", r"@chakra-ui"]),
    ("Ant Design",  [r"antd\.css", r"ant-design"]),
    ("Semantic UI", [r"semantic\.min\.css", r"semantic-ui"]),
]


def detect_frameworks(html: str, inline_js: str = "") -> Dict[str, List[str]]:
    """Exhaustively detect frameworks across 5 categories.

    Returns a dict:
        {
          "ui_frameworks": [...],
          "meta_frameworks": [...],
          "bundlers": [...],
          "cms": [...],
          "css_frameworks": [...],
        }
    """
    combined = html + "\n" + inline_js

    def _match(patterns: List[str]) -> bool:
        return any(re.search(p, combined, re.IGNORECASE) for p in patterns)

    return {
        "ui_frameworks":   [name for name, pats in _UI_FRAMEWORKS   if _match(pats)],
        "meta_frameworks": [name for name, pats in _META_FRAMEWORKS  if _match(pats)],
        "bundlers":        [name for name, pats in _BUNDLERS         if _match(pats)],
        "cms":             [name for name, pats in _CMS              if _match(pats)],
        "css_frameworks":  [name for name, pats in _CSS_FRAMEWORKS   if _match(pats)],
    }


# ══════════════════════════════════════════════════════════════════════════
# MINIFICATION DETECTOR
# ══════════════════════════════════════════════════════════════════════════

MINIFIED_URL_RE = re.compile(
    r"\.min\.(js|css)|"
    r"\.(bundle|chunk|vendor)\.(js|css)|"
    r"-min\.(js|css)|"
    r"\.production\.(js|css)|"
    r"[a-f0-9]{8,}\.(js|css)",
    re.IGNORECASE,
)


def _apex_domain(url: str) -> str:
    """Return the apex (registrable) domain using Mozilla's Public Suffix List.

    e.g.  static.example.co.uk  →  example.co.uk
          cdn.example.com        →  example.com
          js.stripe.com          →  stripe.com
    """
    try:
        ext = tldextract.extract(url)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        return ext.domain or ""
    except Exception:
        return ""


def _is_own_asset(asset_url: str, page_url: str) -> bool:
    """Return True if asset_url is served from the same site as page_url.

    Two URLs are considered same-site when they share the same apex domain
    (including subdomains, e.g. static.example.com == example.com).
    Everything else — analytics, ads, payment SDKs, open CDNs — is skipped.
    """
    page_apex  = _apex_domain(page_url)
    asset_apex = _apex_domain(asset_url)
    return bool(page_apex and asset_apex and page_apex == asset_apex)


def _is_minified_url(url: str) -> bool:
    return bool(MINIFIED_URL_RE.search(url))


# ── Code-density metrics ──────────────────────────────────────────────────

def _comment_ratio(code: str) -> float:
    if not code:
        return 0.0
    chars = 0
    for m in re.finditer(r"(//[^\n]*|/\*.*?\*/)", code, re.DOTALL):
        chars += len(m.group(0))
    return chars / len(code)


def _whitespace_ratio(code: str) -> float:
    if not code:
        return 0.0
    return sum(1 for c in code if c in " \t\n\r") / len(code)


def _short_var_ratio(code: str) -> float:
    if not code:
        return 0.0
    pattern = re.compile(
        r"\b(var|let|const)\s+[a-z]\b|"
        r"function\s*\([a-z](,[a-z])*\)|"
        r"\([a-z](,[a-z])*\)\s*=>|"
        r"\b[a-z]\s*[=:]",
        re.IGNORECASE,
    )
    matches = len(pattern.findall(code))
    return min(1.0, (matches / max(len(code) / 1000, 1)) / 10)


def _has_minified_syntax(code: str) -> bool:
    sample = code[:3000]
    patterns = [r"\{return[^\s]", r";var[^\s]", r";function[^\s]",
                r"\}else\{", r"\)\{", r"\}catch\(", r"\}finally\{", r",function\("]
    return any(re.search(p, sample) for p in patterns)


def _has_source_map(code: str) -> bool:
    return bool(re.search(r"sourceMappingURL=", code[-500:], re.IGNORECASE))


def extract_features(sample: str) -> Dict:
    """Extract all raw code-density features from a code sample.

    These are the raw artifacts used to compute a minification score.
    By storing them directly in the output, thresholds and weights can
    be recalibrated offline without re-running the crawler.
    """
    lines = sample.split("\n")
    total = len(sample)
    if total == 0:
        return {
            "byte_length": 0,
            "line_count": 0,
            "avg_line_length": 0,
            "max_line_length": 0,
            "long_lines_gt500": 0,
            "newline_ratio": 0.0,
            "comment_ratio": 0.0,
            "whitespace_ratio": 0.0,
            "short_var_ratio": 0.0,
            "has_minified_syntax": False,
            "has_source_map": False,
        }

    line_lengths = [len(l) for l in lines]
    return {
        "byte_length":        total,
        "line_count":         len(lines),
        "avg_line_length":    round(total / max(len(lines), 1), 1),
        "max_line_length":    max(line_lengths),
        "long_lines_gt500":   sum(1 for l in line_lengths if l > 500),
        "newline_ratio":      round(sample.count("\n") / total, 4),
        "comment_ratio":      round(_comment_ratio(sample), 4),
        "whitespace_ratio":   round(_whitespace_ratio(sample), 4),
        "short_var_ratio":    round(_short_var_ratio(sample), 4),
        "has_minified_syntax": _has_minified_syntax(sample),
        "has_source_map":     _has_source_map(sample),
    }


def extract_code_features(code: str) -> List[Dict]:
    """Analyse the full code string and return a single raw feature dict.

    No sampling — files are already capped at MAX_RESOURCE_BYTES before
    this function is called, so there's no need to subsample.
    Returns a one-element list to keep the output schema consistent with
    the rest of the file_features structure.
    """
    if not code:
        return []
    return [extract_features(code)]


# ── Resource fetching ─────────────────────────────────────────────────────

def fetch_resource(
    url: str,
    page_url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_RESOURCE_BYTES,
) -> Optional[Tuple[str, int]]:
    """Fetch a JS/CSS file if it is a same-origin own asset.

    Returns (text, byte_size) or None.
    Skips:
      - Different apex domain (third-party: analytics, ads, payment SDKs, ...)
      - Known CDN hosts (always treated as third-party)
    """
    if not _is_own_asset(url, page_url):
        return None
    try:
        resp = requests.get(url, timeout=timeout, verify=False, headers=_HEADERS)
        if resp.status_code != 200:
            return None
        text = resp.text[:max_bytes]
        return text, len(resp.content)
    except Exception:
        return None




# ══════════════════════════════════════════════════════════════════════════
# PAGE ANALYSIS  (runs on homepage AND each subsite)
# ══════════════════════════════════════════════════════════════════════════

def analyze_page(url: str, timeout: int = DEFAULT_TIMEOUT) -> Optional[Dict]:
    """Fetch and analyze a single page URL.

    Stores raw features for every JS/CSS file found. No scoring applied.
    Scoring/thresholding is done offline against the stored feature vectors.

    Output keys:
        url, status, error
        has_minified_urls          — deterministic URL pattern match
        minified_files             — own-origin URLs matching .min. pattern
        third_party_assets_skipped — count of cross-origin assets ignored
        total_scripts, total_stylesheets, resources_analyzed
        file_features              — list of per-file raw feature dicts
        frameworks_detected        — categorical framework identifiers
    """
    result: Dict = {
        "url":                        url,
        "status":                     "success",
        "error":                      None,
        "has_minified_urls":          False,
        "minified_files":             [],
        "third_party_assets_skipped": 0,
        "total_scripts":              0,
        "total_stylesheets":          0,
        "resources_analyzed":         0,
        # Raw per-file feature vectors. Use for offline calibration.
        # Each entry: {source, [asset_url|tag_index], byte_size, sections:[{...}]}
        "file_features":              [],
        "frameworks_detected":        {},
    }

    if not url.startswith("http"):
        url = f"https://{url}"
        result["url"] = url

    try:
        resp = requests.get(url, timeout=timeout, verify=False,
                            allow_redirects=True, headers=_HEADERS)
        if resp.status_code != 200:
            result["status"] = "error"
            result["error"] = f"HTTP {resp.status_code}"
            return result

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        scripts     = soup.find_all("script", src=True)
        stylesheets = soup.find_all("link", rel="stylesheet", href=True)
        result["total_scripts"]     = len(scripts)
        result["total_stylesheets"] = len(stylesheets)

        js_urls  = [urljoin(url, s.get("src",  "")) for s in scripts]
        css_urls = [urljoin(url, l.get("href", "")) for l in stylesheets]

        # Deterministic URL pattern check (own-origin only)
        third_party_skipped = 0
        for u in js_urls + css_urls:
            if _is_own_asset(u, url):
                if _is_minified_url(u):
                    result["has_minified_urls"] = True
                    result["minified_files"].append(u)
            else:
                third_party_skipped += 1
        result["third_party_assets_skipped"] = third_party_skipped

        # Inline scripts — raw features per tag, no scoring
        inline_scripts     = soup.find_all("script", src=False)
        inline_js_combined = ""
        for idx, tag in enumerate(inline_scripts[:MAX_INLINE_SCRIPTS]):
            text = tag.string or ""
            if len(text) > MIN_INLINE_SCRIPT_BYTES:
                result["file_features"].append({
                    "source":    "inline",
                    "tag_index": idx,
                    "byte_size": len(text.encode()),
                    "features":  extract_features(text),
                })
                inline_js_combined += text + "\n"

        # External JS — own-origin only, raw features per file
        js_fetched = 0
        for u in js_urls:
            if js_fetched >= MAX_EXTERNAL_JS:
                break
            fetched = fetch_resource(u, url, timeout)
            if fetched:
                code, size = fetched
                result["file_features"].append({
                    "source":    "external_js",
                    "asset_url": u,
                    "byte_size": size,
                    "features":  extract_features(code),
                })
                result["resources_analyzed"] += 1
                js_fetched += 1

        # External CSS — own-origin only, raw features per file
        css_fetched = 0
        for u in css_urls:
            if css_fetched >= MAX_EXTERNAL_CSS:
                break
            fetched = fetch_resource(u, url, timeout)
            if fetched:
                code, size = fetched
                result["file_features"].append({
                    "source":    "external_css",
                    "asset_url": u,
                    "byte_size": size,
                    "features":  extract_features(code),
                })
                result["resources_analyzed"] += 1
                css_fetched += 1

        result["frameworks_detected"] = detect_frameworks(html, inline_js_combined)

    except requests.exceptions.Timeout:
        result["status"] = "timeout"
        result["error"]  = "Timeout"

    except requests.exceptions.SSLError:
        if url.startswith("https://"):
            return analyze_page(url.replace("https://", "http://", 1), timeout)
        result["status"] = "ssl_error"
        result["error"]  = "SSL error"

    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)

    return result



# ══════════════════════════════════════════════════════════════════════════
# SITEMAP
# ══════════════════════════════════════════════════════════════════════════

_SITEMAP_NS = {
    "sm":    "http://www.sitemaps.org/schemas/sitemap/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
    "news":  "http://www.google.com/schemas/sitemap-news/0.9",
}


def _parse_sitemap_xml(content: bytes) -> List[str]:
    """Parse a sitemap XML and return <loc> URLs."""
    urls: List[str] = []
    try:
        root = ET.fromstring(content)
        for elem in root.findall(".//sm:url/sm:loc", _SITEMAP_NS):
            if elem.text:
                urls.append(elem.text.strip())
        # Also handle sitemap index
        for elem in root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS):
            if elem.text:
                urls.append(elem.text.strip())
    except ET.ParseError:
        pass
    return urls


def fetch_sitemap_urls(base_url: str, timeout: int = DEFAULT_TIMEOUT, limit: int = SITEMAP_URL_LIMIT) -> List[str]:
    """Return up to `limit` page URLs from a site's sitemap."""
    if not base_url.startswith("http"):
        base_url = f"https://{base_url}"
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Try robots.txt for Sitemap: directive
    sitemap_candidates: List[str] = []
    try:
        robots = requests.get(urljoin(origin, "/robots.txt"),
                              timeout=timeout, verify=False, headers=_HEADERS)
        if robots.status_code == 200:
            for line in robots.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_candidates.append(line.split(":", 1)[1].strip())
    except Exception:
        pass

    if not sitemap_candidates:
        sitemap_candidates = [
            urljoin(origin, "/sitemap.xml"),
            urljoin(origin, "/sitemap_index.xml"),
            urljoin(origin, "/sitemap-index.xml"),
        ]

    all_urls: List[str] = []
    for sm_url in sitemap_candidates:
        try:
            resp = requests.get(sm_url, timeout=timeout, verify=False, headers=_HEADERS)
            if resp.status_code != 200:
                continue
            found = _parse_sitemap_xml(resp.content)
            # 1 level deep only: take page URLs directly, ignore any child .xml sitemaps
            page_urls = [u for u in found if not u.endswith(".xml")]
            all_urls.extend(page_urls)
            if all_urls:
                break
        except Exception:
            continue

    # Filter: keep same-origin, exclude .xml/.rss
    filtered = [
        u for u in all_urls
        if urlparse(u).netloc == parsed.netloc
        and not u.lower().endswith((".xml", ".rss", ".atom"))
    ]
    # Deduplicate preserving order
    seen: Set[str] = set()
    deduped: List[str] = []
    for u in filtered:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    return deduped[:limit]


# ══════════════════════════════════════════════════════════════════════════
# DOMAIN PROCESSOR
# ══════════════════════════════════════════════════════════════════════════

def process_domain(
    rank: int,
    domain: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_subsites: int = DEFAULT_MAX_SUBSITES,
    subsite_workers: int = SUBSITE_WORKERS,
) -> Dict:
    """Full analysis: homepage + subsites → unified record.

    No scoring verdict is produced here — raw feature data is stored
    for offline calibration.
    """
    logger.debug("Processing #%d: %s", rank, domain)

    homepage     = analyze_page(domain, timeout)
    subsite_urls = fetch_sitemap_urls(domain, timeout, limit=SITEMAP_URL_LIMIT)

    if len(subsite_urls) > max_subsites:
        step = len(subsite_urls) / max_subsites
        subsite_urls = [subsite_urls[int(i * step)] for i in range(max_subsites)]

    subsite_results: List[Dict] = []
    if subsite_urls:
        with ThreadPoolExecutor(max_workers=subsite_workers) as ex:
            futures = {ex.submit(analyze_page, u, timeout): u for u in subsite_urls}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        subsite_results.append(res)
                except Exception as e:
                    logger.debug("Subsite error for %s: %s", futures[fut], e)

    logger.debug("Done #%d: %s  (%d subsites)", rank, domain, len(subsite_results))

    return {
        "rank":               rank,
        "domain":             domain,
        "homepage":           homepage,
        "subsite_urls_found": len(subsite_urls),
        "subsites":           subsite_results,
    }


# ══════════════════════════════════════════════════════════════════════════
# SITE FINDER (orchestrator)
# ══════════════════════════════════════════════════════════════════════════


def download_tranco_list(cache_file: Path) -> Path:
    """Download and cache Tranco top-1M CSV."""
    if cache_file.exists():
        logger.info("📋 Using cached Tranco list: %s", cache_file)
        return cache_file

    logger.info("📥 Downloading Tranco top-1M list…")
    resp = requests.get(TRANCO_LIST_URL, stream=True)
    resp.raise_for_status()

    import io
    import zipfile

    zip_data = io.BytesIO()
    total = int(resp.headers.get("content-length", 0))
    with tqdm(total=total, unit="B", unit_scale=True, desc="Downloading") as pbar:
        for chunk in resp.iter_content(8192):
            zip_data.write(chunk)
            pbar.update(len(chunk))

    zip_data.seek(0)
    with zipfile.ZipFile(zip_data) as zf:
        csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
        cache_file.write_bytes(zf.read(csv_name))

    logger.info("✅ Saved to %s", cache_file)
    return cache_file


def find_non_minified_sites(
    tranco_file: Path,
    top_n: int,
    output_file: Path,
    checkpoint_file: Path,
    workers: int,
    timeout: int,
    max_subsites: int,
) -> int:
    """Scan Tranco list; stream one JSON record per domain to output_file.

    Returns count of non-minified sites found.
    """
    # Load checkpoint
    processed: Set[str] = set()
    if checkpoint_file.exists():
        processed = {
            l.strip()
            for l in checkpoint_file.read_text().splitlines()
            if l.strip()
        }
        logger.info("📋 Resuming: %d domains already done", len(processed))

    # Read Tranco CSV
    domains: List[Tuple[int, str]] = []
    with open(tranco_file) as f:
        for rank, (_, domain) in enumerate(csv.reader(f), 1):
            if rank > top_n:
                break
            if domain not in processed:
                domains.append((rank, domain))

    logger.info(
        "🔍 Checking %d domains (skipped %d already processed)",
        len(domains), len(processed),
    )

    found_count = 0
    append = output_file.exists() and checkpoint_file.exists()

    with (
        open(output_file, "a" if append else "w") as out_f,
        open(checkpoint_file, "a") as ckpt_f,
        ThreadPoolExecutor(max_workers=workers) as executor,
    ):
        futures = {
            executor.submit(
                process_domain, rank, domain, timeout, max_subsites
            ): (rank, domain)
            for rank, domain in domains
        }

        with tqdm(total=len(futures), desc="Scanning sites") as pbar:
            for fut in as_completed(futures):
                rank, domain = futures[fut]
                try:
                    record = fut.result()
                    out_f.write(json.dumps(record) + "\n")
                    out_f.flush()
                    ckpt_f.write(f"{domain}\n")
                    ckpt_f.flush()
                    pass  # no verdict field — scoring done offline
                except Exception as e:
                    logger.error("Error processing %s: %s", domain, e)

                pbar.update(1)
                pbar.set_postfix({"Non-minified": found_count})
                time.sleep(DOMAIN_POLL_SLEEP)

    logger.info("\n✅ Found %d non-minified sites out of %d checked", found_count, top_n)
    return found_count


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find non-minified sites in Tranco top sites",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--top",          type=int, default=DEFAULT_TOP_N,
                        help="Check top N sites from Tranco list")
    parser.add_argument("--output",       default=DEFAULT_OUTPUT,
                        help="Output JSONL path (one record per domain)")
    parser.add_argument("--workers",      type=int, default=DEFAULT_WORKERS,
                        help="Parallel domain workers")
    parser.add_argument("--timeout",      type=int, default=DEFAULT_TIMEOUT,
                        help="HTTP request timeout (seconds)")
    parser.add_argument("--max-subsites", type=int, default=DEFAULT_MAX_SUBSITES,
                        help="Max subsite pages to analyze per domain")
    parser.add_argument("--cache-dir",    default=DEFAULT_CACHE_DIR,
                        help="Cache dir for Tranco CSV + checkpoint")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(exist_ok=True)

    tranco_file  = cache_dir / "top-1m.csv"
    checkpoint   = cache_dir / "checkpoint.txt"
    output_file  = Path(args.output)

    tranco_file  = download_tranco_list(tranco_file)

    found = find_non_minified_sites(
        tranco_file  = tranco_file,
        top_n        = args.top,
        output_file  = output_file,
        checkpoint_file = checkpoint,
        workers      = args.workers,
        timeout      = args.timeout,
        max_subsites = args.max_subsites,
    )

    logger.info("\n%s", "=" * 70)
    logger.info("SUMMARY")
    logger.info("=" * 70)
    logger.info("Checked      : %d sites", args.top)
    logger.info("Non-minified : %d  (%.1f%%)", found, found / args.top * 100)
    logger.info("Output       : %s  (one JSON record per line)", output_file)
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
