#!/usr/bin/env python3
"""
Classify webpages in the cwv-bench-v0 HuggingFace dataset by page type.

Two-pass pipeline:
  Pass 1 (heuristic) — Classify all URLs by URL patterns (fast, no network)
  Pass 2 (VLM)       — For 'content' catch-all pages, screenshot them and
                        ask a VLM to refine the classification

Usage:
    # Heuristic-only (fast, no creds needed):
    python classify_webpages.py --limit 50

    # With VLM refinement for 'content' pages:
    python classify_webpages.py --limit 5 --vlm --max-vlm-calls 20

    # Full run with VLM, resume from checkpoint:
    python classify_webpages.py --vlm --vlm-resume
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse, unquote


# ══════════════════════════════════════════════════════════════════════════
# CONFIG — DETERMINISTIC (dataset, I/O, VLM infrastructure)
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_DATASET              = "behavior-in-the-wild/cwv-bench-v0"
DEFAULT_SPLIT                = "train"
DEFAULT_OUTPUT               = "webpage_classifications.jsonl"
DEFAULT_SCREENSHOT_DIR       = ".cache/screenshots"
DEFAULT_VLM_CHECKPOINT       = "vlm_classification_checkpoint.json"
DEFAULT_MAX_VLM_CALLS        = 100

# ── VLM / Playwright settings ──────────────────────────────────────────────
VLM_MAX_TOKENS               = 200
VLM_SCREENSHOT_TIMEOUT_MS    = 15_000   # ms before page load gives up
VLM_NETWORKIDLE_TIMEOUT_MS   = 5_000    # ms to wait for network idle after load
VLM_CHECKPOINT_SAVE_INTERVAL = 25       # save checkpoint every N VLM calls
VLM_WORKERS                  = 5        # parallel workers for screenshot + VLM
                                        # keep low to respect API rate limits
SCREENSHOT_NAME_MAX_LEN      = 80       # chars of sanitized URL kept in filename
                                        # a short hash suffix is appended for uniqueness

# ── Azure / OpenAI defaults ────────────────────────────────────────────────
DEFAULT_AZURE_API_VERSION    = "2024-02-15-preview"
DEFAULT_AZURE_DEPLOYMENT     = "gpt-4o"


# ══════════════════════════════════════════════════════════════════════════
# CONFIG — URL CLASSIFIER PATTERNS
# All keyword tuples and regexes live here so rules stay separate from logic.
# ══════════════════════════════════════════════════════════════════════════

# Language/locale prefix segments that sites place before the real path.
# ( /en/about → /about,  /fr-fr/blog → /blog )
#
# Derived from pycountry at import time (all ISO 639-1 alpha-2 codes +
# common BCP-47 regional variants like en-gb, pt-br, zh-cn).
# Falls back to a static set if pycountry is not installed.
def _build_lang_prefixes() -> frozenset:
    _STATIC_FALLBACK = frozenset([
        "en", "de", "fr", "es", "it", "pt", "nl", "pl", "ru", "ja",
        "zh", "ko", "ar", "sv", "da", "fi", "nb", "cs", "sk", "hu",
        "ro", "tr", "he", "uk", "ca", "hr", "sr", "bg", "el", "lt",
        "lv", "et", "sl",
        "en-us", "en-gb", "en-au", "en-ca",
        "fr-fr", "fr-be", "fr-ca",
        "de-de", "de-at", "de-ch",
        "es-es", "es-mx", "es-ar",
        "pt-br", "pt-pt",
        "zh-cn", "zh-tw",
        "nl-be",
    ])
    try:
        import pycountry
        # All ISO 639-1 two-letter codes (e.g. "en", "fr", "de" …)
        alpha2 = {
            lang.alpha_2.lower()
            for lang in pycountry.languages
            if hasattr(lang, "alpha_2")
        }
        # Common BCP-47 regional variants: pair each code with every
        # territory that uses it (e.g. en-gb, en-us, fr-be, pt-br …)
        regional = {
            f"{lang.alpha_2.lower()}-{country.alpha_2.lower()}"
            for lang in pycountry.languages
            if hasattr(lang, "alpha_2")
            for country in pycountry.countries
            # Only keep pairs that appear in real locale data
            if f"{lang.alpha_2.lower()}_{country.alpha_2.upper()}" in {
                lc.name for lc in pycountry.languages
            }
        }
        # Simpler: just take the static regional variants on top of the
        # full alpha2 set — pycountry gives us the language codes, the
        # regional pairs are small enough to keep in the static set.
        result = alpha2 | _STATIC_FALLBACK
        return frozenset(result), len(result), "pycountry"
    except ImportError:
        return _STATIC_FALLBACK, len(_STATIC_FALLBACK), "static-fallback"

_LANG_PREFIXES, _lang_prefix_count, _lang_prefix_source = _build_lang_prefixes()


# ── Meta / feed ───────────────────────────────────────────────────────────
KW_FEED_START   = ("feed", "rss", "atom", "sitemap", "robots.txt")
KW_FEED_END     = ("feed", "rss", "atom", "rss.xml", "atom.xml",
                   "feed.xml", "sitemap.xml", "robots.txt")

# ── Auth ──────────────────────────────────────────────────────────────────
KW_AUTH         = ("login", "signin", "sign-in", "signup", "sign-up",
                   "register", "auth", "logout", "sign_in", "sign_up",
                   "oauth", "sso")

# ── Legal ─────────────────────────────────────────────────────────────────
KW_LEGAL        = ("privacy", "privacy-policy", "terms", "terms-of-service",
                   "tos", "legal", "cookie-policy", "cookies", "gdpr",
                   "imprint", "impressum", "disclaimer", "copyright")

# ── Contact ───────────────────────────────────────────────────────────────
KW_CONTACT      = ("contact", "contact-us", "contacto", "kontakt",
                   "reach-us", "get-in-touch")

# ── About ─────────────────────────────────────────────────────────────────
KW_ABOUT        = ("about", "about-us", "about-me", "bio", "team",
                   "our-team", "staff", "ueber-uns", "qui-sommes-nous",
                   "company", "mission", "vision")

# ── Search / help ─────────────────────────────────────────────────────────
KW_SEARCH       = ("search", "faq", "help", "support",
                   "frequently-asked-questions", "hilfe", "aide")

# ── Tag ───────────────────────────────────────────────────────────────────
KW_TAG_START    = ("tag", "tags", "label", "labels", "topic", "topics",
                   "keyword", "keywords")

# ── Category ──────────────────────────────────────────────────────────────
KW_CATEGORY_START = ("category", "categories", "cat", "rubrik",
                     "kategorie", "rubrique", "section", "sections")

# ── Author / profile ──────────────────────────────────────────────────────
KW_AUTHOR_START = ("author", "authors", "profile", "profiles", "user",
                   "users", "contributor", "contributors", "member", "members")

# ── Documentation ─────────────────────────────────────────────────────────
KW_DOCS_START   = ("docs", "doc", "documentation", "guide", "guides",
                   "tutorial", "tutorials", "wiki", "manual", "reference",
                   "api", "handbook", "learn", "knowledge", "kb")

# ── Product / shop ────────────────────────────────────────────────────────
KW_PRODUCT_START = ("product", "products", "shop", "store", "cart",
                    "checkout", "buy", "pricing", "plans", "order", "orders",
                    "item", "items", "catalogue", "catalog")

# ── Portfolio / gallery ───────────────────────────────────────────────────
KW_PORTFOLIO_START = ("gallery", "portfolio", "portfolios", "project",
                      "projects", "work", "works", "showcase",
                      "case-study", "case-studies")

# ── Archive / blog ────────────────────────────────────────────────────────
KW_ARCHIVE_START = ("archive", "archives")
KW_BLOG_START    = ("blog", "blogs", "post", "posts", "article", "articles",
                    "news", "journal", "stories", "story", "updates",
                    "newsletter")

# ── Regexes ───────────────────────────────────────────────────────────────
RE_DATE_SLUG  = re.compile(r"^/?\d{4}/\d{2}(/\d{2})?(/[^/]+)?/?$")
RE_PAGE_NUM   = re.compile(r"(^|/)page[/-]?\d+", re.IGNORECASE)


# ══════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)
logger.debug("_LANG_PREFIXES: %d codes loaded from %s", _lang_prefix_count, _lang_prefix_source)


# ══════════════════════════════════════════════════════════════════════════
# VALID PAGE TYPES
# ══════════════════════════════════════════════════════════════════════════

PAGE_TYPES = [
    "homepage",
    "blog_post",
    "blog_index",
    "category",
    "tag",
    "documentation",
    "about",
    "contact",
    "product",
    "auth",
    "legal",
    "search",
    "feed",
    "pagination",
    "author",
    "portfolio",
    "web_app",
    "entertainment",
    "landing_page",
    "content",  # catch-all (only if VLM also can't decide)
]


# ══════════════════════════════════════════════════════════════════════════
# PASS 1 — HEURISTIC URL-PATTERN CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════

def _segments(path: str) -> List[str]:
    """Return non-empty path segments, URL-decoded and lowercased."""
    return [unquote(s).lower() for s in path.strip("/").split("/") if s]


def _strip_lang_prefix(segs: List[str]) -> List[str]:
    """Remove a leading language/locale segment if present.

    e.g. ["en", "about"] → ["about"]
         ["fr-fr", "blog", "post"] → ["blog", "post"]
         ["2024", "my-post"] → ["2024", "my-post"]  (unchanged)
    """
    if segs and segs[0] in _LANG_PREFIXES:
        return segs[1:]
    return segs


def _has_any(path_lower: str, keywords: tuple) -> bool:
    """True if any keyword appears as a complete path segment."""
    for kw in keywords:
        if f"/{kw}/" in path_lower or path_lower.endswith(f"/{kw}"):
            return True
        if path_lower == kw:
            return True
    return False


def _segment_starts(segs: List[str], keywords: tuple) -> bool:
    """True if the first segment (after lang-prefix stripping) is a keyword."""
    return bool(segs) and segs[0] in keywords


def classify_url(url: str) -> str:
    """Classify a single URL into a page type using URL-pattern heuristics.

    Returns one of the PAGE_TYPES strings.

    Language/locale prefixes (e.g. /en/, /fr-fr/) are stripped before
    rule evaluation so multilingual sites are handled correctly.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "content"

    path = parsed.path or "/"
    path_lower = path.lower()
    segs_raw = _segments(path)
    segs = _strip_lang_prefix(segs_raw)  # strip /en/, /fr-fr/, etc.

    # Rebuild the effective path_lower after stripping the lang prefix so
    # _has_any() checks work correctly for keyword-anywhere rules.
    effective_path = "/" + "/".join(segs) if segs else "/"

    # 1. Homepage
    if not segs:
        return "homepage"

    # 2. Feed / meta files
    if _segment_starts(segs, KW_FEED_START):
        return "feed"
    if segs[-1] in KW_FEED_END:
        return "feed"

    # 3. Auth
    if _has_any(effective_path, KW_AUTH):
        return "auth"

    # 4. Legal
    if _has_any(effective_path, KW_LEGAL):
        return "legal"

    # 5. Contact
    if _has_any(effective_path, KW_CONTACT):
        return "contact"

    # 6. About
    if _has_any(effective_path, KW_ABOUT):
        return "about"

    # 7. Search / FAQ / help
    if _has_any(effective_path, KW_SEARCH):
        return "search"

    # 8. Tag pages
    if _segment_starts(segs, KW_TAG_START):
        return "tag"

    # 9. Category pages
    if _segment_starts(segs, KW_CATEGORY_START):
        return "category"

    # 10. Author / profile
    if _segment_starts(segs, KW_AUTHOR_START):
        return "author"

    # 11. Documentation / wiki
    if _segment_starts(segs, KW_DOCS_START):
        return "documentation"

    # 12. Product / shop
    if _segment_starts(segs, KW_PRODUCT_START):
        return "product"

    # 13. Portfolio / gallery
    if _segment_starts(segs, KW_PORTFOLIO_START):
        return "portfolio"

    # 14. Pagination
    if RE_PAGE_NUM.search(effective_path):
        return "pagination"

    # 15. Blog index vs blog post
    if _segment_starts(segs, KW_BLOG_START):
        return "blog_index" if len(segs) == 1 else "blog_post"

    # 16. Archive pages
    if _segment_starts(segs, KW_ARCHIVE_START):
        return "blog_index" if len(segs) == 1 else "blog_post"

    # 17. Date-slug pattern (common blog URLs: /2024/05/post-title)
    if RE_DATE_SLUG.match(effective_path):
        return "blog_post"

    # 18. Catch-all
    return "content"


