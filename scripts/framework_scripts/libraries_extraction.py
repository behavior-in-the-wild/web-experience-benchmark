"""Extract libraries used in repos for the top 4 most populous frameworks in our benchmark
(Static HTML, Jekyll, Hexo, Hugo).

This script can:
- iterate over a local benchmark JSON/JSONL file (default: final_results.json), OR
- iterate directly over the Hugging Face dataset `behavior-in-the-wild/cwv-bench-v0`,
  git clone each repo, analyze libraries, write results, and delete the clone.

CLI options include resume/limit so you can pause and resume long runs.
"""

# something.min from css / js 
# css, js : link rel = stylesheet

import argparse
import json
import subprocess
import shutil
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from datasets import load_dataset  # type: ignore[import]

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


def iter_source_records(
    source: str,
    benchmark_path: Path,
    hf_dataset_name: str,
    hf_split: str,
) -> Tuple[Iterator[Tuple[int, Dict]], Optional[int], callable, callable]:
    """Return (iterator of (idx, record), total_or_None, get_repo, get_framework)."""
    if source == "benchmark":
        records_iter: Iterator[Tuple[int, Dict]] = enumerate(load_benchmark(benchmark_path))
        get_repo = lambda rec: rec.get("REPO_ID") or rec.get("repo_id")
        get_framework = lambda rec: rec.get("FRAMEWORK") or rec.get("framework")
        total = None
    else:
        ds = load_dataset(hf_dataset_name, split=hf_split)
        total = len(ds)

        def records_iter() -> Iterator[Tuple[int, Dict]]:
            for idx in range(total):  # type: ignore[operator]
                yield idx, ds[idx]  # type: ignore[index]

        get_repo = lambda rec: rec.get("repo_id") or rec.get("REPO_ID") or rec.get("github_repo")
        get_framework = lambda rec: rec.get("framework") or rec.get("FRAMEWORK") or rec.get("framework_name")

    return records_iter(), total, get_repo, get_framework


def process_one_repo(
    idx: int,
    record: Dict,
    get_repo,
    get_framework,
    clone_root: Path,
) -> Optional[Dict]:
    """Clone, analyze, and clean up a single repo. Returns result dict or None on failure/skip."""
    repo_id = get_repo(record)
    framework = get_framework(record)

    if not repo_id or framework not in ALLOWED_FRAMEWORKS:
        return None

    repo_dir = clone_root / str(repo_id).replace("/", "__")

    if repo_dir.exists():
        shutil.rmtree(repo_dir)

    if not clone_repo(repo_id, repo_dir):
        print(f"[warn] Failed to clone {repo_id}, skipping.")
        return None

    try:
        libs = analyze_repo(repo_dir, framework)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)

    return {
        "repo_id": repo_id,
        "framework": framework,
        "libraries": libs,
        "source_index": idx,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Extract library usage from repos by cloning and analyzing them, "
            "optionally reading directly from the HF dataset behavior-in-the-wild/cwv-bench-v0."
        )
    )
    parser.add_argument(
        "--source",
        choices=["benchmark", "hf"],
        default="hf",
        help="Where to read repo list from: local benchmark JSON/JSONL or HF dataset (default: hf).",
    )
    parser.add_argument(
        "--benchmark-path",
        type=Path,
        default=BENCHMARK_PATH,
        help="Path to local benchmark JSON/JSONL (used when --source=benchmark).",
    )
    parser.add_argument(
        "--hf-dataset-name",
        default="behavior-in-the-wild/cwv-bench-v0",
        help="HF dataset repo id (default: behavior-in-the-wild/cwv-bench-v0).",
    )
    parser.add_argument(
        "--hf-split",
        default="train",
        help="HF split to use (default: train).",
    )
    parser.add_argument(
        "--resume-from-index",
        type=int,
        default=0,
        help="0-based index to resume from within the chosen source (default: 0).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many records from resume point (default: no limit).",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=OUTPUT_PATH,
        help="Where to write JSONL results (default: repo_framework_entry_libs.jsonl).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of parallel workers for cloning/analyzing repos (default: 8).",
    )
    parser.add_argument(
        "--flush-every",
        type=int,
        default=50,
        help="Flush results to the output file after this many completed repos (default: 50).",
    )
    args = parser.parse_args()

    clone_root = CLONE_ROOT
    output_path = args.output_path
    print(f"Writing to {output_path}\n")

    clone_root.mkdir(exist_ok=True)
    # Only delete previous output if starting from scratch
    if args.resume_from_index <= 0:
        output_path.unlink(missing_ok=True)

    start_idx = max(args.resume_from_index, 0)
    processed = 0

    records_iter, total, get_repo, get_framework = iter_source_records(
        source=args.source,
        benchmark_path=args.benchmark_path,
        hf_dataset_name=args.hf_dataset_name,
        hf_split=args.hf_split,
    )

    flush_every = max(args.flush_every, 1)
    buffer: List[Dict] = []

    # Submit tasks to a thread pool and write results as they complete
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as ex:
        futures = {}
        for idx, record in records_iter:
            if idx < start_idx:
                continue
            if args.limit is not None and processed >= args.limit:
                break

            fut = ex.submit(process_one_repo, idx, record, get_repo, get_framework, clone_root)
            futures[fut] = idx

        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                print(f"[error] Worker failed at idx={idx}: {e}")
                continue

            if result is None:
                continue

            processed += 1
            buffer.append(result)

            if total is not None:
                print(
                    f"[progress] completed={processed} (idx={result['source_index']}/{total}) "
                    f"repo={result['repo_id']} framework={result['framework']}"
                )
            else:
                print(
                    f"[progress] completed={processed} (idx={result['source_index']}) "
                    f"repo={result['repo_id']} framework={result['framework']}"
                )

            # Periodically flush buffered results to disk for crash-safe progress
            if len(buffer) >= flush_every:
                print("\n FLUSHING \n")
                with open(output_path, "a") as f:
                    for rec in buffer:
                        f.write(json.dumps(rec) + "\n")
                buffer.clear()

    # Final flush of any remaining buffered results
    if buffer:
        with open(output_path, "a") as f:
            for rec in buffer:
                f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
