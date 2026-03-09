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
# ── Resource (Feed / Meta) ────────────────────────────────────────────────
KW_FEED_START   = ("feed", "rss", "atom", "sitemap", "robots.txt")
KW_FEED_END     = ("feed", "rss", "atom", "rss.xml", "atom.xml",
                   "feed.xml", "sitemap.xml", "robots.txt")

# ── App (Auth / Functional) ───────────────────────────────────────────────
KW_APP          = ("login", "signin", "sign-in", "signup", "sign-up",
                   "register", "auth", "logout", "sign_in", "sign_up",
                   "oauth", "sso")

# ── Corporate (Legal, About, Contact) ─────────────────────────────────────
KW_CORPORATE    = ("about", "about-us", "about-me", "bio", "team",
                   "our-team", "staff", "ueber-uns", "qui-sommes-nous",
                   "company", "mission", "vision",
                   "contact", "contact-us", "contacto", "kontakt",
                   "reach-us", "get-in-touch",
                   "privacy", "privacy-policy", "terms", "terms-of-service",
                   "tos", "legal", "cookie-policy", "cookies", "gdpr",
                   "imprint", "impressum", "disclaimer", "copyright")

# ── Listing (Search, Tag, Category, Archive) ──────────────────────────────
KW_LISTING      = ("search", "faq", "help", "support",
                   "frequently-asked-questions", "hilfe", "aide",
                   "tag", "tags", "label", "labels", "topic", "topics",
                   "keyword", "keywords",
                   "category", "categories", "cat", "rubrik",
                   "kategorie", "rubrique", "section", "sections",
                   "archive", "archives")

# ── Profile (Author, Portfolio) ───────────────────────────────────────────
KW_PROFILE      = ("author", "authors", "profile", "profiles", "user",
                   "users", "contributor", "contributors", "member", "members",
                   "gallery", "portfolio", "portfolios", "project",
                   "projects", "work", "works", "showcase",
                   "case-study", "case-studies")

# ── Documentation ─────────────────────────────────────────────────────────
KW_DOCS_START   = ("docs", "doc", "documentation", "guide", "guides",
                   "tutorial", "tutorials", "wiki", "manual", "reference",
                   "api", "handbook", "learn", "knowledge", "kb")

# ── Product ───────────────────────────────────────────────────────────────
KW_PRODUCT_START = ("product", "products", "shop", "store", "cart",
                    "checkout", "buy", "pricing", "plans", "order", "orders",
                    "item", "items", "catalogue", "catalog")

# ── Marketing ─────────────────────────────────────────────────────────────
KW_MARKETING    = ("promo", "promotion", "offer", "campaign", "landing",
                   "sale", "special", "showcase")

# ── Article (Blog / News) ─────────────────────────────────────────────────
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
# VALID PAGE TYPES (REVISED & SIMPLIFIED)
# ══════════════════════════════════════════════════════════════════════════

