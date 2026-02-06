#!/usr/bin/env python3
"""
INP Pattern Analyzer for CWV Benchmark Repos

This script:
1. Loads the HuggingFace dataset (behavior-in-the-wild/cwv-bench-v0)
2. Clones each repo
3. Analyzes for interactive patterns that can produce non-zero INP
4. Outputs findings as JSON/CSV

Usage:
    python inp_pattern_analyzer.py --output results.json --max-repos 100
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
# INP PATTERN DEFINITIONS
# ============================================================================

@dataclass
class INPPattern:
    """A pattern that can contribute to INP"""
    name: str
    regex: str
    category: str  # 'discrete_input', 'paint_trigger', 'layout_thrash', 'sync_work', 'framework_specific'
    framework: str  # 'global', 'react', 'vue', 'nextjs', 'static', 'quarto', etc.
    severity: str  # 'high', 'medium', 'low'
    description: str
    score: int = 1  # Base score for this pattern (can be weighted by severity)


# Severity to score multiplier mapping
SEVERITY_MULTIPLIERS = {
    'high': 3,
    'medium': 2,
    'low': 1,
}


# Global patterns (apply to all frameworks)
GLOBAL_PATTERNS = [
    # === DISCRETE INPUT EVENT REGISTRATION (MANDATORY for INP) ===
    INPPattern(
        name="addEventListener_discrete",
        regex=r"addEventListener\s*\(\s*['\"]?(click|keydown|keyup|keypress|input|change|submit|pointerdown|pointerup|touchstart|touchend)['\"]?",
        category="discrete_input",
        framework="global",
        severity="high",
        description="addEventListener for discrete input events"
    ),
    INPPattern(
        name="on_event_assignment",
        regex=r"\.(onclick|onkeydown|onkeyup|onkeypress|oninput|onchange|onsubmit)\s*=",
        category="discrete_input",
        framework="global",
        severity="high",
        description="Direct on-event property assignment"
    ),
    INPPattern(
        name="html_inline_handler",
        regex=r"(onclick|onkeydown|onkeyup|onkeypress|oninput|onchange|onsubmit)\s*=",
        category="discrete_input",
        framework="global",
        severity="high",
        description="Inline HTML event handlers"
    ),
    
    # === DOM MUTATION INSIDE HANDLERS (PAINT TRIGGER) ===
    INPPattern(
        name="classList_mutation",
        regex=r"classList\.(add|remove|toggle)",
        category="paint_trigger",
        framework="global",
        severity="medium",
        description="classList manipulation (triggers paint)"
    ),
    INPPattern(
        name="attribute_mutation",
        regex=r"(setAttribute|removeAttribute)\s*\(",
        category="paint_trigger",
        framework="global",
        severity="medium",
        description="Attribute manipulation"
    ),
    INPPattern(
        name="innerHTML_mutation",
        regex=r"(innerHTML|outerHTML|textContent)\s*=",
        category="paint_trigger",
        framework="global",
        severity="high",
        description="innerHTML/textContent assignment (triggers reflow)"
    ),
    INPPattern(
        name="dom_child_manipulation",
        regex=r"(appendChild|removeChild|replaceChild|insertBefore|insertAdjacentElement|insertAdjacentHTML)",
        category="paint_trigger",
        framework="global",
        severity="high",
        description="DOM child node manipulation"
    ),
    INPPattern(
        name="style_mutation",
        regex=r"\.style\.[a-zA-Z]+\s*=",
        category="paint_trigger",
        framework="global",
        severity="medium",
        description="Direct style property mutation"
    ),
    
    # === FORCED LAYOUT / STYLE RECALCULATION (HIGH INP RISK) ===
    INPPattern(
        name="layout_thrash_read",
        regex=r"(getBoundingClientRect|offsetHeight|offsetWidth|offsetTop|offsetLeft|scrollHeight|scrollWidth|scrollTop|scrollLeft|clientHeight|clientWidth)",
        category="layout_thrash",
        framework="global",
        severity="high",
        description="Layout property read (forces sync layout if DOM dirty)"
    ),
    INPPattern(
        name="getComputedStyle",
        regex=r"getComputedStyle\s*\(",
        category="layout_thrash",
        framework="global",
        severity="high",
        description="getComputedStyle (forces style recalculation)"
    ),
    
    # === SYNCHRONOUS JS WORK IN HANDLERS ===
    INPPattern(
        name="sync_loop_for",
        regex=r"for\s*\([^)]+\)\s*\{",
        category="sync_work",
        framework="global",
        severity="low",
        description="For loop (potential sync work)"
    ),
    INPPattern(
        name="sync_loop_while",
        regex=r"while\s*\([^)]+\)\s*\{",
        category="sync_work",
        framework="global",
        severity="low",
        description="While loop (potential sync work)"
    ),
    INPPattern(
        name="array_operations",
        regex=r"(Array\.from|\.map\s*\(|\.reduce\s*\(|\.filter\s*\(|\.forEach\s*\()",
        category="sync_work",
        framework="global",
        severity="low",
        description="Array iteration methods"
    ),
    INPPattern(
        name="json_operations",
        regex=r"JSON\.(parse|stringify)\s*\(",
        category="sync_work",
        framework="global",
        severity="medium",
        description="JSON parse/stringify (can be slow for large data)"
    ),
    
    # === JQUERY PATTERNS (common in static sites/themes) ===
    INPPattern(
        name="jquery_event_binding",
        regex=r"\$\s*\(['\"][^'\"]+['\"]\)\s*\.\s*(on|click|change|submit|keydown|keyup|focus|blur)\s*\(",
        category="discrete_input",
        framework="global",
        severity="high",
        description="jQuery event binding"
    ),
    INPPattern(
        name="jquery_dom_manipulation",
        regex=r"\$\s*\([^)]+\)\s*\.\s*(html|append|prepend|after|before|remove|empty|addClass|removeClass|toggleClass|css|attr|prop)\s*\(",
        category="paint_trigger",
        framework="global",
        severity="medium",
        description="jQuery DOM manipulation"
    ),
]

# React-specific patterns
REACT_PATTERNS = [
    INPPattern(
        name="react_event_props",
        regex=r"on(Click|Change|Submit|KeyDown|KeyUp|KeyPress|Input|PointerDown|PointerUp|TouchStart|TouchEnd|Focus|Blur)\s*=\s*\{",
        category="discrete_input",
        framework="react",
        severity="high",
        description="React event handler props"
    ),
    INPPattern(
        name="react_setState",
        regex=r"setState\s*\(",
        category="paint_trigger",
        framework="react",
        severity="high",
        description="React setState (triggers re-render)"
    ),
    INPPattern(
        name="react_useState",
        regex=r"useState\s*\(",
        category="paint_trigger",
        framework="react",
        severity="medium",
        description="React useState hook"
    ),
    INPPattern(
        name="react_state_setter",
        regex=r"set[A-Z][a-zA-Z0-9_]*\s*\([^)]*\)",
        category="paint_trigger",
        framework="react",
        severity="high",
        description="React state setter function call"
    ),
    INPPattern(
        name="react_controlled_input",
        regex=r"<input[^>]+value\s*=\s*\{",
        category="discrete_input",
        framework="react",
        severity="high",
        description="React controlled input (guaranteed INP on typing)"
    ),
    INPPattern(
        name="react_controlled_textarea",
        regex=r"<textarea[^>]+value\s*=\s*\{",
        category="discrete_input",
        framework="react",
        severity="high",
        description="React controlled textarea"
    ),
    INPPattern(
        name="react_useLayoutEffect",
        regex=r"useLayoutEffect\s*\(",
        category="layout_thrash",
        framework="react",
        severity="high",
        description="useLayoutEffect (blocks paint)"
    ),
    INPPattern(
        name="react_forceUpdate",
        regex=r"forceUpdate\s*\(",
        category="paint_trigger",
        framework="react",
        severity="high",
        description="React forceUpdate"
    ),
    INPPattern(
        name="react_useReducer",
        regex=r"useReducer\s*\(",
        category="paint_trigger",
        framework="react",
        severity="medium",
        description="React useReducer hook"
    ),
]

# Next.js specific patterns (additive to React)
NEXTJS_PATTERNS = [
    INPPattern(
        name="nextjs_router_push",
        regex=r"router\.push\s*\(",
        category="paint_trigger",
        framework="nextjs",
        severity="high",
        description="Next.js client navigation"
    ),
    INPPattern(
        name="nextjs_router_replace",
        regex=r"router\.replace\s*\(",
        category="paint_trigger",
        framework="nextjs",
        severity="high",
        description="Next.js client navigation (replace)"
    ),
    INPPattern(
        name="nextjs_useRouter",
        regex=r"useRouter\s*\(",
        category="paint_trigger",
        framework="nextjs",
        severity="medium",
        description="Next.js useRouter hook"
    ),
    INPPattern(
        name="nextjs_server_action_form",
        regex=r"<form[^>]+action\s*=\s*\{",
        category="discrete_input",
        framework="nextjs",
        severity="high",
        description="Next.js App Router server action form"
    ),
    INPPattern(
        name="nextjs_dynamic_import",
        regex=r"import\s*\(\s*['\"]",
        category="sync_work",
        framework="nextjs",
        severity="medium",
        description="Dynamic import (may block on interaction)"
    ),
    INPPattern(
        name="nextjs_link_component",
        regex=r"<Link[^>]+onClick",
        category="discrete_input",
        framework="nextjs",
        severity="medium",
        description="Next.js Link with onClick handler"
    ),
]

# Vue-specific patterns
VUE_PATTERNS = [
    INPPattern(
        name="vue_event_directive",
        regex=r"@(click|submit|change|input|keydown|keyup|keypress|focus|blur|touchstart|touchend)(\.[a-z]+)*\s*=",
        category="discrete_input",
        framework="vue",
        severity="high",
        description="Vue event directive (@click, etc.)"
    ),
    INPPattern(
        name="vue_v_on_directive",
        regex=r"v-on:(click|submit|change|input|keydown|keyup|keypress)\s*=",
        category="discrete_input",
        framework="vue",
        severity="high",
        description="Vue v-on directive"
    ),
    INPPattern(
        name="vue_v_model",
        regex=r"v-model(\.[a-z]+)*\s*=",
        category="discrete_input",
        framework="vue",
        severity="high",
        description="Vue v-model (controlled input, guaranteed INP)"
    ),
    INPPattern(
        name="vue_ref_value_mutation",
        regex=r"\.value\s*=",
        category="paint_trigger",
        framework="vue",
        severity="high",
        description="Vue ref value mutation"
    ),
    INPPattern(
        name="vue_reactive_mutation",
        regex=r"this\.[a-zA-Z_$][a-zA-Z0-9_$]*\s*=",
        category="paint_trigger",
        framework="vue",
        severity="medium",
        description="Vue reactive property mutation"
    ),
    INPPattern(
        name="vue_watch",
        regex=r"watch\s*\(",
        category="paint_trigger",
        framework="vue",
        severity="medium",
        description="Vue watch (may trigger on interaction)"
    ),
    INPPattern(
        name="vue_computed",
        regex=r"computed\s*\(",
        category="paint_trigger",
        framework="vue",
        severity="low",
        description="Vue computed property"
    ),
    INPPattern(
        name="vue_emit",
        regex=r"\$emit\s*\(|emit\s*\(",
        category="paint_trigger",
        framework="vue",
        severity="medium",
        description="Vue event emission"
    ),
]

# Static HTML / SSG patterns (Jekyll, Hugo, Hexo, Pelican, Quarto, Flask)
STATIC_PATTERNS = [
    INPPattern(
        name="html_button_handler",
        regex=r"<button[^>]+on(click|keydown|keyup)\s*=",
        category="discrete_input",
        framework="static",
        severity="high",
        description="Button with inline event handler"
    ),
    INPPattern(
        name="html_input_handler",
        regex=r"<input[^>]+on(click|change|input|keydown|keyup|focus|blur)\s*=",
        category="discrete_input",
        framework="static",
        severity="high",
        description="Input with inline event handler"
    ),
    INPPattern(
        name="html_form_handler",
        regex=r"<form[^>]+on(submit|change)\s*=",
        category="discrete_input",
        framework="static",
        severity="high",
        description="Form with inline event handler"
    ),
    INPPattern(
        name="html_select_handler",
        regex=r"<select[^>]+on(change|click)\s*=",
        category="discrete_input",
        framework="static",
        severity="high",
        description="Select with inline event handler"
    ),
    INPPattern(
        name="dom_query_selector",
        regex=r"document\.(querySelector|querySelectorAll|getElementById|getElementsByClassName|getElementsByTagName)\s*\(",
        category="paint_trigger",
        framework="static",
        severity="low",
        description="DOM query (often precedes event binding)"
    ),
    INPPattern(
        name="prevent_default",
        regex=r"preventDefault\s*\(",
        category="discrete_input",
        framework="static",
        severity="medium",
        description="preventDefault (indicates JS-handled form/event)"
    ),
]

# Quarto-specific patterns
QUARTO_PATTERNS = [
    INPPattern(
        name="quarto_panel_tabset",
        regex=r"panel-tabset|\.panel-tabset",
        category="discrete_input",
        framework="quarto",
        severity="high",
        description="Quarto panel-tabset (JS-driven tabs)"
    ),
    INPPattern(
        name="quarto_callout",
        regex=r"\.callout|callout-",
        category="paint_trigger",
        framework="quarto",
        severity="medium",
        description="Quarto callout (may have interactive elements)"
    ),
    INPPattern(
        name="quarto_js",
        regex=r"quarto\.js|quarto-nav|quarto-search",
        category="discrete_input",
        framework="quarto",
        severity="high",
        description="Quarto JS components"
    ),
    INPPattern(
        name="quarto_observable",
        regex=r"ojs-|observable",
        category="discrete_input",
        framework="quarto",
        severity="high",
        description="Quarto Observable JS (reactive)"
    ),
]

# Combine all patterns
ALL_PATTERNS = (
    GLOBAL_PATTERNS + 
    REACT_PATTERNS + 
    NEXTJS_PATTERNS + 
    VUE_PATTERNS + 
    STATIC_PATTERNS + 
    QUARTO_PATTERNS
)

# File extensions to analyze
ANALYZABLE_EXTENSIONS = {
    '.js', '.jsx', '.ts', '.tsx',  # JavaScript/TypeScript
    '.vue', '.svelte',              # Vue/Svelte
    '.html', '.htm',                # HTML
    '.md', '.mdx', '.qmd',          # Markdown (may contain JS/HTML)
    '.liquid', '.njk', '.ejs',      # Template engines
    '.hbs', '.handlebars',
    '.jinja', '.jinja2', '.j2',
    '.erb',
}

# Directories to skip
SKIP_DIRS = {
    'node_modules', '.git', 'dist', 'build', '.next', '.nuxt',
    '__pycache__', '.pytest_cache', '.venv', 'venv', 'env',
    'vendor', 'bower_components', '.cache', 'coverage',
    'public/assets', 'static/assets',  # Often compiled/minified
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
    score: int = 1  # Score for this specific match


@dataclass
class FileAnalysis:
    """Analysis results for a single file"""
    file_path: str
    relative_path: str
    matches: list = field(default_factory=list)
    has_discrete_input: bool = False
    has_paint_trigger: bool = False
    inp_likely: bool = False
    total_score: int = 0  # Sum of all pattern scores in this file
    score_by_category: dict = field(default_factory=dict)
    score_by_severity: dict = field(default_factory=dict)


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
    matches_by_category: dict = field(default_factory=dict)
    matches_by_severity: dict = field(default_factory=dict)
    matches_by_pattern: dict = field(default_factory=dict)
    files_with_inp_likely: list = field(default_factory=list)
    inp_confidence: str = "none"  # none, low, medium, high
    file_analyses: list = field(default_factory=list)
    error: Optional[str] = None
    # Scoring
    total_score: int = 0  # Sum of all pattern scores in this repo
    score_by_category: dict = field(default_factory=dict)
    score_by_severity: dict = field(default_factory=dict)
    score_by_pattern: dict = field(default_factory=dict)
    score_by_file: dict = field(default_factory=dict)  # file_path -> score
    top_scoring_files: list = field(default_factory=list)  # Top files by score


# ============================================================================
# ANALYZER
# ============================================================================

class INPPatternAnalyzer:
    """Analyzes repositories for INP-producing patterns"""
    
    def __init__(self, patterns: list[INPPattern] = None, verbose: bool = False):
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
        """Analyze a single file for INP patterns"""
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
                    line_content = lines[line_start].strip()[:200]  # Truncate long lines
                    
                    # Calculate score for this match (base score * severity multiplier)
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
                    )
                    analysis.matches.append(pattern_match)
                    
                    # Update file scores
                    analysis.total_score += match_score
                    analysis.score_by_category[pattern.category] = analysis.score_by_category.get(pattern.category, 0) + match_score
                    analysis.score_by_severity[pattern.severity] = analysis.score_by_severity.get(pattern.severity, 0) + match_score
                    
                    # Track categories
                    if pattern.category == 'discrete_input':
                        analysis.has_discrete_input = True
                    elif pattern.category in ('paint_trigger', 'layout_thrash'):
                        analysis.has_paint_trigger = True
            
            # INP is likely if we have BOTH discrete input AND paint trigger
            analysis.inp_likely = analysis.has_discrete_input and analysis.has_paint_trigger
            
        except Exception as e:
            logger.debug(f"Error analyzing {file_path}: {e}")
        
        return analysis
    
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
        
        try:
            # Find all analyzable files
            if self.verbose:
                logger.info(f"    [1/4] Scanning for analyzable files...")
            files_to_analyze = []
            for file_path in repo_path.rglob('*'):
                if file_path.is_file() and self.should_analyze_file(file_path):
                    files_to_analyze.append(file_path)
            
            analysis.total_files_analyzed = len(files_to_analyze)
            if self.verbose:
                logger.info(f"    [2/4] Found {len(files_to_analyze)} files to analyze")
            
            # Analyze each file
            if self.verbose:
                logger.info(f"    [3/4] Running pattern matching...")
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
                
                if file_analysis.inp_likely:
                    analysis.files_with_inp_likely.append(file_analysis.relative_path)
            
            # Determine INP confidence
            if self.verbose:
                logger.info(f"    [4/4] Calculating INP confidence...")
            analysis.inp_confidence = self._calculate_inp_confidence(analysis)
            
            # Get top scoring files (sorted by score descending)
            analysis.top_scoring_files = sorted(
                [
                    {'file': path, 'score': score}
                    for path, score in analysis.score_by_file.items()
                ],
                key=lambda x: x['score'],
                reverse=True
            )[:20]  # Top 20 files
            
            # Convert defaultdicts to regular dicts for JSON serialization
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
    
    def _calculate_inp_confidence(self, analysis: RepoAnalysis) -> str:
        """Calculate confidence level that repo produces INP"""
        if not analysis.matches_by_category:
            return "none"
        
        has_discrete = analysis.matches_by_category.get('discrete_input', 0) > 0
        has_paint = (
            analysis.matches_by_category.get('paint_trigger', 0) > 0 or
            analysis.matches_by_category.get('layout_thrash', 0) > 0
        )
        high_severity = analysis.matches_by_severity.get('high', 0)
        
        if has_discrete and has_paint:
            if high_severity > 10:
                return "high"
            elif high_severity > 3:
                return "medium"
            else:
                return "low"
        elif has_discrete:
            return "low"
        
        return "none"


# ============================================================================
# REPO CLONING
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
            # Extract to temp location first
            temp_extract = dest_path.parent / f"{dest_path.name}_temp"
            zf.extractall(temp_extract)
            
            # Find the actual repo folder (usually nested)
            extracted_dirs = list(temp_extract.iterdir())
            if len(extracted_dirs) == 1 and extracted_dirs[0].is_dir():
                # Move contents up
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
        # Try GitHub first
        github_url = f"https://github.com/{repo_id}.git"
        
        result = subprocess.run(
            ["git", "clone", "--depth", "1", github_url, str(dest_path)],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        if result.returncode != 0:
            # Try Hugging Face
            hf_url = f"https://huggingface.co/datasets/{repo_id}"
            result = subprocess.run(
                ["git", "clone", "--depth", "1", hf_url, str(dest_path)],
                capture_output=True,
                text=True,
                timeout=120
            )
        
        if result.returncode == 0 and commit_id:
            # Checkout specific commit if provided
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
    output_path: str = "inp_analysis_results.json",
    max_repos: int = None,
    clone_dir: str = None,
    keep_clones: bool = False,
    num_workers: int = 4,
    framework_filter: str = None,
):
    """Main function to load dataset and analyze repos"""
    
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
        clone_base = Path(tempfile.mkdtemp(prefix="inp_analysis_"))
        use_temp = True
    
    logger.info(f"Clone directory: {clone_base}")
    
    # Initialize analyzer (verbose only in single-threaded mode)
    verbose_mode = num_workers == 1
    analyzer = INPPatternAnalyzer(verbose=verbose_mode)
    
    results = []
    results_lock = threading.Lock()
    framework_stats = defaultdict(lambda: {
        'total': 0,
        'with_inp': 0,
        'confidence_high': 0,
        'confidence_medium': 0,
        'confidence_low': 0,
        'confidence_none': 0,
    })
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
        analysis = analyzer.analyze_repo(repo_dest, repo_id, framework, commit_id)
        
        # Clean up clone if not keeping
        if not keep_clones and not use_temp:
            shutil.rmtree(repo_dest, ignore_errors=True)
        
        return analysis
    
    def update_stats(analysis):
        """Update framework statistics thread-safely"""
        with stats_lock:
            framework_stats[analysis.framework]['total'] += 1
            if analysis.inp_confidence != 'none':
                framework_stats[analysis.framework]['with_inp'] += 1
            framework_stats[analysis.framework][f'confidence_{analysis.inp_confidence}'] += 1
    
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
                        'inp_confidence': r.inp_confidence,
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
                                'conf': analysis.inp_confidence
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
                repo_id = row['REPO_ID']
                framework = row['FRAMEWORK']
                commit_id = row.get('COMMIT_ID', '')
                
                analysis = process_repo((idx, row))
                results.append(analysis)
                update_stats(analysis)
                maybe_checkpoint()
        
        # Final checkpoint
        save_checkpoint()
    
    finally:
        # Clean up temp directory
        if use_temp and not keep_clones:
            shutil.rmtree(clone_base, ignore_errors=True)
    
    # Prepare output
    output = {
        'summary': {
            'total_repos': len(results),
            'repos_with_inp': sum(1 for r in results if r.inp_confidence != 'none'),
            'total_score_all_repos': sum(r.total_score for r in results),
            'avg_score_per_repo': sum(r.total_score for r in results) / max(1, len(results)),
            'framework_stats': dict(framework_stats),
        },
        'scoring_info': {
            'description': 'Each pattern match gets: base_score (1) * severity_multiplier',
            'severity_multipliers': SEVERITY_MULTIPLIERS,
            'score_interpretation': {
                '0-10': 'Minimal interactivity',
                '11-50': 'Low interactivity',
                '51-150': 'Moderate interactivity',
                '151-500': 'High interactivity',
                '500+': 'Very high interactivity (likely SPA)',
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
                'files_with_inp_likely': r.files_with_inp_likely,
                'inp_confidence': r.inp_confidence,
                'error': r.error,
                # Include detailed file analyses with scores and exact locations
                'file_analyses': [
                    {
                        'relative_path': fa.relative_path,
                        'total_score': fa.total_score,
                        'score_by_category': fa.score_by_category,
                        'score_by_severity': fa.score_by_severity,
                        'has_discrete_input': fa.has_discrete_input,
                        'has_paint_trigger': fa.has_paint_trigger,
                        'inp_likely': fa.inp_likely,
                        'matches': [
                            {
                                'pattern_name': m.pattern_name,
                                'category': m.category,
                                'severity': m.severity,
                                'score': m.score,
                                'line_number': m.line_number,
                                'line_content': m.line_content,
                            }
                            for m in fa.matches
                        ]
                    }
                    for fa in r.file_analyses
                ] if r.total_score > 0 else []
            }
            for r in results
        ],
        # Leaderboard: repos sorted by score
        'leaderboard': sorted(
            [
                {
                    'repo_id': r.repo_id,
                    'framework': r.framework,
                    'total_score': r.total_score,
                    'total_matches': r.total_matches,
                    'inp_confidence': r.inp_confidence,
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
    print("\n" + "="*60)
    print("INP PATTERN ANALYSIS SUMMARY")
    print("="*60)
    print(f"\nTotal repos analyzed: {output['summary']['total_repos']}")
    print(f"Repos with INP patterns: {output['summary']['repos_with_inp']}")
    print(f"Total score (all repos): {output['summary']['total_score_all_repos']}")
    print(f"Average score per repo: {output['summary']['avg_score_per_repo']:.1f}")
    print("\nBy Framework:")
    for fw, stats in sorted(framework_stats.items()):
        print(f"  {fw}:")
        print(f"    Total: {stats['total']}")
        print(f"    With INP: {stats['with_inp']} ({100*stats['with_inp']/max(1,stats['total']):.1f}%)")
        print(f"    Confidence - High: {stats['confidence_high']}, Medium: {stats['confidence_medium']}, Low: {stats['confidence_low']}")
    
    print("\n" + "-"*60)
    print("TOP 10 REPOS BY SCORE:")
    print("-"*60)
    for i, entry in enumerate(output['leaderboard'][:10], 1):
        top_file = entry['top_file']['file'] if entry['top_file'] else 'N/A'
        top_file_score = entry['top_file']['score'] if entry['top_file'] else 0
        print(f"  {i}. {entry['repo_id']}")
        print(f"     Score: {entry['total_score']} | Matches: {entry['total_matches']} | Framework: {entry['framework']}")
        print(f"     Top file: {top_file} (score: {top_file_score})")
    
    return output


def main():
    parser = argparse.ArgumentParser(
        description="Analyze CWV benchmark repos for INP-producing patterns"
    )
    parser.add_argument(
        '--output', '-o',
        default='inp_analysis_results.json',
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
