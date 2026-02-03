from pathlib import Path

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