def classify_webpage_list(urls: List[str]) -> List[dict]:
    """Classify a list of URLs.  Returns [{"url": ..., "page_type": ...}]."""
    return [{"url": u, "page_type": classify_url(u)} for u in urls]


def summarize_types(classifications: List[dict]) -> dict:
    """Count page types in a classification list."""
    return dict(Counter(c["page_type"] for c in classifications))


# ══════════════════════════════════════════════════════════════════════════
# PASS 2 — VLM SCREENSHOT-BASED CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════

VLM_PROMPT = """You are classifying a webpage screenshot into exactly ONE page type.

Categories:
- homepage         : Main landing page of the website
- blog_post        : Individual article / blog post / writeup
- blog_index       : List of blog posts / articles
- category         : Category listing page
- tag              : Tag listing page
- documentation    : Documentation, guides, tutorials, API reference, wiki
- about            : About page, bio, team page
- contact          : Contact page / form
- product          : Product page, shop, pricing, e-commerce
- auth             : Login / signup / registration page
- legal            : Privacy policy, terms of service, legal notices
- search           : Search results, FAQ, help center
- feed             : RSS feed, sitemap, or other meta files
- pagination       : Paginated listing page
- author           : Author profile, contributor page
- portfolio        : Gallery, portfolio, project showcase, case studies
- web_app          : Interactive web application, dashboard, calculator, tool
- entertainment    : Game, media player, creative/artistic page
- landing_page     : Marketing landing page, product promo with CTA
- content          : General content page (use ONLY if none of the above fit)

Return JSON only:
{"page_type": "<one of the above>", "confidence": 0.0-1.0, "reason": "brief explanation"}
"""


