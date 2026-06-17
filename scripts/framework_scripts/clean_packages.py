"""
Extract relevant dependencies from raw package-extraction JSONL.

No hardcoded library names.
No ecosystem assumptions.
Purely frequency + structure driven.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Set


# ---------------- HEURISTICS ----------------

MIN_TOKEN_LEN = 1
MIN_GLOBAL_FREQ = 1          # appears in ≥ N repos
MAX_GENERIC_FREQ = 1      # appears in >60% repos → too generic
MAX_SUBTOKEN_RATIO = 0.85    # variant collapse threshold


GENERIC_TOKENS = {
    "all", "main", "page", "pages", "script", "scripts",
    "style", "styles", "lib", "libs", "build", "dist",
    "bundle", "vendor", "assets", "static", "js", "css",
}


SUFFIX_BLACKLIST = (
    "-map", "-css-map", "-js-map", "-theme", "-themes",
    "-bundle", "-min", "-old",
)


SPLIT_RE = re.compile(r"[-_.]")


# ---------------- CORE LOGIC ----------------

def base_token(pkg: str) -> str:
    """
    Reduce variants:
      jquery-scrollto → jquery
      bootstrap-theme → bootstrap
    """
    parts = SPLIT_RE.split(pkg)
    return parts[0]


def is_metadata_junk(pkg: str) -> bool:
    if len(pkg) < MIN_TOKEN_LEN:
        return True
    if pkg in GENERIC_TOKENS:
        return True
    if any(pkg.endswith(s) for s in SUFFIX_BLACKLIST):
        return True
    if pkg.isdigit():
        return True
    return False


def load_records(path: Path) -> List[Dict]:
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def compute_global_stats(records: List[Dict]) -> Dict:
    repo_count = len(records)

    pkg_repo_freq = Counter()
    base_pkg_map = defaultdict(set)

    for rec in records:
        seen = set(rec.get("packages", []))
        for pkg in seen:
            pkg_repo_freq[pkg] += 1
            base_pkg_map[base_token(pkg)].add(pkg)

    return {
        "repo_count": repo_count,
        "pkg_repo_freq": pkg_repo_freq,
        "base_pkg_map": base_pkg_map,
    }


def select_relevant_packages(
    packages: Iterable[str],
    stats: Dict,
) -> List[str]:
    selected: Set[str] = set()

    for pkg in packages:
        if is_metadata_junk(pkg):
            continue

        freq = stats["pkg_repo_freq"][pkg]
        freq_ratio = freq / stats["repo_count"]

        # drop ultra-generic tokens
        if freq_ratio > MAX_GENERIC_FREQ:
            continue

        # must appear in multiple repos
        if freq < MIN_GLOBAL_FREQ:
            continue

        selected.add(pkg)

    # collapse variants → base token
    collapsed: Dict[str, Set[str]] = defaultdict(set)
    for pkg in selected:
        collapsed[base_token(pkg)].add(pkg)

    final: Set[str] = set()
    for base, variants in collapsed.items():
        # if many variants map to same base → keep base
        if len(variants) / max(1, stats["pkg_repo_freq"][base]) > MAX_SUBTOKEN_RATIO:
            final.add(base)
        else:
            final.update(variants)

    return sorted(final)


# ---------------- MAIN ----------------

def main(input_path: Path, output_path: Path) -> None:
    records = load_records(input_path)
    stats = compute_global_stats(records)

    with open(output_path, "w") as out:
        for rec in records:
            raw = rec.get("packages", [])
            cleaned = select_relevant_packages(raw, stats)

            # overwrite packages with the cleaned list
            rec["packages"] = cleaned

            out.write(json.dumps(rec) + "\n")


    print(f"Saved → {output_path}")
    print(f"Repos processed: {stats['repo_count']}")
    print(f"Unique packages: {len(stats['pkg_repo_freq'])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default = "scripts/framework_scripts/packages.jsonl", required=False)
    parser.add_argument("--output", type=Path, default = "scripts/framework_scripts/clean_packages.jsonl", required=False)
    args = parser.parse_args()

    main(args.input, args.output)