PAGE_TYPES = [
    "homepage",       # Root of the domain
    "listing",        # List of items: blog index, category, tag, search, archive
    "article",        # Long-form text: blog post, news, press release
    "product",        # E-commerce product detail page
    "documentation",  # Technical docs, wiki, help center
    "corporate",      # About, contact, legal, careers, company info
    "profile",        # User profile, author bio, portfolio
    "app",            # Interactive tool, dashboard, login/auth, game
    "marketing",      # Landing page, promo, showcase
    "resource",       # Raw file, feed, sitemap
    "other",          # Fallback
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
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "other"

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

    # 2. Resource / Feed
    if _segment_starts(segs, KW_FEED_START):
        return "resource"
    if segs[-1] in KW_FEED_END:
        return "resource"

    # 3. App / Auth (Functional)
    if _has_any(effective_path, KW_APP):
        return "app"

    # 4. Corporate (Legal, About, Contact)
    # Merging legal, about, contact into 'corporate'
    if _has_any(effective_path, KW_CORPORATE):
        return "corporate"

    # 5. Listing (Search, Tag, Category)
    if _has_any(effective_path, KW_LISTING):
        return "listing"
    if RE_PAGE_NUM.search(effective_path):
        return "listing"
    # Note: KW_LISTING now includes tag/category keywords so individual checks are removed
    
    # 6. Profile (Author, Portfolio)
    if _segment_starts(segs, KW_PROFILE):
        return "profile"

    # 7. Documentation
    if _segment_starts(segs, KW_DOCS_START):
        return "documentation"

    # 8. Marketing
    if _has_any(effective_path, KW_MARKETING):
        return "marketing"

    # 9. Product
    if _segment_starts(segs, KW_PRODUCT_START):
        return "product"

    # 9. Article vs Listing (Blog logic)
    if _segment_starts(segs, KW_BLOG_START):
        # 'blog' root is usually a listing, sub-paths often posts
        # But /blog/page/2 is listing.
        # Simple heuristic: "blog" alone = listing, "blog/xyz" = article
        return "listing" if len(segs) == 1 else "article"

    # 10. Date-slug pattern (likely article)
    if RE_DATE_SLUG.match(effective_path):
        return "article"

    # 11. Catch-all
    return "other"


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
- homepage       : The main entry point (root) of the website.
- listing        : A list of items (e.g., blog index, category page, search results, archives, tags).
- article        : A distinct content piece (e.g., blog post, news article, essay, press release).
- product        : A dedicated page for selling a specific product or service (e-commerce detail, pricing).
- documentation  : Technical documentation, help center, wiki, API reference, or how-to guide.
- corporate      : Information about the entity (About Us, Company, Bios, Careers) or policies (Legal, Privacy, Contact).
- profile        : A specific page for a user, author, or portfolio showcase.
- app            : Interactive interface (Login/Auth, dashboard, tool, calculator, game, cart).
- marketing      : A standalone landing page detailed to convert or showcase (often distinct from standard site layout).
- resource       : Raw data file (XML, JSON, txt), feed, or sitemap.
- other          : Use ONLY if the page fits none of the above (e.g. blank, error page).

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
            max_completion_tokens=VLM_MAX_TOKENS,
        )

        content = response.choices[0].message.content
        result = json.loads(content)

        # Validate page_type
        page_type = result.get("page_type", "other")
        if page_type not in PAGE_TYPES:
            logger.warning("VLM returned unknown type '%s' for %s, keeping 'other'", page_type, url)
            page_type = "other"
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
            return {"url": url, "page_type": "other",
                    "confidence": 0, "reason": "screenshot_failed", "error": True}

    result = vlm_classify_screenshot(ss_path, url, openai_client, deployment_name)
    if result:
        return {"url": url, **result, "error": False}
    return {"url": url, "page_type": "other",
            "confidence": 0, "reason": "vlm_error", "error": True}




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


def main() -> None:
    from datasets import load_dataset
    from tqdm import tqdm

    args = parse_args()

    logger.info("Loading dataset %s (split=%s)...", args.dataset, args.split)
    ds = load_dataset(args.dataset, split=args.split)


# ── Thread-local Storage ────────────────────────────────────────────────────
_thread_local = threading.local()

def _get_thread_browser():
    """Get or create a thread-local Playwright browser instance."""
    if not hasattr(_thread_local, "pw"):
        from playwright.sync_api import sync_playwright
        _thread_local.pw = sync_playwright().start()
        _thread_local.browser = _thread_local.pw.chromium.launch(headless=True)
    return _thread_local.browser


def _close_thread_browser():
    """Close the thread-local Playwright instance if it exists."""
    if hasattr(_thread_local, "browser"):
        _thread_local.browser.close()
        del _thread_local.browser
    if hasattr(_thread_local, "pw"):
        _thread_local.pw.stop()
        del _thread_local.pw


