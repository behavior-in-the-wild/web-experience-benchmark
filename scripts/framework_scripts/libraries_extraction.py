# Extracting libraries used in the repos of the top4 most populous frameworks in our benchmark (Static HTML, Jekyll, Hexo, Hugo)

import json
import subprocess
import shutil
import re
from pathlib import Path
from typing import Set, Tuple

# ---------------- CONFIG ----------------

BENCHMARK_PATH = Path("final_results.json")  # or .jsonl
CLONE_ROOT = Path("tmp_clones")
OUTPUT_PATH = Path("repo_framework_entry_libs.jsonl")

ALLOWED_FRAMEWORKS = {"Static HTML", "Jekyll", "Hexo", "Hugo"}

# ---- Hugo ----
HUGO_CONFIG_FILES_STRONG = ["hugo.toml", "hugo.yaml", "hugo.yml"]
HUGO_CONFIG_FILES_GENERIC = ["config.toml", "config.yaml", "config.yml"]

# ---- Hexo ----
HEXO_META_PREFIX = '<meta name="generator" content="hexo'
HEXO_KEYWORDS = ["powered by hexo", "由 hexo", "hexo"]

# ---- Static HTML ----
STATIC_HTML_INDICATORS = [
    'href="styles.css"',
    'href="style.css"',
    'href="css/style.css"',
    'href="./css/',
    'src="script.js"',
    'src="js/main.js"',
    'src="./js/',
    'href="index.html"',
    'href="about.html"',
    'href="contact.html"',
]

CANONICAL_LIBS = {
    "jquery": ["jquery"],
    "bootstrap": ["bootstrap"],
    "tailwind": ["tailwind"],
    "alpinejs": ["alpine"],
    "react": ["react", "react-dom"],
    "vue": ["vue"],
    "d3": ["d3"],
    "threejs": ["three"],
    "swiper": ["swiper"],
    "gsap": ["gsap", "greensock"],
    "fontawesome": ["fontawesome", "fa-"],
}

# --------------------------------------

SCRIPT_RE = re.compile(r'src=["\']([^"\']+)["\']', re.I)
LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
IMPORT_RE = re.compile(r'import\s+.*?from\s+[\'"]([^\'"]+)[\'"]')
REQUIRE_RE = re.compile(r'require\([\'"]([^\'"]+)[\'"]\)')


def normalize(token: str) -> str | None:
    token = token.lower()
    for lib, variants in CANONICAL_LIBS.items():
        for v in variants:
            if v in token:
                return lib
    return None


def extract_libs_from_text(path: Path) -> Set[str]:
    libs = set()
    try:
        text = path.read_text(errors="ignore").lower()
    except Exception:
        return libs

    candidates = []
    candidates += SCRIPT_RE.findall(text)
    candidates += LINK_RE.findall(text)
    candidates += IMPORT_RE.findall(text)
    candidates += REQUIRE_RE.findall(text)

    for token in candidates:
        lib = normalize(token)
        if lib:
            libs.add(lib)

    return libs


def extract_from_package_json(path: Path) -> Set[str]:
    libs = set()
    try:
        data = json.loads(path.read_text())
    except Exception:
        return libs

    deps = {}
    deps.update(data.get("dependencies", {}))
    deps.update(data.get("devDependencies", {}))

    for name in deps:
        lib = normalize(name)
        if lib:
            libs.add(lib)

    return libs


# ---------------- Framework-specific file selection ----------------

def get_static_html_files(repo_dir: Path) -> Set[Path]:
    files = set()
    for html in repo_dir.glob("*.html"):
        try:
            text = html.read_text(errors="ignore").lower()
        except Exception:
            continue
        if any(ind in text for ind in STATIC_HTML_INDICATORS):
            files.add(html)
    return files


def get_jekyll_files(repo_dir: Path) -> Set[Path]:
    files = set()
    for fname in ["index.html", "Gemfile", "_config.yml", "package.json"]:
        path = repo_dir / fname
        if path.exists():
            files.add(path)
    return files


def get_hugo_files(repo_dir: Path) -> Set[Path]:
    files = set()
    for fname in HUGO_CONFIG_FILES_STRONG + HUGO_CONFIG_FILES_GENERIC:
        path = repo_dir / fname
        if path.exists():
            files.add(path)
    index = repo_dir / "index.html"
    if index.exists():
        files.add(index)
    return files


def get_hexo_files(repo_dir: Path) -> Set[Path]:
    files = set()
    for html in repo_dir.glob("*.html"):
        try:
            text = html.read_text(errors="ignore").lower()
        except Exception:
            continue
        if HEXO_META_PREFIX in text or any(k in text for k in HEXO_KEYWORDS):
            files.add(html)
    index = repo_dir / "index.html"
    if index.exists():
        files.add(index)
    return files


# ---------------- Analysis ----------------

def analyze_repo(repo_dir: Path, framework: str) -> Set[str]:
    libs = set()

    if framework == "Static HTML":
        files = get_static_html_files(repo_dir)
    elif framework == "Jekyll":
        files = get_jekyll_files(repo_dir)
    elif framework == "Hugo":
        files = get_hugo_files(repo_dir)
    elif framework == "Hexo":
        files = get_hexo_files(repo_dir)
    else:
        return libs

    for path in files:
        if path.suffix in {".html", ".js", ".css"}:
            libs |= extract_libs_from_text(path)
        elif path.name == "package.json":
            libs |= extract_from_package_json(path)

    return sorted(libs)


# ---------------- IO ----------------

def clone_repo(repo_id: str, dest: Path) -> bool:
    url = f"https://github.com/{repo_id}.git"
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def load_benchmark(path: Path):
    if path.suffix == ".jsonl":
        with open(path) as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
    else:
        with open(path) as f:
            yield from json.load(f)


def main():
    CLONE_ROOT.mkdir(exist_ok=True)
    OUTPUT_PATH.unlink(missing_ok=True)

    for record in load_benchmark(BENCHMARK_PATH):
        repo_id = record.get("REPO_ID")
        framework = record.get("FRAMEWORK")

        if not repo_id or framework not in ALLOWED_FRAMEWORKS:
            continue

        repo_dir = CLONE_ROOT / repo_id.replace("/", "__")

        if repo_dir.exists():
            shutil.rmtree(repo_dir)

        if not clone_repo(repo_id, repo_dir):
            continue

        libs = analyze_repo(repo_dir, framework)

        out = {
            "repo_id": repo_id,
            "framework": framework,
            "libraries": libs,
        }

        with open(OUTPUT_PATH, "a") as f:
            f.write(json.dumps(out) + "\n")

        shutil.rmtree(repo_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
