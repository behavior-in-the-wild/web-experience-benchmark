#!/usr/bin/env python3
"""
CLS (Cumulative Layout Shift) Pattern Analyzer for CWV Benchmark Repos

This script:
1. Loads the HuggingFace dataset (behavior-in-the-wild/cwv-bench-v0)
2. Clones each repo
3. Analyzes for patterns that can produce non-zero CLS
4. Outputs findings as JSON with scoring

CLS measures visual stability - how much the page layout shifts unexpectedly.
Non-zero CLS is caused by:
- Images/videos without dimensions
- Ads, embeds, iframes without reserved space
- Dynamically injected content above existing content
- Web fonts causing FOIT/FOUT (Flash of Invisible/Unstyled Text)
- Animations using layout-triggering properties
- Late-loading content that pushes other elements

Usage:
    python cls_pattern_analyzer.py --output cls_results.json --max-repos 100
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import shutil
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import logging
import threading

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False
    def tqdm(iterable, **kwargs):
        return iterable

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# CLS PATTERN DEFINITIONS
# ============================================================================

@dataclass
class CLSPattern:
    """A pattern that can contribute to CLS"""
    name: str
    regex: str
    category: str  # 'missing_dimensions', 'dynamic_injection', 'font_loading', 'animations', 'ads_embeds', 'lazy_loading'
    framework: str  # 'global', 'react', 'vue', 'nextjs', 'static', 'css', etc.
    severity: str  # 'high', 'medium', 'low'
    description: str
    score: int = 1  # Base score for this pattern


# Severity to score multiplier mapping
SEVERITY_MULTIPLIERS = {
    'high': 3,
    'medium': 2,
    'low': 1,
}


# ============================================================================
# GLOBAL PATTERNS (Apply to all frameworks)
# ============================================================================

GLOBAL_PATTERNS = [
    # === IMAGES WITHOUT DIMENSIONS (MAJOR CLS CAUSE) ===
    CLSPattern(
        name="img_no_dimensions",
        regex=r"<img(?![^>]*(?:width|height)\s*=)[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="high",
        description="<img> tag without width/height attributes"
    ),
    CLSPattern(
        name="img_no_width",
        regex=r"<img(?![^>]*width\s*=)[^>]+height\s*=[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="high",
        description="<img> tag with height but no width"
    ),
    CLSPattern(
        name="img_no_height",
        regex=r"<img(?![^>]*height\s*=)[^>]+width\s*=[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="high",
        description="<img> tag with width but no height"
    ),
    CLSPattern(
        name="img_src_dynamic",
        regex=r"<img[^>]+src\s*=\s*['\"]?\s*$|img\.src\s*=",
        category="dynamic_injection",
        framework="global",
        severity="medium",
        description="Dynamic image source assignment"
    ),
    
    # === VIDEO/IFRAME WITHOUT DIMENSIONS ===
    CLSPattern(
        name="video_no_dimensions",
        regex=r"<video(?![^>]*(?:width|height)\s*=)[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="high",
        description="<video> tag without width/height attributes"
    ),
    CLSPattern(
        name="iframe_no_dimensions",
        regex=r"<iframe(?![^>]*(?:width|height)\s*=)[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="high",
        description="<iframe> tag without width/height attributes"
    ),
    CLSPattern(
        name="embed_no_dimensions",
        regex=r"<embed(?![^>]*(?:width|height)\s*=)[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="high",
        description="<embed> tag without width/height attributes"
    ),
    CLSPattern(
        name="object_no_dimensions",
        regex=r"<object(?![^>]*(?:width|height)\s*=)[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="medium",
        description="<object> tag without width/height attributes"
    ),
    CLSPattern(
        name="canvas_no_dimensions",
        regex=r"<canvas(?![^>]*(?:width|height)\s*=)[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="medium",
        description="<canvas> tag without width/height attributes"
    ),
    CLSPattern(
        name="svg_no_dimensions",
        regex=r"<svg(?![^>]*(?:width|height|viewBox)\s*=)[^>]*>",
        category="missing_dimensions",
        framework="global",
        severity="medium",
        description="<svg> tag without width/height/viewBox"
    ),
    
    # === DYNAMIC DOM INJECTION (CONTENT SHIFT) ===
    CLSPattern(
        name="innerHTML_assignment",
        regex=r"\.innerHTML\s*=",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="innerHTML assignment (can inject content causing shift)"
    ),
    CLSPattern(
        name="outerHTML_assignment",
        regex=r"\.outerHTML\s*=",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="outerHTML assignment (replaces element)"
    ),
    CLSPattern(
        name="insertAdjacentHTML",
        regex=r"insertAdjacentHTML\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="insertAdjacentHTML (injects HTML at position)"
    ),
    CLSPattern(
        name="appendChild",
        regex=r"\.appendChild\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="medium",
        description="appendChild (adds element to DOM)"
    ),
    CLSPattern(
        name="insertBefore",
        regex=r"\.insertBefore\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="insertBefore (inserts element, can push content down)"
    ),
    CLSPattern(
        name="prepend",
        regex=r"\.(prepend|before)\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="prepend/before (inserts at start, shifts content)"
    ),
    CLSPattern(
        name="document_write",
        regex=r"document\.write\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="document.write (blocks parsing, injects content)"
    ),
    CLSPattern(
        name="createElement_img",
        regex=r"createElement\s*\(\s*['\"]img['\"]\s*\)",
        category="dynamic_injection",
        framework="global",
        severity="medium",
        description="Creating img element dynamically"
    ),
    CLSPattern(
        name="createElement_div",
        regex=r"createElement\s*\(\s*['\"]div['\"]\s*\)",
        category="dynamic_injection",
        framework="global",
        severity="low",
        description="Creating div element dynamically"
    ),
    
    # === FONT LOADING (FOIT/FOUT) ===
    CLSPattern(
        name="font_face_declaration",
        regex=r"@font-face\s*\{",
        category="font_loading",
        framework="css",
        severity="medium",
        description="@font-face declaration (potential FOIT/FOUT)"
    ),
    CLSPattern(
        name="font_display_auto",
        regex=r"font-display\s*:\s*(auto|block)\s*;",
        category="font_loading",
        framework="css",
        severity="high",
        description="font-display: auto/block (causes FOIT)"
    ),
    CLSPattern(
        name="font_display_swap",
        regex=r"font-display\s*:\s*swap\s*;",
        category="font_loading",
        framework="css",
        severity="medium",
        description="font-display: swap (causes FOUT, better than FOIT)"
    ),
    CLSPattern(
        name="google_fonts_link",
        regex=r"fonts\.googleapis\.com|fonts\.gstatic\.com",
        category="font_loading",
        framework="global",
        severity="medium",
        description="Google Fonts (external font loading)"
    ),
    CLSPattern(
        name="typekit_fonts",
        regex=r"use\.typekit\.net|typekit\.com",
        category="font_loading",
        framework="global",
        severity="medium",
        description="Adobe Typekit fonts (external font loading)"
    ),
    CLSPattern(
        name="font_awesome",
        regex=r"font-awesome|fontawesome",
        category="font_loading",
        framework="global",
        severity="low",
        description="Font Awesome (icon font loading)"
    ),
    CLSPattern(
        name="webfont_loader",
        regex=r"WebFont\.load|webfontloader",
        category="font_loading",
        framework="global",
        severity="medium",
        description="WebFont loader library"
    ),
    
    # === ADS AND THIRD-PARTY EMBEDS ===
    CLSPattern(
        name="google_adsense",
        regex=r"googlesyndication\.com|adsbygoogle|google_ad_client",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Google AdSense (ads cause major CLS)"
    ),
    CLSPattern(
        name="google_doubleclick",
        regex=r"doubleclick\.net|googletag\.cmd",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Google DoubleClick/GPT ads"
    ),
    CLSPattern(
        name="amazon_ads",
        regex=r"amazon-adsystem\.com",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Amazon ads"
    ),
    CLSPattern(
        name="ad_slot_div",
        regex=r"<div[^>]+(ad-slot|ad-container|ad-wrapper|advertisement|banner-ad)[^>]*>",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Ad container div (likely dynamic ad injection)"
    ),
    CLSPattern(
        name="twitter_embed",
        regex=r"platform\.twitter\.com/widgets|twitter-tweet",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Twitter/X embed (loads async, causes shift)"
    ),
    CLSPattern(
        name="facebook_embed",
        regex=r"connect\.facebook\.net|fb-post|fb-video",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Facebook embed (loads async, causes shift)"
    ),
    CLSPattern(
        name="instagram_embed",
        regex=r"instagram\.com/embed|instagram-media",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Instagram embed (loads async, causes shift)"
    ),
    CLSPattern(
        name="youtube_embed",
        regex=r"youtube\.com/embed|youtube-nocookie\.com",
        category="ads_embeds",
        framework="global",
        severity="medium",
        description="YouTube embed (usually has dimensions)"
    ),
    CLSPattern(
        name="disqus_comments",
        regex=r"disqus\.com|disqus_thread",
        category="ads_embeds",
        framework="global",
        severity="high",
        description="Disqus comments (loads async at bottom)"
    ),
    CLSPattern(
        name="intercom_widget",
        regex=r"intercom\.com|intercomSettings",
        category="ads_embeds",
        framework="global",
        severity="medium",
        description="Intercom chat widget"
    ),
    CLSPattern(
        name="drift_chat",
        regex=r"drift\.com|driftt\.com",
        category="ads_embeds",
        framework="global",
        severity="medium",
        description="Drift chat widget"
    ),
    CLSPattern(
        name="hubspot_forms",
        regex=r"hsforms\.com|hbspt\.forms",
        category="ads_embeds",
        framework="global",
        severity="medium",
        description="HubSpot forms (async loading)"
    ),
    
    # === CSS ANIMATIONS THAT CAUSE LAYOUT SHIFT ===
    CLSPattern(
        name="animation_height",
        regex=r"animation[^;]*height|@keyframes[^}]*height\s*:",
        category="animations",
        framework="css",
        severity="high",
        description="Animation changing height (causes layout shift)"
    ),
    CLSPattern(
        name="animation_width",
        regex=r"animation[^;]*width|@keyframes[^}]*width\s*:",
        category="animations",
        framework="css",
        severity="high",
        description="Animation changing width (causes layout shift)"
    ),
    CLSPattern(
        name="animation_top",
        regex=r"@keyframes[^}]*top\s*:",
        category="animations",
        framework="css",
        severity="high",
        description="Animation changing top position"
    ),
    CLSPattern(
        name="animation_left",
        regex=r"@keyframes[^}]*left\s*:",
        category="animations",
        framework="css",
        severity="high",
        description="Animation changing left position"
    ),
    CLSPattern(
        name="animation_margin",
        regex=r"@keyframes[^}]*margin",
        category="animations",
        framework="css",
        severity="high",
        description="Animation changing margin (causes reflow)"
    ),
    CLSPattern(
        name="animation_padding",
        regex=r"@keyframes[^}]*padding",
        category="animations",
        framework="css",
        severity="medium",
        description="Animation changing padding"
    ),
    CLSPattern(
        name="transition_height",
        regex=r"transition[^;]*height",
        category="animations",
        framework="css",
        severity="medium",
        description="Height transition (can cause shift)"
    ),
    CLSPattern(
        name="transition_width",
        regex=r"transition[^;]*width",
        category="animations",
        framework="css",
        severity="medium",
        description="Width transition (can cause shift)"
    ),
    CLSPattern(
        name="transition_all",
        regex=r"transition\s*:\s*all",
        category="animations",
        framework="css",
        severity="medium",
        description="transition: all (may include layout properties)"
    ),
    
    # === LAZY LOADING PATTERNS ===
    CLSPattern(
        name="lazy_load_attribute",
        regex=r"loading\s*=\s*['\"]lazy['\"]",
        category="lazy_loading",
        framework="global",
        severity="low",
        description="Native lazy loading (good if dimensions set)"
    ),
    CLSPattern(
        name="lazyload_class",
        regex=r"class\s*=\s*['\"][^'\"]*lazy[^'\"]*['\"]",
        category="lazy_loading",
        framework="global",
        severity="medium",
        description="Lazy load class (needs placeholder)"
    ),
    CLSPattern(
        name="data_src_pattern",
        regex=r"data-src\s*=",
        category="lazy_loading",
        framework="global",
        severity="medium",
        description="data-src pattern (lazy loading images)"
    ),
    CLSPattern(
        name="lazysizes_library",
        regex=r"lazysizes|lazyload\.js",
        category="lazy_loading",
        framework="global",
        severity="medium",
        description="Lazysizes library"
    ),
    CLSPattern(
        name="intersection_observer",
        regex=r"IntersectionObserver",
        category="lazy_loading",
        framework="global",
        severity="low",
        description="IntersectionObserver (used for lazy loading)"
    ),
    
    # === SKELETON/PLACEHOLDER PATTERNS (POSITIVE - REDUCES CLS) ===
    CLSPattern(
        name="skeleton_loader",
        regex=r"skeleton|placeholder|shimmer",
        category="skeleton_placeholder",
        framework="global",
        severity="low",
        description="Skeleton/placeholder pattern (helps prevent CLS)"
    ),
    CLSPattern(
        name="aspect_ratio_box",
        regex=r"aspect-ratio\s*:|padding-bottom\s*:\s*\d+(\.\d+)?%",
        category="skeleton_placeholder",
        framework="css",
        severity="low",
        description="Aspect ratio box (reserves space)"
    ),
    
    # === JQUERY DOM MANIPULATION ===
    CLSPattern(
        name="jquery_prepend",
        regex=r"\$\([^)]+\)\.(prepend|before|insertBefore)\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="jQuery prepend/before (inserts above, causes shift)"
    ),
    CLSPattern(
        name="jquery_append",
        regex=r"\$\([^)]+\)\.(append|after|insertAfter)\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="medium",
        description="jQuery append/after"
    ),
    CLSPattern(
        name="jquery_html",
        regex=r"\$\([^)]+\)\.html\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="medium",
        description="jQuery .html() (replaces content)"
    ),
    CLSPattern(
        name="jquery_load",
        regex=r"\$\([^)]+\)\.load\s*\(",
        category="dynamic_injection",
        framework="global",
        severity="high",
        description="jQuery .load() (async content injection)"
    ),
]

# ============================================================================
# REACT-SPECIFIC PATTERNS
# ============================================================================

REACT_PATTERNS = [
    CLSPattern(
        name="react_img_no_dimensions",
        regex=r"<img[^>]+src\s*=\s*\{(?![^>]*(?:width|height))[^>]*/>",
        category="missing_dimensions",
        framework="react",
        severity="high",
        description="React <img> with dynamic src but no dimensions"
    ),
    CLSPattern(
        name="react_lazy_component",
        regex=r"React\.lazy\s*\(|lazy\s*\(\s*\(\s*\)\s*=>",
        category="lazy_loading",
        framework="react",
        severity="medium",
        description="React.lazy (code splitting, may cause flash)"
    ),
    CLSPattern(
        name="react_suspense",
        regex=r"<Suspense",
        category="lazy_loading",
        framework="react",
        severity="low",
        description="React Suspense (needs proper fallback)"
    ),
    CLSPattern(
        name="react_conditional_render",
        regex=r"\{[^}]*\?\s*<[A-Z][^:]*:\s*(null|<)",
        category="dynamic_injection",
        framework="react",
        severity="medium",
        description="Conditional rendering (may cause layout shift)"
    ),
    CLSPattern(
        name="react_dangerouslySetInnerHTML",
        regex=r"dangerouslySetInnerHTML",
        category="dynamic_injection",
        framework="react",
        severity="high",
        description="dangerouslySetInnerHTML (injects raw HTML)"
    ),
    CLSPattern(
        name="react_portal",
        regex=r"createPortal|ReactDOM\.createPortal",
        category="dynamic_injection",
        framework="react",
        severity="medium",
        description="React Portal (renders outside DOM hierarchy)"
    ),
    CLSPattern(
        name="react_loading_state",
        regex=r"(isLoading|loading)\s*\?\s*",
        category="dynamic_injection",
        framework="react",
        severity="medium",
        description="Loading state conditional (content swap)"
    ),
    CLSPattern(
        name="react_use_effect_dom",
        regex=r"useEffect\s*\([^)]*\{[^}]*(innerHTML|appendChild|insertBefore)",
        category="dynamic_injection",
        framework="react",
        severity="high",
        description="useEffect with DOM manipulation"
    ),
]

# ============================================================================
# NEXT.JS SPECIFIC PATTERNS
# ============================================================================

NEXTJS_PATTERNS = [
    CLSPattern(
        name="nextjs_image_no_size",
        regex=r"<Image(?![^>]*(?:width|height|fill|layout\s*=\s*['\"]fill))[^>]*/>",
        category="missing_dimensions",
        framework="nextjs",
        severity="high",
        description="Next.js Image without width/height/fill"
    ),
    CLSPattern(
        name="nextjs_image_component",
        regex=r"import[^}]*Image[^}]*from\s*['\"]next/image['\"]|<Image",
        category="missing_dimensions",
        framework="nextjs",
        severity="low",
        description="Next.js Image component (good if sized)"
    ),
    CLSPattern(
        name="nextjs_dynamic_import",
        regex=r"dynamic\s*\(\s*\(\s*\)\s*=>\s*import",
        category="lazy_loading",
        framework="nextjs",
        severity="medium",
        description="Next.js dynamic import"
    ),
    CLSPattern(
        name="nextjs_script_component",
        regex=r"<Script[^>]+strategy\s*=\s*['\"]afterInteractive['\"]",
        category="dynamic_injection",
        framework="nextjs",
        severity="medium",
        description="Next.js Script afterInteractive (may inject late)"
    ),
    CLSPattern(
        name="nextjs_font_optimization",
        regex=r"next/font|@next/font",
        category="font_loading",
        framework="nextjs",
        severity="low",
        description="Next.js font optimization (reduces CLS)"
    ),
]

# ============================================================================
# VUE-SPECIFIC PATTERNS
# ============================================================================

VUE_PATTERNS = [
    CLSPattern(
        name="vue_v_if_content",
        regex=r"v-if\s*=\s*['\"][^'\"]+['\"]",
        category="dynamic_injection",
        framework="vue",
        severity="medium",
        description="v-if directive (conditional rendering)"
    ),
    CLSPattern(
        name="vue_v_show",
        regex=r"v-show\s*=",
        category="dynamic_injection",
        framework="vue",
        severity="low",
        description="v-show (toggles display, reserves space)"
    ),
    CLSPattern(
        name="vue_v_html",
        regex=r"v-html\s*=",
        category="dynamic_injection",
        framework="vue",
        severity="high",
        description="v-html directive (injects raw HTML)"
    ),
    CLSPattern(
        name="vue_async_component",
        regex=r"defineAsyncComponent|asyncComponent",
        category="lazy_loading",
        framework="vue",
        severity="medium",
        description="Vue async component"
    ),
    CLSPattern(
        name="vue_transition",
        regex=r"<transition|<Transition",
        category="animations",
        framework="vue",
        severity="low",
        description="Vue transition component"
    ),
    CLSPattern(
        name="vue_img_no_dimensions",
        regex=r"<img[^>]+:src\s*=\s*['\"]?[^'\"]+['\"]?(?![^>]*(?:width|height))[^>]*>",
        category="missing_dimensions",
        framework="vue",
        severity="high",
        description="Vue dynamic img without dimensions"
    ),
]

# ============================================================================
# STATIC SITE PATTERNS (Jekyll, Hugo, Hexo, etc.)
# ============================================================================

STATIC_PATTERNS = [
    CLSPattern(
        name="static_include_partial",
        regex=r"\{%\s*include|\{\{\s*partial|<%=\s*include|@include",
        category="dynamic_injection",
        framework="static",
        severity="low",
        description="Template include/partial (server-rendered, OK)"
    ),
    CLSPattern(
        name="hugo_shortcode",
        regex=r"\{\{<\s*\w+|shortcode",
        category="dynamic_injection",
        framework="static",
        severity="low",
        description="Hugo shortcode"
    ),
    CLSPattern(
        name="jekyll_responsive_image",
        regex=r"\{%\s*responsive_image|\{%\s*picture",
        category="missing_dimensions",
        framework="static",
        severity="low",
        description="Jekyll responsive image plugin"
    ),
]

# ============================================================================
# CSS-SPECIFIC PATTERNS
# ============================================================================

CSS_PATTERNS = [
    CLSPattern(
        name="css_position_absolute",
        regex=r"position\s*:\s*absolute",
        category="animations",
        framework="css",
        severity="low",
        description="position: absolute (doesn't cause shift if contained)"
    ),
    CLSPattern(
        name="css_position_fixed",
        regex=r"position\s*:\s*fixed",
        category="animations",
        framework="css",
        severity="low",
        description="position: fixed (out of flow, no shift)"
    ),
    CLSPattern(
        name="css_contain_layout",
        regex=r"contain\s*:[^;]*(layout|strict|content)",
        category="skeleton_placeholder",
        framework="css",
        severity="low",
        description="CSS containment (helps prevent shifts)"
    ),
    CLSPattern(
        name="css_min_height",
        regex=r"min-height\s*:\s*\d+",
        category="skeleton_placeholder",
        framework="css",
        severity="low",
        description="min-height (reserves space)"
    ),
    CLSPattern(
        name="css_transform_animation",
        regex=r"@keyframes[^}]*(transform|translate|scale|rotate)",
        category="animations",
        framework="css",
        severity="low",
        description="Transform animation (doesn't cause layout shift)"
    ),
    CLSPattern(
        name="css_will_change",
        regex=r"will-change\s*:",
        category="animations",
        framework="css",
        severity="low",
        description="will-change hint (optimization)"
    ),
]

# ============================================================================
# COMBINE ALL PATTERNS
# ============================================================================

ALL_PATTERNS = (
    GLOBAL_PATTERNS +
    REACT_PATTERNS +
    NEXTJS_PATTERNS +
    VUE_PATTERNS +
    STATIC_PATTERNS +
    CSS_PATTERNS
)

# File extensions to analyze
ANALYZABLE_EXTENSIONS = {
    '.js', '.jsx', '.ts', '.tsx',  # JavaScript/TypeScript
    '.vue', '.svelte',              # Vue/Svelte
    '.html', '.htm',                # HTML
    '.css', '.scss', '.sass', '.less',  # Stylesheets
    '.md', '.mdx', '.qmd',          # Markdown (may contain HTML)
    '.liquid', '.njk', '.ejs',      # Template engines
    '.hbs', '.handlebars',
    '.jinja', '.jinja2', '.j2',
    '.erb',
    '.php',                         # PHP templates
}

# Directories to skip
SKIP_DIRS = {
    'node_modules', '.git', 'dist', 'build', '.next', '.nuxt',
    '__pycache__', '.pytest_cache', '.venv', 'venv', 'env',
    'vendor', 'bower_components', '.cache', 'coverage',
}


# ============================================================================
# ANALYSIS CLASSES
# ============================================================================

@dataclass
class PatternMatch:
    """A single pattern match in a file"""
    pattern_name: str
    category: str
    framework: str
    severity: str
    line_number: int
    line_content: str
    file_path: str
    score: int = 1
    matched_text: str = ""  # The actual matched text


@dataclass
class FileAnalysis:
    """Analysis results for a single file"""
    file_path: str
    relative_path: str
    matches: list = field(default_factory=list)
    total_score: int = 0
    score_by_category: dict = field(default_factory=dict)
    score_by_severity: dict = field(default_factory=dict)
    has_missing_dimensions: bool = False
    has_dynamic_injection: bool = False
    has_font_issues: bool = False
    has_ads_embeds: bool = False
    cls_risk_level: str = "none"  # none, low, medium, high


@dataclass
class RepoAnalysis:
    """Analysis results for a repository"""
    repo_id: str
    framework: str
    commit_id: str
    clone_path: str
    total_files_analyzed: int = 0
    files_with_matches: int = 0
    total_matches: int = 0
    # Counts
    matches_by_category: dict = field(default_factory=dict)
    matches_by_severity: dict = field(default_factory=dict)
    matches_by_pattern: dict = field(default_factory=dict)
    # Scoring
    total_score: int = 0
    score_by_category: dict = field(default_factory=dict)
    score_by_severity: dict = field(default_factory=dict)
    score_by_pattern: dict = field(default_factory=dict)
    score_by_file: dict = field(default_factory=dict)
    top_scoring_files: list = field(default_factory=list)
    # Risk assessment
    cls_risk_level: str = "none"
    risk_factors: list = field(default_factory=list)
    # File details
    file_analyses: list = field(default_factory=list)
    error: Optional[str] = None


# ============================================================================
# ANALYZER
# ============================================================================

class CLSPatternAnalyzer:
    """Analyzes repositories for CLS-producing patterns"""

    def __init__(self, patterns: list[CLSPattern] = None, verbose: bool = False):
        self.patterns = patterns or ALL_PATTERNS
        self.verbose = verbose
        # Pre-compile all regexes
        self.compiled_patterns = [
            (p, re.compile(p.regex, re.IGNORECASE | re.MULTILINE))
            for p in self.patterns
        ]

    def should_analyze_file(self, file_path: Path) -> bool:
        """Check if file should be analyzed"""
        if file_path.suffix.lower() not in ANALYZABLE_EXTENSIONS:
            return False

        # Skip files in excluded directories
        for part in file_path.parts:
            if part in SKIP_DIRS:
                return False

        # Skip minified files
        if '.min.' in file_path.name:
            return False

        return True

    def analyze_file(self, file_path: Path, repo_root: Path) -> FileAnalysis:
        """Analyze a single file for CLS patterns"""
        relative_path = str(file_path.relative_to(repo_root))
        analysis = FileAnalysis(
            file_path=str(file_path),
            relative_path=relative_path,
            score_by_category={},
            score_by_severity={},
        )

        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            lines = content.split('\n')

            for pattern, compiled_regex in self.compiled_patterns:
                for match in compiled_regex.finditer(content):
                    # Find line number
                    line_start = content.count('\n', 0, match.start())
                    line_content = lines[line_start].strip()[:200]

                    # Calculate score
                    match_score = pattern.score * SEVERITY_MULTIPLIERS.get(pattern.severity, 1)

                    pattern_match = PatternMatch(
                        pattern_name=pattern.name,
                        category=pattern.category,
                        framework=pattern.framework,
                        severity=pattern.severity,
                        line_number=line_start + 1,
                        line_content=line_content,
                        file_path=relative_path,
                        score=match_score,
                        matched_text=match.group(0)[:100],  # Truncate matched text
                    )
                    analysis.matches.append(pattern_match)

                    # Update file scores
                    analysis.total_score += match_score
                    analysis.score_by_category[pattern.category] = analysis.score_by_category.get(pattern.category, 0) + match_score
                    analysis.score_by_severity[pattern.severity] = analysis.score_by_severity.get(pattern.severity, 0) + match_score

                    # Track issue categories
                    if pattern.category == 'missing_dimensions':
                        analysis.has_missing_dimensions = True
                    elif pattern.category == 'dynamic_injection':
                        analysis.has_dynamic_injection = True
                    elif pattern.category == 'font_loading':
                        analysis.has_font_issues = True
                    elif pattern.category == 'ads_embeds':
                        analysis.has_ads_embeds = True

            # Calculate file risk level
            analysis.cls_risk_level = self._calculate_file_risk(analysis)

        except Exception as e:
            logger.debug(f"Error analyzing {file_path}: {e}")

        return analysis

    def _calculate_file_risk(self, analysis: FileAnalysis) -> str:
        """Calculate CLS risk level for a file"""
        high_severity_score = analysis.score_by_severity.get('high', 0)
        
        if high_severity_score >= 15:
            return "high"
        elif high_severity_score >= 6 or analysis.total_score >= 20:
            return "medium"
        elif analysis.total_score > 0:
            return "low"
        return "none"

    def analyze_repo(self, repo_path: Path, repo_id: str, framework: str, commit_id: str) -> RepoAnalysis:
        """Analyze an entire repository"""
        analysis = RepoAnalysis(
            repo_id=repo_id,
            framework=framework,
            commit_id=commit_id,
            clone_path=str(repo_path)
        )

        # Initialize counters
        analysis.matches_by_category = defaultdict(int)
        analysis.matches_by_severity = defaultdict(int)
        analysis.matches_by_pattern = defaultdict(int)
        analysis.score_by_category = defaultdict(int)
        analysis.score_by_severity = defaultdict(int)
        analysis.score_by_pattern = defaultdict(int)
        analysis.score_by_file = {}
        analysis.risk_factors = []

        try:
            # Find all analyzable files
            if self.verbose:
                logger.info(f"    [1/5] Scanning for analyzable files...")
            files_to_analyze = []
            for file_path in repo_path.rglob('*'):
                if file_path.is_file() and self.should_analyze_file(file_path):
                    files_to_analyze.append(file_path)

            analysis.total_files_analyzed = len(files_to_analyze)
            if self.verbose:
                logger.info(f"    [2/5] Found {len(files_to_analyze)} files to analyze")

            # Analyze each file
            if self.verbose:
                logger.info(f"    [3/5] Running pattern matching...")
            for i, file_path in enumerate(files_to_analyze):
                if self.verbose and len(files_to_analyze) > 50 and (i + 1) % 50 == 0:
                    logger.info(f"          Processed {i+1}/{len(files_to_analyze)} files...")
                file_analysis = self.analyze_file(file_path, repo_path)

                if file_analysis.matches:
                    analysis.files_with_matches += 1
                    analysis.file_analyses.append(file_analysis)

                    # Track file score
                    if file_analysis.total_score > 0:
                        analysis.score_by_file[file_analysis.relative_path] = file_analysis.total_score

                    for match in file_analysis.matches:
                        analysis.total_matches += 1
                        analysis.matches_by_category[match.category] += 1
                        analysis.matches_by_severity[match.severity] += 1
                        analysis.matches_by_pattern[match.pattern_name] += 1

                        # Update repo scores
                        analysis.total_score += match.score
                        analysis.score_by_category[match.category] += match.score
                        analysis.score_by_severity[match.severity] += match.score
                        analysis.score_by_pattern[match.pattern_name] += match.score

            # Identify risk factors
            if self.verbose:
                logger.info(f"    [4/5] Identifying risk factors...")
            analysis.risk_factors = self._identify_risk_factors(analysis)

            # Calculate overall risk level
            if self.verbose:
                logger.info(f"    [5/5] Calculating CLS risk level...")
            analysis.cls_risk_level = self._calculate_repo_risk(analysis)

            # Get top scoring files
            analysis.top_scoring_files = sorted(
                [{'file': path, 'score': score} for path, score in analysis.score_by_file.items()],
                key=lambda x: x['score'],
                reverse=True
            )[:20]

            # Convert defaultdicts to regular dicts
            analysis.matches_by_category = dict(analysis.matches_by_category)
            analysis.matches_by_severity = dict(analysis.matches_by_severity)
            analysis.matches_by_pattern = dict(analysis.matches_by_pattern)
            analysis.score_by_category = dict(analysis.score_by_category)
            analysis.score_by_severity = dict(analysis.score_by_severity)
            analysis.score_by_pattern = dict(analysis.score_by_pattern)

        except Exception as e:
            analysis.error = str(e)
            logger.error(f"Error analyzing repo {repo_id}: {e}")

        return analysis

    def _identify_risk_factors(self, analysis: RepoAnalysis) -> list:
        """Identify specific CLS risk factors"""
        risk_factors = []

        if analysis.matches_by_category.get('missing_dimensions', 0) > 5:
            risk_factors.append({
                'factor': 'Multiple images/media without dimensions',
                'severity': 'high',
                'count': analysis.matches_by_category['missing_dimensions']
            })

        if analysis.matches_by_category.get('ads_embeds', 0) > 0:
            risk_factors.append({
                'factor': 'Third-party ads/embeds detected',
                'severity': 'high',
                'count': analysis.matches_by_category['ads_embeds']
            })

        if analysis.matches_by_category.get('font_loading', 0) > 3:
            risk_factors.append({
                'factor': 'Multiple web fonts (FOIT/FOUT risk)',
                'severity': 'medium',
                'count': analysis.matches_by_category['font_loading']
            })

        if analysis.matches_by_category.get('dynamic_injection', 0) > 10:
            risk_factors.append({
                'factor': 'Heavy dynamic DOM injection',
                'severity': 'high',
                'count': analysis.matches_by_category['dynamic_injection']
            })

        if analysis.matches_by_category.get('animations', 0) > 5:
            risk_factors.append({
                'factor': 'Layout-affecting animations',
                'severity': 'medium',
                'count': analysis.matches_by_category['animations']
            })

        return risk_factors

    def _calculate_repo_risk(self, analysis: RepoAnalysis) -> str:
        """Calculate overall CLS risk level for repo"""
        high_severity_score = analysis.score_by_severity.get('high', 0)
        has_ads = analysis.matches_by_category.get('ads_embeds', 0) > 0
        missing_dims = analysis.matches_by_category.get('missing_dimensions', 0)

        if has_ads or high_severity_score >= 50 or missing_dims > 20:
            return "high"
        elif high_severity_score >= 20 or missing_dims > 10:
            return "medium"
        elif analysis.total_score > 0:
            return "low"
        return "none"


# ============================================================================
# REPO CLONING (same as INP analyzer)
# ============================================================================

def clone_repo_from_zip(zip_url: str, dest_path: Path) -> bool:
    """Download and extract a repo from ZIP URL"""
    try:
        import urllib.request
        import zipfile
        import io

        logger.info(f"Downloading {zip_url}")

        with urllib.request.urlopen(zip_url, timeout=60) as response:
            zip_data = response.read()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            temp_extract = dest_path.parent / f"{dest_path.name}_temp"
            zf.extractall(temp_extract)

            extracted_dirs = list(temp_extract.iterdir())
            if len(extracted_dirs) == 1 and extracted_dirs[0].is_dir():
                shutil.move(str(extracted_dirs[0]), str(dest_path))
                temp_extract.rmdir()
            else:
                shutil.move(str(temp_extract), str(dest_path))

        return True
    except Exception as e:
        logger.error(f"Failed to clone from ZIP {zip_url}: {e}")
        return False


def clone_repo_from_git(repo_id: str, commit_id: str, dest_path: Path) -> bool:
    """Clone a repo using git"""
    try:
        github_url = f"https://github.com/{repo_id}.git"

        result = subprocess.run(
            ["git", "clone", "--depth", "1", github_url, str(dest_path)],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            hf_url = f"https://huggingface.co/datasets/{repo_id}"
            result = subprocess.run(
                ["git", "clone", "--depth", "1", hf_url, str(dest_path)],
                capture_output=True,
                text=True,
                timeout=120
            )

        if result.returncode == 0 and commit_id:
            subprocess.run(
                ["git", "checkout", commit_id],
                cwd=str(dest_path),
                capture_output=True,
                timeout=30
            )

        return result.returncode == 0
    except Exception as e:
        logger.error(f"Failed to clone {repo_id}: {e}")
        return False


# ============================================================================
# MAIN SCRIPT
# ============================================================================

def load_dataset_and_analyze(
    output_path: str = "cls_analysis_results.json",
    max_repos: int = None,
    clone_dir: str = None,
    keep_clones: bool = False,
    num_workers: int = 4,
    framework_filter: str = None,
):
    """Main function to load dataset and analyze repos for CLS patterns"""

    from datasets import load_dataset

    logger.info("Loading dataset from HuggingFace...")
    ds = load_dataset("behavior-in-the-wild/cwv-bench-v0")

    train_data = ds['train']
    logger.info(f"Dataset loaded: {len(train_data)} repos")
    logger.info(f"Features: {train_data.features}")

    # Filter by framework if specified
    if framework_filter:
        train_data = train_data.filter(
            lambda x: x['FRAMEWORK'].lower() == framework_filter.lower()
        )
        logger.info(f"Filtered to {len(train_data)} {framework_filter} repos")

    # Limit number of repos if specified
    if max_repos:
        train_data = train_data.select(range(min(max_repos, len(train_data))))
        logger.info(f"Limited to {len(train_data)} repos")

    # Setup clone directory
    if clone_dir:
        clone_base = Path(clone_dir)
        clone_base.mkdir(parents=True, exist_ok=True)
        use_temp = False
    else:
        clone_base = Path(tempfile.mkdtemp(prefix="cls_analysis_"))
        use_temp = True

    logger.info(f"Clone directory: {clone_base}")

    # Initialize analyzer (verbose only in single-threaded mode)
    verbose_mode = num_workers == 1
    analyzer = CLSPatternAnalyzer(verbose=verbose_mode)

    results = []
    framework_stats = defaultdict(lambda: {
        'total': 0,
        'with_cls_risk': 0,
        'risk_high': 0,
        'risk_medium': 0,
        'risk_low': 0,
        'risk_none': 0,
        'total_score': 0,
    })

    results_lock = threading.Lock()
    stats_lock = threading.Lock()
    
    def process_repo(row_data):
        """Process a single repo - clone and analyze"""
        idx, row = row_data
        repo_id = row['REPO_ID']
        framework = row['FRAMEWORK']
        commit_id = row.get('COMMIT_ID', '')
        
        # Create clone destination
        safe_repo_name = repo_id.replace('/', '_')
        repo_dest = clone_base / safe_repo_name
        
        # Skip if already cloned
        if not repo_dest.exists():
            # Clone via git
            cloned = clone_repo_from_git(repo_id, commit_id, repo_dest)
            
            if not cloned:
                return RepoAnalysis(
                    repo_id=repo_id,
                    framework=framework,
                    commit_id=commit_id,
                    clone_path="",
                    error="Failed to clone"
                )
        
        # Analyze the repo
        repo_analysis = analyzer.analyze_repo(repo_dest, repo_id, framework, commit_id)
        
        # Clean up clone if not keeping
        if not keep_clones and not use_temp:
            shutil.rmtree(repo_dest, ignore_errors=True)
        
        return repo_analysis
    
    def update_stats(analysis):
        """Update framework statistics thread-safely"""
        with stats_lock:
            framework_stats[analysis.framework]['total'] += 1
            framework_stats[analysis.framework]['total_score'] += analysis.total_score
            if analysis.cls_risk_level != 'none':
                framework_stats[analysis.framework]['with_cls_risk'] += 1
            framework_stats[analysis.framework][f'risk_{analysis.cls_risk_level}'] += 1
    
    # Checkpoint settings
    checkpoint_interval = 5  # Save every N repos
    checkpoint_file = Path(output_path).with_suffix('.checkpoint.json')
    last_checkpoint = [0]  # Use list to allow mutation in nested function
    
    def save_checkpoint():
        """Save current progress to checkpoint file"""
        with results_lock:
            checkpoint_data = {
                'completed_repos': len(results),
                'total_repos': len(work_items) if 'work_items' in dir() else 0,
                'framework_stats': {k: dict(v) for k, v in framework_stats.items()},
                'results': [
                    {
                        'repo_id': r.repo_id,
                        'framework': r.framework,
                        'commit_id': r.commit_id,
                        'total_files_analyzed': r.total_files_analyzed,
                        'files_with_matches': r.files_with_matches,
                        'total_matches': r.total_matches,
                        'total_score': r.total_score,
                        'cls_risk_level': r.cls_risk_level,
                        'score_by_category': dict(r.score_by_category) if r.score_by_category else {},
                        'score_by_severity': dict(r.score_by_severity) if r.score_by_severity else {},
                        'matches_by_category': dict(r.matches_by_category) if r.matches_by_category else {},
                        'error': r.error,
                    }
                    for r in results
                ]
            }
        try:
            with open(checkpoint_file, 'w') as f:
                json.dump(checkpoint_data, f, indent=2)
            logger.info(f"Checkpoint saved: {len(results)} repos processed")
        except Exception as e:
            logger.warning(f"Failed to save checkpoint: {e}")
    
    def maybe_checkpoint():
        """Save checkpoint if interval reached"""
        with results_lock:
            current_count = len(results)
        if current_count - last_checkpoint[0] >= checkpoint_interval:
            save_checkpoint()
            last_checkpoint[0] = current_count
    
    try:
        # Prepare work items
        work_items = list(enumerate(train_data))
        
        if num_workers > 1:
            logger.info(f"Using {num_workers} parallel workers")
            
            with ThreadPoolExecutor(max_workers=num_workers) as executor:
                # Submit all tasks
                futures = {executor.submit(process_repo, item): item for item in work_items}
                
                # Process results with tqdm progress bar
                with tqdm(total=len(work_items), desc="Analyzing repos", unit="repo") as pbar:
                    for future in as_completed(futures):
                        item = futures[future]
                        idx, row = item
                        try:
                            analysis = future.result()
                            with results_lock:
                                results.append(analysis)
                            update_stats(analysis)
                            
                            # Update progress bar description
                            pbar.set_postfix({
                                'repo': row['REPO_ID'].split('/')[-1][:20],
                                'score': analysis.total_score,
                                'risk': analysis.cls_risk_level
                            })
                            maybe_checkpoint()
                        except Exception as e:
                            logger.error(f"Error processing {row['REPO_ID']}: {e}")
                            with results_lock:
                                results.append(RepoAnalysis(
                                    repo_id=row['REPO_ID'],
                                    framework=row['FRAMEWORK'],
                                    commit_id=row.get('COMMIT_ID', ''),
                                    clone_path="",
                                    error=str(e)
                                ))
                        finally:
                            pbar.update(1)
            # Final checkpoint after parallel processing
            save_checkpoint()
        else:
            # Single-threaded mode with tqdm
            for idx, row in tqdm(work_items, desc="Analyzing repos", unit="repo"):
                analysis = process_repo((idx, row))
                results.append(analysis)
                update_stats(analysis)
                maybe_checkpoint()
        
        # Final checkpoint
        save_checkpoint()
    
    finally:
        if use_temp and not keep_clones:
            shutil.rmtree(clone_base, ignore_errors=True)

    # Prepare output
    output = {
        'summary': {
            'total_repos': len(results),
            'repos_with_cls_risk': sum(1 for r in results if r.cls_risk_level != 'none'),
            'total_score_all_repos': sum(r.total_score for r in results),
            'avg_score_per_repo': sum(r.total_score for r in results) / max(1, len(results)),
            'framework_stats': dict(framework_stats),
        },
        'scoring_info': {
            'description': 'Each pattern match gets: base_score (1) * severity_multiplier',
            'severity_multipliers': SEVERITY_MULTIPLIERS,
            'categories': {
                'missing_dimensions': 'Images/videos/iframes without width/height',
                'dynamic_injection': 'DOM content injected after load',
                'font_loading': 'Web fonts causing FOIT/FOUT',
                'ads_embeds': 'Third-party ads and social embeds',
                'animations': 'CSS animations affecting layout',
                'lazy_loading': 'Lazy loading patterns',
                'skeleton_placeholder': 'Positive patterns (reduce CLS)',
            },
            'risk_interpretation': {
                'none': 'No CLS risk detected',
                'low': 'Minor CLS risk',
                'medium': 'Moderate CLS risk - review needed',
                'high': 'High CLS risk - likely poor CLS score',
            }
        },
        'pattern_catalog': {
            p.name: {
                'regex': p.regex,
                'category': p.category,
                'framework': p.framework,
                'severity': p.severity,
                'description': p.description,
                'score_per_match': p.score * SEVERITY_MULTIPLIERS.get(p.severity, 1),
            }
            for p in ALL_PATTERNS
        },
        'repos': [
            {
                'repo_id': r.repo_id,
                'framework': r.framework,
                'commit_id': r.commit_id,
                'total_files_analyzed': r.total_files_analyzed,
                'files_with_matches': r.files_with_matches,
                'total_matches': r.total_matches,
                # Risk assessment
                'cls_risk_level': r.cls_risk_level,
                'risk_factors': r.risk_factors,
                # Scoring
                'total_score': r.total_score,
                'score_by_category': r.score_by_category,
                'score_by_severity': r.score_by_severity,
                'score_by_pattern': r.score_by_pattern,
                'top_scoring_files': r.top_scoring_files,
                # Counts
                'matches_by_category': r.matches_by_category,
                'matches_by_severity': r.matches_by_severity,
                'matches_by_pattern': r.matches_by_pattern,
                'error': r.error,
                # File details with exact locations
                'file_analyses': [
                    {
                        'relative_path': fa.relative_path,
                        'total_score': fa.total_score,
                        'cls_risk_level': fa.cls_risk_level,
                        'score_by_category': fa.score_by_category,
                        'score_by_severity': fa.score_by_severity,
                        'issues': {
                            'missing_dimensions': fa.has_missing_dimensions,
                            'dynamic_injection': fa.has_dynamic_injection,
                            'font_issues': fa.has_font_issues,
                            'ads_embeds': fa.has_ads_embeds,
                        },
                        'matches': [
                            {
                                'pattern_name': m.pattern_name,
                                'category': m.category,
                                'severity': m.severity,
                                'score': m.score,
                                'line_number': m.line_number,
                                'line_content': m.line_content,
                                'matched_text': m.matched_text,
                            }
                            for m in fa.matches
                        ]
                    }
                    for fa in r.file_analyses
                ] if r.total_score > 0 else []
            }
            for r in results
        ],
        # Leaderboard: repos sorted by CLS risk score
        'leaderboard_highest_risk': sorted(
            [
                {
                    'repo_id': r.repo_id,
                    'framework': r.framework,
                    'total_score': r.total_score,
                    'cls_risk_level': r.cls_risk_level,
                    'top_issues': r.risk_factors[:3] if r.risk_factors else [],
                    'top_file': r.top_scoring_files[0] if r.top_scoring_files else None,
                }
                for r in results if r.total_score > 0
            ],
            key=lambda x: x['total_score'],
            reverse=True
        )
    }

    # Save results
    output_file = Path(output_path)
    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2)

    logger.info(f"\nResults saved to {output_file}")

    # Print summary
    print("\n" + "="*70)
    print("CLS (CUMULATIVE LAYOUT SHIFT) PATTERN ANALYSIS SUMMARY")
    print("="*70)
    print(f"\nTotal repos analyzed: {output['summary']['total_repos']}")
    print(f"Repos with CLS risk: {output['summary']['repos_with_cls_risk']}")
    print(f"Total score (all repos): {output['summary']['total_score_all_repos']}")
    print(f"Average score per repo: {output['summary']['avg_score_per_repo']:.1f}")

    print("\nBy Framework:")
    for fw, stats in sorted(framework_stats.items()):
        print(f"  {fw}:")
        print(f"    Total: {stats['total']}")
        print(f"    With CLS risk: {stats['with_cls_risk']} ({100*stats['with_cls_risk']/max(1,stats['total']):.1f}%)")
        print(f"    Risk - High: {stats['risk_high']}, Medium: {stats['risk_medium']}, Low: {stats['risk_low']}")
        print(f"    Total Score: {stats['total_score']}")

    print("\n" + "-"*70)
    print("TOP 10 REPOS BY CLS RISK SCORE:")
    print("-"*70)
    for i, entry in enumerate(output['leaderboard_highest_risk'][:10], 1):
        top_file = entry['top_file']['file'] if entry['top_file'] else 'N/A'
        top_file_score = entry['top_file']['score'] if entry['top_file'] else 0
        top_issues = ', '.join([f['factor'] for f in entry['top_issues']]) if entry['top_issues'] else 'Various'
        print(f"  {i}. {entry['repo_id']}")
        print(f"     Score: {entry['total_score']} | Risk: {entry['cls_risk_level']} | Framework: {entry['framework']}")
        print(f"     Top issues: {top_issues}")
        print(f"     Top file: {top_file} (score: {top_file_score})")

    print("\n" + "-"*70)
    print("CLS ISSUE CATEGORIES FOUND:")
    print("-"*70)
    all_category_scores = defaultdict(int)
    for r in results:
        for cat, score in r.score_by_category.items():
            all_category_scores[cat] += score
    
    for cat, score in sorted(all_category_scores.items(), key=lambda x: x[1], reverse=True):
        print(f"  {cat}: {score} points")

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Analyze CWV benchmark repos for CLS-producing patterns"
    )
    parser.add_argument(
        '--output', '-o',
        default='cls_analysis_results.json',
        help='Output file path (JSON)'
    )
    parser.add_argument(
        '--max-repos', '-n',
        type=int,
        default=None,
        help='Maximum number of repos to analyze'
    )
    parser.add_argument(
        '--clone-dir', '-d',
        default=None,
        help='Directory to clone repos into (uses temp dir if not specified)'
    )
    parser.add_argument(
        '--keep-clones', '-k',
        action='store_true',
        help='Keep cloned repos after analysis'
    )
    parser.add_argument(
        '--framework', '-f',
        default=None,
        help='Filter to specific framework (e.g., react, vue, nextjs)'
    )
    parser.add_argument(
        '--workers', '-w',
        type=int,
        default=4,
        help='Number of parallel workers'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    load_dataset_and_analyze(
        output_path=args.output,
        max_repos=args.max_repos,
        clone_dir=args.clone_dir,
        keep_clones=args.keep_clones,
        num_workers=args.workers,
        framework_filter=args.framework,
    )


if __name__ == '__main__':
    main()
