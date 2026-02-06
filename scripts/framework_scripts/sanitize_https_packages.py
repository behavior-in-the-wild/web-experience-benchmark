"""
Remove explicitly identified junk tokens from package lists.

Assumptions:
- `packages` already come from HTTPS-based extraction upstream
- We ONLY remove obvious garbage / noise
- No inference, no collapsing, no library guessing

This is a pure negative filter.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Set


# -------------------------------------------------------------------
# JUNK DEFINITIONS
# -------------------------------------------------------------------

# Tokens that are never meaningful standalone dependencies
EXACT_JUNK = {
    # generic placeholders
    "all", "main", "index", "default", "app",
    "data", "page", "pages",

    # paths / layout
    "js", "css", "html",
    "script", "scripts",
    "style", "styles",
    "assets", "static",
    "lib", "libs",
    "vendor", "vendors",

    # site / content
    "content", "media", "images", "fonts",
    "header", "footer", "nav", "menu", "sidebar",
    "about", "blog", "post", "posts",

    # generic tech words
    "api", "client", "server",
    "framework", "frameworks",
    "theme", "themes", "skin", "skins",

    # misc observed noise
    "github",
}

# Substrings that indicate build artifacts or non-dependencies
SUBSTRING_JUNK = (
    "css-map",
    "js-map",
    "source-map",
    ".map",
)

# Long hash-like blobs (minifier output, fingerprints)
HEX_RE = re.compile(r"[a-f0-9]{8,}", re.IGNORECASE)


# -------------------------------------------------------------------
# CORE LOGIC
# -------------------------------------------------------------------

def is_junk(token: str) -> bool:
    t = token.strip().lower()

    if not t:
        return True

    # exact matches
    if t in EXACT_JUNK:
        return True

    # substring matches
    for s in SUBSTRING_JUNK:
        if s in t:
            return True

    # hash-like garbage
    if HEX_RE.search(t) and len(t) > 10:
        return True

    # too short to be meaningful
    if len(t) <= 2:
        return True

    return False


def clean_packages(packages: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []

    for p in packages:
        q = p.strip()
        if not q:
            continue
        if is_junk(q):
            continue
        if q not in seen:
            seen.add(q)
            out.append(q)

    return out


# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------

def main(input_path: Path, output_path: Path) -> None:
    with open(input_path, "r", encoding="utf-8") as fin, \
         open(output_path, "w", encoding="utf-8") as fout:

        for line in fin:
            if not line.strip():
                continue

            rec: Dict = json.loads(line)

            raw_packages = rec.get("packages", [])
            rec["packages"] = clean_packages(raw_packages)
            rec["num_packages"] = len(rec["packages"])

            fout.write(json.dumps(rec) + "\n")
        
    print(f"Cleaned package lists written to: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Remove explicitly identified junk tokens from package lists"
    )
    parser.add_argument("--input", type=Path, default = "scripts/framework_scripts/packages.jsonl", required=False)
    parser.add_argument("--output", type=Path, default = "scripts/framework_scripts/final_packages.jsonl", required=False)
    args = parser.parse_args()

    main(args.input, args.output)