def _init_playwright():
    """Initialize Playwright browser (lazy singleton)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error("Playwright is required for VLM mode. Install: pip install playwright && playwright install chromium")
        return None, None

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    return pw, browser


def screenshot_url(url: str, output_path: Path, browser, timeout_ms: int = VLM_SCREENSHOT_TIMEOUT_MS) -> bool:
    """Take a screenshot of a live URL using Playwright.

    Returns True if screenshot was saved successfully.
    """
    page = None
    try:
        page = browser.new_page()
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=VLM_NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            pass  # Continue even if networkidle times out
        page.screenshot(path=str(output_path), full_page=False)
        return True
    except Exception as e:
        logger.debug("Screenshot failed for %s: %s", url, e)
        return False
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def _encode_image_base64(image_path: Path) -> str:
    """Encode image file as base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _screenshot_path(screenshot_dir: Path, url: str) -> Path:
    """Return a unique, filesystem-safe screenshot path for a URL.

    Uses the first SCREENSHOT_NAME_MAX_LEN chars of the sanitized URL
    plus an 8-char MD5 hash to prevent collisions on long/similar URLs.
    """
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", url)[:SCREENSHOT_NAME_MAX_LEN]
    return screenshot_dir / f"{safe_name}_{url_hash}.png"


def vlm_classify_screenshot(
    screenshot_path: Path,
    url: str,
    openai_client,
    deployment_name: str,
) -> Optional[dict]:
    """Send a screenshot to Azure OpenAI Vision and get page type classification.

    Returns {"page_type": str, "confidence": float, "reason": str} or None.
    """
    if not screenshot_path.exists():
        return None

    try:
        base64_image = _encode_image_base64(screenshot_path)

        response = openai_client.chat.completions.create(
            model=deployment_name,
            messages=[
                {"role": "system", "content": "You are a precise webpage classifier."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"URL: {url}\n\n{VLM_PROMPT}"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}",
                            },
                        },
                    ],
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=VLM_MAX_TOKENS,
        )

        content = response.choices[0].message.content
        result = json.loads(content)

        # Validate page_type
        page_type = result.get("page_type", "content")
        if page_type not in PAGE_TYPES:
            logger.warning("VLM returned unknown type '%s' for %s, keeping 'content'", page_type, url)
            page_type = "content"
        result["page_type"] = page_type
        return result

    except Exception as e:
        logger.warning("VLM classification failed for %s: %s", url, e)
        return None


