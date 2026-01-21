import json
import subprocess
import tempfile
import shutil
import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------
# Hexo detection (index.html ONLY)
# ------------------------

HEX0_META_PREFIX = '<meta name="generator" content="hexo'  # already lowercase
HEX0_KEYWORDS = [
    "powered by hexo",
    "由 hexo",
    "hexo"
]

def detect_hexo(root: Path):
    index_html = root / "index.html"
    if not index_html.exists():
        return False, None

    try:
        text = index_html.read_text(errors="ignore").lower()
    except Exception:
        return False, None

    if HEX0_META_PREFIX in text:
        return True, "index.html contains <meta name='generator' content='Hexo...'>"

    for kw in HEX0_KEYWORDS:
        if kw in text:
            return True, f"index.html contains keyword '{kw}'"

    return False, None


# ------------------------
# Jekyll detection (Gemfile / _config.yml ONLY)
# ------------------------

def detect_jekyll(root: Path):
    try:
        gemfile = root / "Gemfile"
        config_yml = root / "_config.yml"

        if gemfile.exists():
            txt = gemfile.read_text(encoding="utf-8", errors="ignore").lower()
            if "jekyll" in txt:
                return True, "Gemfile references 'jekyll'"

        if config_yml.exists():
            txt = config_yml.read_text(encoding="utf-8", errors="ignore").lower()
            if "jekyll" in txt:
                return True, "_config.yml references 'jekyll'"

    except Exception:
        pass

    return False, None


# ------------------------
# Static HTML detection (plain HTML sites, no SSG)
# ------------------------

# Files that indicate a framework/build tool (NOT static HTML)
SSG_BUILD_FILES = [
    "package.json",      # Node-based frameworks (Next.js, Gatsby, etc.)
    "Gemfile",           # Ruby-based (Jekyll)
    "_config.yml",       # Jekyll config
    "config.toml",       # Hugo config
    "config.yaml",       # Hugo config
    "hugo.toml",         # Hugo config
    "hugo.yaml",         # Hugo config
    "gatsby-config.js",  # Gatsby
    "gatsby-config.ts",  # Gatsby (TypeScript)
    "next.config.js",    # Next.js
    "next.config.mjs",   # Next.js
    "nuxt.config.js",    # Nuxt.js
    "nuxt.config.ts",    # Nuxt.js
    "astro.config.mjs",  # Astro
    "svelte.config.js",  # SvelteKit
    "vite.config.js",    # Vite
    "vite.config.ts",    # Vite
    "webpack.config.js", # Webpack
    "rollup.config.js",  # Rollup
    "eleventy.config.js", # Eleventy (11ty)
    ".eleventy.js",      # Eleventy
    "mkdocs.yml",        # MkDocs
    "docusaurus.config.js", # Docusaurus
    "pelican.py",        # Pelican (Python)
    "pelicanconf.py",    # Pelican config
]

# Directories that indicate a framework (NOT static HTML)
SSG_BUILD_DIRS = [
    "node_modules",
    "_layouts",       # Jekyll
    "_includes",      # Jekyll
    "_posts",         # Jekyll
    "_sass",          # Jekyll
    "archetypes",     # Hugo
    "layouts",        # Hugo (with content dir)
    "themes",         # Hugo/Hexo themes
    "scaffolds",      # Hexo
    "source/_posts",  # Hexo
    ".next",          # Next.js build output
    ".nuxt",          # Nuxt.js build output
    ".gatsby-cache",  # Gatsby cache
]

# Patterns in HTML that suggest static HTML (positive indicators)
STATIC_HTML_INDICATORS = [
    # Simple relative paths to assets
    'href="styles.css"',
    'href="style.css"',
    'href="css/style.css"',
    'href="./css/',
    'src="script.js"',
    'src="js/main.js"',
    'src="./js/',
    # No templating/framework markers
    'href="index.html"',
    'href="about.html"',
    'href="contact.html"',
]

