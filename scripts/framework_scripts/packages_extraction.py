"""Extract package usage from repos.

This script mirrors the shape of `libraries_extraction.py` but instead of
canonical JS/CSS libraries it tries to infer concrete package names
from:

- External CSS/JS URLs in the cloned repo (e.g. CDN / npm URLs).
- Minified build artifacts listed under CODE_STATS.build_artifacts.

Results are written as JSONL, one repo per line.
"""

"""
To-do:
make a bar plot histogram to see distribution

"""


from datetime import datetime
import argparse
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from datasets import load_dataset  # type: ignore[import]


# ---------------- CONFIG ----------------

BENCHMARK_PATH = Path("final_results.json")  # only used with --source=benchmark
CLONE_ROOT = Path("tmp_clones_packages")
OUTPUT_DIR = Path("out")
# Default (non-timestamped) output path; can be overridden via --output-path
OUTPUT_PATH = OUTPUT_DIR / "packages.jsonl"


ALLOWED_FRAMEWORKS = {"Static HTML", "Jekyll", "Hexo", "Hugo"}


# ---------------- REGEXES ----------------

LINK_STYLESHEET_RE = re.compile(
    r'<link[^>]+rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

SCRIPT_SRC_RE = re.compile(
    r'<script[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

MINIFIED_ASSET_RE = re.compile(
    r'["\']([^"\']*?\.min\.[^"\']+?)["\']'
)


# ---------------- IO HELPERS ----------------


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


def load_benchmark(path: Path) -> Iterable[Dict]:
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
) -> Tuple[
    Iterator[Tuple[int, Dict]],
    Optional[int],
    Callable[[Dict], Any],
    Callable[[Dict], Any],
    Callable[[Dict], Any],
]:
    """Return iterator + helper accessors for repo/framework/CODE_STATS."""
    if source == "benchmark":
        records_iter: Iterator[Tuple[int, Dict]] = enumerate(load_benchmark(benchmark_path))

        def get_repo(rec: Dict) -> Any:
            return rec.get("REPO_ID") or rec.get("repo_id") or rec.get("github_repo")

        def get_framework(rec: Dict) -> Any:
            return rec.get("FRAMEWORK") or rec.get("framework") or rec.get("framework_name")

        def get_code_stats(rec: Dict) -> Any:
            return rec.get("CODE_STATS") or rec.get("code_stats")

        total = None
    else:
        ds = load_dataset(hf_dataset_name, split=hf_split)
        total = len(ds)

        def records_iter() -> Iterator[Tuple[int, Dict]]:
            for idx in range(total):  # type: ignore[operator]
                yield idx, ds[idx]  # type: ignore[index]

        def get_repo(rec: Dict) -> Any:
            return rec.get("repo_id") or rec.get("REPO_ID") or rec.get("github_repo")

        def get_framework(rec: Dict) -> Any:
            return rec.get("framework") or rec.get("FRAMEWORK") or rec.get("framework_name")

        def get_code_stats(rec: Dict) -> Any:
            return rec.get("code_stats") or rec.get("CODE_STATS")

    return records_iter(), total, get_repo, get_framework, get_code_stats


# ---------------- EXTRACTION HELPERS ----------------


def _normalize_package_from_token(token: str) -> Optional[str]:
    """Best-effort extraction of a package/library name from a URL or path.

    Examples:
    - "css/bootstrap-3.3.5.min.css"                  -> "bootstrap"
    - "css/bootstrap-select-1.7.2.min.css"           -> "bootstrap-select"
    - "js/jquery-2.1.4.min.js"                       -> "jquery"
    - "https://cdn.jsdelivr.net/npm/@fancyapps/ui@5" -> "fancyapps"
    - "https://cdn.jsdelivr.net/npm/bulma@1.0.0/..." -> "bulma"
    """
    if not token:
        return None

    t = token.strip()
    lower = t.lower()

    # jsDelivr npm pattern: /npm/<pkg>[@version]/...
    m = re.search(r"/npm/([^/@]+(?:/[^/@]+)?)", lower)
    if m:
        seg = m.group(1)  # "@fancyapps/ui" or "bulma"
        if seg.startswith("@"):
            seg = seg[1:]
        if "/" in seg:
            seg = seg.split("/", 1)[0]
        seg = seg.strip()
        if seg:
            return seg

    # Google Fonts as a single logical package.
    if "fonts.googleapis.com" in lower:
        return "google-fonts"

    # Fallback: infer from filename.
    last = lower.split("?", 1)[0].split("#", 1)[0].split("/")[-1]
    if not last:
        return None

    for ext in (".css", ".js"):
        if last.endswith(ext):
            last = last[: -len(ext)]

    last = last.replace(".min", "").replace("-min", "")

    parts = re.split(r"[-_.]", last)
    name_parts: List[str] = []
    for part in parts:
        if not part:
            continue
        if part[0].isdigit():
            break
        name_parts.append(part)

    if not name_parts:
        return None

    return "-".join(name_parts)


# def extract_external_assets_from_repo(repo_dir: Path) -> Dict[str, List[str]]:
#     """Collect raw external stylesheet/script URLs from web-ish files in the repo."""
#     link_stylesheets: Set[str] = set()
#     script_src: Set[str] = set()

#     exts = {
#         ".html",
#         ".htm",
#         ".php",
#         ".js",
#         ".jsx",
#         ".ts",
#         ".tsx",
#         ".vue",
#         ".ejs",
#         ".njk",
#         ".liquid",
#     }

#     for path in repo_dir.rglob("*"):
#         if not path.is_file():
#             continue
#         if path.suffix.lower() not in exts:
#             continue

#         try:
#             text = path.read_text(errors="ignore")
#         except Exception:
#             continue

#         for href in LINK_STYLESHEET_RE.findall(text):
#             link_stylesheets.add(href.strip())
#         for src in SCRIPT_SRC_RE.findall(text):
#             script_src.add(src.strip())

#     return {
#         "link_stylesheets": sorted(link_stylesheets),
#         "script_src": sorted(script_src),
#     }
def extract_external_assets_from_repo(repo_dir: Path) -> Dict[str, List[str]]:
    """Collect raw external stylesheet/script URLs from web-ish files in the repo."""
    link_stylesheets: Set[str] = set()
    script_src: Set[str] = set()

    exts = {
        ".html",
        ".htm",
        ".php",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".vue",
        ".ejs",
        ".njk",
        ".liquid",
    }

    for path in repo_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue

        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue

        for href in LINK_STYLESHEET_RE.findall(text):
            h = href.strip()
            if "https" in h.lower():
                link_stylesheets.add(h)

        for src in SCRIPT_SRC_RE.findall(text):
            s = src.strip()
            if "https" in s.lower():
                script_src.add(s)

    return {
        "link_stylesheets": sorted(link_stylesheets),
        "script_src": sorted(script_src),
    }


# def packages_from_external_assets(external_assets: Dict[str, List[str]]) -> List[str]:
#     packages: Set[str] = set()
#     for key in ("link_stylesheets", "script_src"):
#         for token in external_assets.get(key, []):
#             pkg = _normalize_package_from_token(token)
#             if pkg:
#                 packages.add(pkg)
#     return sorted(packages)
def packages_from_external_assets(external_assets: Dict[str, List[str]]) -> List[str]:
    packages: Set[str] = set()
    for key in ("link_stylesheets", "script_src"):
        for token in external_assets.get(key, []):
            # Only extract from fully-qualified https URLs
            if "https" not in token.lower():
                continue
            pkg = _normalize_package_from_token(token)
            if pkg:
                packages.add(pkg)
    return sorted(packages)



def extract_minified_from_code_stats(code_stats: Any) -> List[str]:
    """Return all `*.min.*` artifact strings from CODE_STATS.build_artifacts."""
    results: Set[str] = set()

    if isinstance(code_stats, dict):
        artifacts = code_stats.get("build_artifacts") or []
        try:
            for item in artifacts:
                if isinstance(item, str) and ".min." in item:
                    results.add(item)
        except TypeError:
            pass

    elif isinstance(code_stats, str):
        for match in MINIFIED_ASSET_RE.findall(code_stats):
            if ".min." in match:
                results.add(match)

    return sorted(results)


def packages_from_minified_artifacts(artifacts: List[str]) -> List[str]:
    packages: Set[str] = set()
    for token in artifacts:
        pkg = _normalize_package_from_token(token)
        if pkg:
            packages.add(pkg)
    return sorted(packages)


# ---------------- PER-REPO PROCESSING ----------------


def process_one_repo(
    idx: int,
    record: Dict,
    get_repo: Callable[[Dict], Any],
    get_framework: Callable[[Dict], Any],
    get_code_stats: Callable[[Dict], Any],
    clone_root: Path,
) -> Optional[Dict]:
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
        external_assets = extract_external_assets_from_repo(repo_dir)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)

    code_stats = get_code_stats(record)
    minified_artifacts = extract_minified_from_code_stats(code_stats)

    external_packages = packages_from_external_assets(external_assets)
    minified_packages = packages_from_minified_artifacts(minified_artifacts)
    all_packages = sorted({*external_packages, *minified_packages})

    return {
        "repo_id": repo_id,
        "framework": framework,
        "source_index": idx,
        "external_packages": external_packages,
        "minified_packages": minified_packages,
        "packages": all_packages,
    }