def _init_openai_client():
    """Initialize Azure OpenAI client from env vars.

    Returns (client, deployment_name) or (None, None).
    """
    try:
        from openai import AzureOpenAI
    except ImportError:
        logger.error("openai package required for VLM mode. Install: pip install openai")
        return None, None

    api_key  = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if not api_key or not endpoint:
        logger.error("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set for VLM mode.")
        return None, None

    client = AzureOpenAI(
        api_key=api_key,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", DEFAULT_AZURE_API_VERSION),
        azure_endpoint=endpoint,
    )
    deployment = os.getenv(
        "AZURE_DEPLOYMENT",
        os.getenv("AZURE_OPENAI_DEPLOYMENT", DEFAULT_AZURE_DEPLOYMENT),
    )
    return client, deployment


def _load_vlm_checkpoint(path: str) -> dict:
    """Load VLM checkpoint: {url: {page_type, confidence, reason}}."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_vlm_checkpoint(path: str, results: dict) -> None:
    """Save VLM checkpoint atomically."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(results, f)
    os.replace(tmp, path)


def _process_one_vlm_item(
    cls_item: dict,
    browser,
    openai_client,
    deployment_name: str,
    screenshot_dir: Path,
) -> dict:
    """Worker: screenshot one URL then call VLM.  Runs in a thread.

    Each thread creates its own Playwright page from the shared browser
    (browser.new_page() is thread-safe in Playwright).

    Returns a result dict:
        {url, page_type, confidence, reason, error: bool}
    """
    url = cls_item["url"]
    ss_path = _screenshot_path(screenshot_dir, url)

    if not ss_path.exists():
        if not screenshot_url(url, ss_path, browser):
            return {"url": url, "page_type": "content",
                    "confidence": 0, "reason": "screenshot_failed", "error": True}

    result = vlm_classify_screenshot(ss_path, url, openai_client, deployment_name)
    if result:
        return {"url": url, **result, "error": False}
    return {"url": url, "page_type": "content",
            "confidence": 0, "reason": "vlm_error", "error": True}