# Extensions that indicate static assets
STATIC_ASSET_EXTENSIONS = {".html", ".htm", ".css", ".js", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico"}


def detect_static_html(root: Path):
    """
    Detect if a repo is a plain static HTML site (no SSG framework).
    
    Criteria:
    1. Has index.html at root
    2. Does NOT have common SSG config files
    3. Has typical static site structure (html, css, js files)
    """
    index_html = root / "index.html"
    if not index_html.exists():
        return False, None

    # Check for SSG build files (if present, NOT static HTML)
    for build_file in SSG_BUILD_FILES:
        if (root / build_file).exists():
            return False, None

    # Check for SSG directories (if present, NOT static HTML)
    for build_dir in SSG_BUILD_DIRS:
        if (root / build_dir).exists() and (root / build_dir).is_dir():
            return False, None

    # Count HTML files and static assets
    html_files = list(root.glob("*.html")) + list(root.glob("**/*.html"))
    css_files = list(root.glob("*.css")) + list(root.glob("**/*.css"))
    
    # Must have at least index.html
    if not html_files:
        return False, None

    evidence_parts = []
    evidence_parts.append(f"index.html exists at root")
    evidence_parts.append(f"found {len(html_files)} HTML file(s)")
    
    if css_files:
        evidence_parts.append(f"found {len(css_files)} CSS file(s)")

    # Check for static HTML indicators in index.html
    try:
        html_content = index_html.read_text(errors="ignore").lower()
        
        # Negative check: meta generator tags for common SSGs
        ssg_generators = [
            "generator\" content=\"hexo",
            "generator\" content=\"jekyll",
            "generator\" content=\"hugo",
            "generator\" content=\"gatsby",
            "generator\" content=\"next.js",
            "generator\" content=\"nuxt",
            "generator\" content=\"eleventy",
            "generator\" content=\"docusaurus",
        ]
        for gen in ssg_generators:
            if gen in html_content:
                return False, None

        # Positive indicators
        static_hints_found = []
        for indicator in STATIC_HTML_INDICATORS:
            if indicator.lower() in html_content:
                static_hints_found.append(indicator)
        
        if static_hints_found:
            evidence_parts.append(f"static patterns: {static_hints_found[:3]}")  # limit to 3

    except Exception:
        pass

    # Check for typical static site file structure
    has_css_dir = (root / "css").is_dir() or (root / "styles").is_dir()
    has_js_dir = (root / "js").is_dir() or (root / "scripts").is_dir()
    has_images_dir = (root / "images").is_dir() or (root / "img").is_dir() or (root / "assets").is_dir()
    
    if has_css_dir:
        evidence_parts.append("has css/ or styles/ directory")
    if has_js_dir:
        evidence_parts.append("has js/ or scripts/ directory")
    if has_images_dir:
        evidence_parts.append("has images/img/assets directory")

    # Final decision: if we have index.html with no SSG markers, it's static HTML
    return True, "; ".join(evidence_parts)


# ------------------------
# Git utilities
# ------------------------

def git_clone(repo_url: str, dst: Path) -> bool:
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dst)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        return dst.exists() and any(dst.iterdir())
    except Exception:
        return False


# ------------------------
# Single repo processor (for threading)
# ------------------------

def process_single_repo(obj: dict, repo_field: str):
    """Process a single repo and return result dict or None."""
    repo = obj.get(repo_field)
    
    if not repo:
        return None, "missing_field"

    repo_url = (
        repo if repo.startswith("http")
        else f"https://github.com/{repo}.git"
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="ssg_detect_"))

    try:
        if not git_clone(repo_url, tmpdir):
            return None, "clone_failed"

        frameworks = []
        evidence = {}

        hexo_ok, hexo_ev = detect_hexo(tmpdir)
        if hexo_ok:
            frameworks.append("Hexo")
            evidence["Hexo"] = hexo_ev

        jekyll_ok, jekyll_ev = detect_jekyll(tmpdir)
        if jekyll_ok:
            frameworks.append("Jekyll")
            evidence["Jekyll"] = jekyll_ev

        # Only check for Static HTML if no SSG framework was detected
        # This prevents conflicts where a built SSG site could be misclassified
        if not frameworks:
            static_ok, static_ev = detect_static_html(tmpdir)
            if static_ok:
                frameworks.append("Static HTML")
                evidence["Static HTML"] = static_ev

        if frameworks:
            obj["framework"] = ",".join(frameworks)
            obj["framework_evidence"] = evidence
            return obj, "found"
        else:
            return None, "no_framework"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------