# ---------------- CLI ----------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract package usage from repos by cloning and analyzing them, "
            "optionally reading directly from the HF dataset "
            "behavior-in-the-wild/cwv-bench-v0."
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
        help="Where to write JSONL results (default: out/packages.jsonl).",
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
    # output_path = args.output_path
    # print(f"Writing to {output_path}\n")
    OUTPUT_DIR.mkdir(exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = (
        args.output_path
        if args.output_path != OUTPUT_PATH
        else OUTPUT_DIR / f"packages_{ts}.jsonl"
    )

    print(f"Writing to {output_path}\n")


    clone_root.mkdir(exist_ok=True)
    if args.resume_from_index <= 0:
        output_path.unlink(missing_ok=True)

    start_idx = max(args.resume_from_index, 0)
    processed = 0  # number of successful repo results written/queued

    records_iter, total, get_repo, get_framework, get_code_stats = iter_source_records(
        source=args.source,
        benchmark_path=args.benchmark_path,
        hf_dataset_name=args.hf_dataset_name,
        hf_split=args.hf_split,
    )

    flush_every = max(args.flush_every, 1)
    buffer: List[Dict] = []

    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as ex:
        futures: Dict[Any, int] = {}
        for idx, record in records_iter:
            if idx < start_idx:
                continue
            if args.limit is not None and processed >= args.limit:
                break

            fut = ex.submit(
                process_one_repo,
                idx,
                record,
                get_repo,
                get_framework,
                get_code_stats,
                clone_root,
            )
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

            # Flush strictly every `flush_every` successful datapoints.
            if processed % flush_every == 0:
                with open(output_path, "a") as f:
                    for rec in buffer:
                        f.write(json.dumps(rec) + "\n")
                buffer.clear()

    # Final flush for any remaining (< flush_every) records.
    if buffer:
        with open(output_path, "a") as f:
            for rec in buffer:
                f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()