def reclassify_content_pages(
    all_records: List[dict],
    openai_client,
    deployment_name: str,
    screenshot_dir: Path,
    checkpoint_path: str,
    max_vlm_calls: int,
    resume: bool,
    workers: int = VLM_WORKERS,
) -> Tuple[int, int]:
    """Re-classify 'content' pages using VLM screenshots.

    Runs screenshot + VLM API calls in parallel (one Playwright page per
    thread; a threading.Lock guards the shared cache, counters, and
    checkpoint saves).

    Modifies records in-place. Returns (reclassified_count, error_count).
    """
    from tqdm import tqdm

    screenshot_dir.mkdir(parents=True, exist_ok=True)

    vlm_cache = _load_vlm_checkpoint(checkpoint_path) if resume else {}
    logger.info("VLM checkpoint: %d cached results", len(vlm_cache))

    # Build a flat list of cls_item dicts that still need VLM attention,
    # keyed by url so we can update them in-place when futures complete.
    url_to_items: dict = {}   # url → [cls_item, ...]  (same URL may appear > once)
    for rec in all_records:
        for cls in rec["webpage_types"]:
            if cls["page_type"] == "content":
                url_to_items.setdefault(cls["url"], []).append(cls)

    # Apply cached results immediately (no API call needed)
    pending_items = []
    reclassified = 0
    for url, items in url_to_items.items():
        if url in vlm_cache:
            cached = vlm_cache[url]
            for item in items:
                item["page_type"]      = cached["page_type"]
                item["vlm_confidence"] = cached.get("confidence")
                item["vlm_reason"]     = cached.get("reason")
                item["vlm_classified"] = True
            if cached["page_type"] != "content":
                reclassified += len(items)
        else:
            pending_items.append(items[0])  # one representative per unique URL

    # Cap at max_vlm_calls
    pending_items = pending_items[:max_vlm_calls]
    logger.info("Content pages to reclassify: %d unique URLs ("
                "%d after cache / cap, workers=%d)",
                len(url_to_items), len(pending_items), workers)

    pw, browser = _init_playwright()
    if browser is None:
        logger.error("Cannot initialize Playwright — aborting VLM pass")
        return reclassified, 0

    errors    = 0
    vlm_calls = 0
    lock      = threading.Lock()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _process_one_vlm_item,
                    item, browser, openai_client, deployment_name, screenshot_dir,
                ): item
                for item in pending_items
            }

            with tqdm(total=len(futures), desc="VLM classifying", unit="url") as pbar:
                for fut in as_completed(futures):
                    original_item = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        logger.debug("VLM worker raised: %s", exc)
                        res = {"url": original_item["url"], "page_type": "content",
                               "confidence": 0, "reason": "worker_exception", "error": True}

                    url = res["url"]
                    # Update all cls_items that share this URL
                    for item in url_to_items.get(url, [original_item]):
                        item["page_type"]      = res["page_type"]
                        item["vlm_confidence"] = res.get("confidence")
                        item["vlm_reason"]     = res.get("reason")
                        item["vlm_classified"] = True

                    with lock:
                        vlm_cache[url] = {
                            "page_type":  res["page_type"],
                            "confidence": res.get("confidence", 0),
                            "reason":     res.get("reason", ""),
                        }
                        if res["error"]:
                            errors += 1
                        else:
                            vlm_calls += 1
                            if res["page_type"] != "content":
                                reclassified += len(url_to_items.get(url, [original_item]))
                            if vlm_calls % VLM_CHECKPOINT_SAVE_INTERVAL == 0:
                                _save_vlm_checkpoint(checkpoint_path, vlm_cache)

                    pbar.update(1)

    finally:
        browser.close()
        pw.stop()
        _save_vlm_checkpoint(checkpoint_path, vlm_cache)

    logger.info("VLM pass done: %d reclassified, %d errors, %d VLM calls",
                reclassified, errors, vlm_calls)
    return reclassified, errors