def process_row(
    idx: int,
    row: dict,
    args: argparse.Namespace,
    openai_client,
    deployment: str,
    vlm_counter_lock: threading.Lock,
    vlm_counter: List[int],  # Mutable list to simulate pass-by-reference
) -> dict:
    """Process a single row (heuristics + optional VLM)."""
    repo_id = row.get("REPO_ID") or row.get("repo_id") or ""
    urls = row.get("deduped_webpages") or []

    # 1. Heuristic Classification
    webpage_types = classify_webpage_list(urls)

    # 2. VLM Refinement
    if args.vlm and openai_client:
        # Refine 'other' (catch-all) pages using VLM
        content_pages = [item for item in webpage_types if item["page_type"] == "other"]
        
        if content_pages:
            browser = None
            try:
                # Check limit before initializing browser
                limit_reached = False
                if args.max_vlm_calls > 0:
                    with vlm_counter_lock:
                         if vlm_counter[0] >= args.max_vlm_calls:
                             limit_reached = True
                
                if not limit_reached:
                     browser = _get_thread_browser()
                     
                     # Process items serially within the row (since rows are parallel)
                     for item in content_pages:
                        # Check limit again for each item
                        if args.max_vlm_calls > 0:
                             with vlm_counter_lock:
                                 if vlm_counter[0] >= args.max_vlm_calls:
                                     break

                        # Perform VLM
                        res = _process_one_vlm_item(
                            item, browser, openai_client, deployment, Path(args.screenshot_dir)
                        )
                        
                        # Update item logic (same as before)
                        item["page_type"]      = res["page_type"]
                        item["vlm_confidence"] = res.get("confidence")
                        item["vlm_reason"]     = res.get("reason")
                        item["vlm_classified"] = True
                        
                        if not res.get("error", False):
                             with vlm_counter_lock:
                                 vlm_counter[0] += 1
            except Exception as e:
                logger.error(f"Row {idx} VLM error: {e}")
                # Don't re-raise, return what we have

    return {
        "index": idx,
        "repo_id": repo_id,
        "num_pages": len(urls),
        "webpage_types": webpage_types,
        "type_summary": summarize_types(webpage_types)
    }


def main() -> None:
    from datasets import load_dataset
    from tqdm import tqdm

    args = parse_args()

    logger.info("Loading dataset %s (split=%s)...", args.dataset, args.split)
    ds = load_dataset(args.dataset, split=args.split)

    # ── Initialization ────────────────────────────────────────────────────────
    # Determine start index based on existing output if resuming
    start_index = args.start
    if args.vlm_resume and os.path.exists(args.output):
        with open(args.output, "r") as f:
            existing_lines = sum(1 for _ in f)
        logger.info("Resuming from output file: skipping %d rows", existing_lines)
        start_index += existing_lines

    # Calculate range
    end = len(ds) if args.limit <= 0 else min(len(ds), args.start + args.limit)
    # If we resumed, we might have skipped some of the requested 'limit' or 'start'
    current_idx = max(start_index, args.start)
    
    if current_idx >= end:
        logger.info("Nothing to do (start %d >= end %d)", current_idx, end)
        return

    total_to_process = end - current_idx
    logger.info("Processing rows [%d, %d)  (%d rows)", current_idx, end, total_to_process)

    # Global VLM Resources
    openai_client = None
    deployment = None
    if args.vlm:
        openai_client, deployment = _init_openai_client()
        if not openai_client:
             logger.warning("Failed to init OpenAI. VLM will be disabled.")
        else:
             Path(args.screenshot_dir).mkdir(parents=True, exist_ok=True)

    # Shared State
    vlm_counter = [0]
    vlm_counter_lock = threading.Lock()

    # ── Main Loop (Parallel) ──────────────────────────────────────────────────
    
    rows_processed = 0
    
    # Decide max_workers 
    # If VLM is on, using too many workers might hit API limits or CPU limits for Playwright.
    # Default vlm_workers=5 is a good start.
    max_workers = args.vlm_workers if args.vlm else (os.cpu_count() or 4) * 2
    logger.info(f"Using {max_workers} worker threads.")

    try:
        with open(args.output, "a") as fout:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Map futures to indices
                futures = {}
                for idx in range(current_idx, end):
                    row = ds[idx]
                    fut = executor.submit(
                        process_row, 
                        idx, row, args, openai_client, deployment, 
                        vlm_counter_lock, vlm_counter
                    )
                    futures[fut] = idx

                # Process as they complete
                for fut in tqdm(as_completed(futures), total=len(futures), desc="Classifying", unit="row"):
                    try:
                        result = fut.result()
                        fout.write(json.dumps(result) + "\n")
                        fout.flush()
                        rows_processed += 1
                    except Exception as e:
                        logger.error(f"Failed to process row (future result): {e}")

    finally:
        # Implicitly, thread-local browsers should be closed when threads die, 
        # but cleanup is cleaner if we could trigger it. 
        # Since we can't easily run code on thread exit in Executor, we rely on process exit.
        pass

    logger.info("Done. Processed %d rows.", rows_processed)

if __name__ == "__main__":
    main()