# Main JSONL processor (multithreaded)
# ------------------------

def process_jsonl(
    input_path: str,
    output_path: str,
    repo_field: str = "repo_name",
    max_workers: int = 8,
):
    # Check input file exists
    if not Path(input_path).exists():
        logger.error(f"Input file not found: {input_path}")
        logger.error("Please provide a valid input file with -i/--input")
        return
    
    # Load all entries (skip empty lines)
    with open(input_path, "r") as f:
        entries = []
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Skipping invalid JSON at line {line_num}: {e}")
    
    if not entries:
        logger.error(f"No valid entries found in {input_path}")
        return
    
    total = len(entries)
    logger.info(f"Processing {total} repos from {input_path} with {max_workers} threads")
    
    # Thread-safe counters
    kept = 0
    clone_failed = 0
    no_framework = 0
    missing_field = 0
    
    # Lock for file writing and counter updates
    write_lock = threading.Lock()
    
    results = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_entry = {
            executor.submit(process_single_repo, entry, repo_field): entry
            for entry in entries
        }
        
        # Process results as they complete with progress bar
        with tqdm(total=total, desc="Scanning repos", unit="repo") as pbar:
            for future in as_completed(future_to_entry):
                entry = future_to_entry[future]
                repo = entry.get(repo_field, "unknown")
                
                try:
                    result, status = future.result()
                    
                    with write_lock:
                        if status == "found":
                            results.append(result)
                            kept += 1
                            logger.info(f"✓ {repo} → {result['framework']}")
                        elif status == "clone_failed":
                            clone_failed += 1
                        elif status == "no_framework":
                            no_framework += 1
                        elif status == "missing_field":
                            missing_field += 1
                            
                except Exception as e:
                    logger.error(f"Error processing {repo}: {e}")
                
                pbar.update(1)
    
    # Write results to output file (create parent directories if needed)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w") as fout:
        for result in results:
            fout.write(json.dumps(result) + "\n")
    
    logger.info("=" * 50)
    logger.info(f"✅ Done — kept {kept}/{total} repos")
    logger.info(f"   Clone failed: {clone_failed}")
    logger.info(f"   No framework detected: {no_framework}")
    if missing_field:
        logger.info(f"   Missing field: {missing_field}")
    logger.info(f"Output written to: {output_path}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Detect Hexo/Jekyll/Static HTML frameworks in GitHub repos from a JSONL dataset"
    )
    parser.add_argument(
        "-i", "--input",
        default="cwv-bench-exps/gh_25_github_io_repos_filtered.jsonl",
        help="Path to input JSONL file (default: cwv-bench-exps/gh_25_github_io_repos_filtered.jsonl)"
    )
    parser.add_argument(
        "-o", "--output",
        default="heuristic_eval/framework_filtered.jsonl",
        help="Path to output JSONL file (default: heuristic_eval/framework_filtered.jsonl)"
    )
    parser.add_argument(
        "-f", "--field",
        default="repo_name",
        help="JSON field containing repo name/URL (default: repo_name)"
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=8,
        help="Number of parallel threads (default: 8)"
    )
    
    args = parser.parse_args()
    
    process_jsonl(
        input_path=args.input,
        output_path=args.output,
        repo_field=args.field,
        max_workers=args.workers,
    )