# ══════════════════════════════════════════════════════════════════════════
# CLI & MAIN
# ══════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Classify webpages in cwv-bench-v0 by page type",
    )
    p.add_argument("--dataset",  default=DEFAULT_DATASET,  help="HF dataset name")
    p.add_argument("--split",    default=DEFAULT_SPLIT)
    p.add_argument("--start",    type=int, default=0,      help="Start row index")
    p.add_argument("--limit",    type=int, default=0,      help="Max rows to process (0 = all)")
    p.add_argument("--output",   default=DEFAULT_OUTPUT,   help="Output JSONL path")

    p.add_argument("--vlm",          action="store_true",
                   help="Enable VLM pass to refine 'content' pages")
    p.add_argument("--max-vlm-calls", type=int, default=DEFAULT_MAX_VLM_CALLS,
                   help="Max number of VLM API calls (cost control)")
    p.add_argument("--screenshot-dir", default=DEFAULT_SCREENSHOT_DIR,
                   help="Directory to save screenshots")
    p.add_argument("--vlm-checkpoint", default=DEFAULT_VLM_CHECKPOINT,
                   help="VLM checkpoint file for resume")
    p.add_argument("--vlm-resume", action="store_true",
                   help="Resume VLM pass from checkpoint")
    p.add_argument("--vlm-workers", type=int, default=VLM_WORKERS,
                   help="Parallel workers for VLM pass")
    return p.parse_args()


def _print_summary(global_counts: Counter, total_urls: int, total_rows: int,
                   vlm_reclassified: int = 0) -> None:
    """Print aggregate classification statistics."""
    print("\n" + "=" * 60)
    print(f"  Webpage Classification Summary")
    print(f"  Rows processed   : {total_rows}")
    print(f"  Total URLs       : {total_urls}")
    if vlm_reclassified:
        print(f"  VLM reclassified : {vlm_reclassified}")
    print("=" * 60)
    print(f"\n{'Page Type':<20} {'Count':>8} {'Pct':>7}")
    print("-" * 37)
    for ptype, count in global_counts.most_common():
        pct = count / total_urls * 100 if total_urls else 0
        print(f"  {ptype:<18} {count:>8,} {pct:>6.1f}%")
    print("-" * 37)
    print(f"  {'TOTAL':<18} {total_urls:>8,}\n")


def main() -> None:
    from datasets import load_dataset
    from tqdm import tqdm

    args = parse_args()

    logger.info("Loading dataset %s (split=%s)...", args.dataset, args.split)
    ds = load_dataset(args.dataset, split=args.split)

    end   = len(ds) if args.limit <= 0 else min(len(ds), args.start + args.limit)
    total = max(0, end - args.start)
    logger.info("Processing rows [%d, %d)  (%d rows)", args.start, end, total)

    # ── Pass 1: Heuristic classification ──────────────────────────────────
    all_records = []
    total_urls  = 0

    for idx in tqdm(range(args.start, end), desc="Pass 1: Heuristic", unit="row"):
        row     = ds[idx]
        repo_id = row.get("REPO_ID") or row.get("repo_id") or ""
        urls    = row.get("deduped_webpages") or []

        classifications = classify_webpage_list(urls)
        total_urls += len(classifications)

        all_records.append({
            "index":         idx,
            "repo_id":       repo_id,
            "num_pages":     len(urls),
            "webpage_types": classifications,
        })

    global_counts: Counter = Counter(
        c["page_type"]
        for rec in all_records
        for c in rec["webpage_types"]
    )
    content_count = global_counts.get("content", 0)
    logger.info("Pass 1 done: %d URLs, %d classified as 'content'",
                total_urls, content_count)

    # ── Pass 2: VLM refinement (optional) ─────────────────────────────────
    vlm_reclassified = 0
    if args.vlm and content_count > 0:
        logger.info("Starting VLM pass...")
        openai_client, deployment = _init_openai_client()
        if openai_client is None:
            logger.error("Cannot init Azure OpenAI client — skipping VLM pass")
        else:
            vlm_reclassified, _ = reclassify_content_pages(
                all_records=all_records,
                openai_client=openai_client,
                deployment_name=deployment,
                screenshot_dir=Path(args.screenshot_dir),
                checkpoint_path=args.vlm_checkpoint,
                max_vlm_calls=args.max_vlm_calls,
                resume=args.vlm_resume,
                workers=args.vlm_workers,
            )
            global_counts = Counter(
                c["page_type"]
                for rec in all_records
                for c in rec["webpage_types"]
            )
    elif args.vlm and content_count == 0:
        logger.info("No 'content' pages found — skipping VLM pass")

    # ── Write output ───────────────────────────────────────────────────────
    with open(args.output, "w") as fout:
        for rec in all_records:
            rec["type_summary"] = summarize_types(rec["webpage_types"])
            fout.write(json.dumps(rec) + "\n")

    _print_summary(global_counts, total_urls, total, vlm_reclassified)


if __name__ == "__main__":
    main()
