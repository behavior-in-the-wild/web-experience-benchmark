#!/usr/bin/env python3
"""
build_autodep_csv.py — generate the evaluate.sh-compatible CSV that wires
the 100 auto-deployed repos to the OSS-models pipeline.

Input:
  autodep_final_100_host_scripts/final_100_successful_autodeployed_repos_details.csv
  with columns: repo_url, source_run, detected_framework, confidence,
                best_attempt, script_path

Output (default):
  harness/SAMPLE/autodep_100.csv
  with columns: ID, REPO_ID, FRAMEWORK, COMMIT_ID, ZIP_REPO_PATH,
                HOST_FILE_PATH, CWV_MOBILE, CWV_DESKTOP,
                LCP_ENTRIES_MOBILE, LCP_ENTRIES_DESKTOP

Every row uses HOST_FILE_PATH = "host_files/host_autodep.sh" — the dispatcher
that resolves the right per-repo autodep script at run time via REPO_ID.

ID is the leading-zero index from the autodep filename ("001__..." -> "001").
COMMIT_ID is left blank; evaluate.sh's clone step takes the default branch
HEAD, which is the same commit the autodep scripts were generated against
(autodep generation operated on HEAD at v3 capture time).

CWV baseline columns are emitted empty. If you need them populated, run
the baseline pipeline against the autodep CSV first and update in place.

Usage:
  python3 scripts/build_autodep_csv.py
  python3 scripts/build_autodep_csv.py --out custom/path.csv
  python3 scripts/build_autodep_csv.py --details path/to/details.csv
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

ROOT = Path("/dev/shm/ayush/web-experience-benchmark")
DEFAULT_DETAILS = ROOT / "autodep_final_100_host_scripts" / "final_100_successful_autodeployed_repos_details.csv"
DEFAULT_AUTODEP_ROOT = ROOT / "autodep_final_100_host_scripts"
DEFAULT_OUT = ROOT / "harness" / "SAMPLE" / "autodep_100.csv"

CSV_FIELDS = [
    "ID", "REPO_ID", "FRAMEWORK", "COMMIT_ID",
    "ZIP_REPO_PATH", "HOST_FILE_PATH",
    "CWV_MOBILE", "CWV_DESKTOP",
    "LCP_ENTRIES_MOBILE", "LCP_ENTRIES_DESKTOP",
]

HOST_DISPATCHER = "host_files/host_autodep.sh"

# Filename pattern: "001__org__repo__host.sh" -> id="001", org/repo
FILENAME_RE = re.compile(r"^(\d+)__(.+)__host\.sh$")


def repo_id_from_url(url: str) -> str:
    """Convert 'https://github.com/org/repo' (with optional trailing /, .git, tree/..) to 'org/repo'."""
    url = url.strip()
    url = re.sub(r"^https?://(?:www\.)?github\.com/", "", url)
    url = re.sub(r"\.git$", "", url)
    url = url.rstrip("/")
    return "/".join(url.split("/")[:2])


def build_index_by_repo(autodep_root: Path) -> dict[str, tuple[str, str]]:
    """Walk autodep_root for *__org__repo__host.sh and return {org/repo: (ID, filename)}."""
    out: dict[str, tuple[str, str]] = {}
    for p in sorted(autodep_root.glob("*__host.sh")):
        m = FILENAME_RE.match(p.name)
        if not m:
            continue
        idx, slug = m.group(1), m.group(2)
        # slug is "org__repo" — the autodep generator uses '__' as the '/' replacement.
        # However repo names themselves may contain underscores; we split on the *first* '__'.
        if "__" not in slug:
            continue
        org, repo = slug.split("__", 1)
        out[f"{org}/{repo}"] = (idx, p.name)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate autodep_100.csv for evaluate.sh.")
    ap.add_argument("--details", type=Path, default=DEFAULT_DETAILS,
                    help=f"Source CSV (default: {DEFAULT_DETAILS})")
    ap.add_argument("--autodep-root", type=Path, default=DEFAULT_AUTODEP_ROOT,
                    help=f"Dir containing the autodep host scripts (default: {DEFAULT_AUTODEP_ROOT})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"Output CSV (default: {DEFAULT_OUT})")
    args = ap.parse_args()

    if not args.details.exists():
        raise SystemExit(f"Input CSV not found: {args.details}")
    if not args.autodep_root.is_dir():
        raise SystemExit(f"Autodep root not found: {args.autodep_root}")

    index = build_index_by_repo(args.autodep_root)
    if not index:
        raise SystemExit(f"No matching '*__host.sh' files in {args.autodep_root}")

    rows = []
    skipped: list[str] = []
    with open(args.details, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("repo_url", "")
            repo_id = repo_id_from_url(url)
            framework = (row.get("detected_framework") or "").strip() or "unknown"
            if repo_id not in index:
                skipped.append(f"{url} (looked for '{repo_id}' in autodep filenames)")
                continue
            idx, _filename = index[repo_id]
            rows.append({
                "ID": idx,
                "REPO_ID": repo_id,
                "FRAMEWORK": framework,
                "COMMIT_ID": "",   # use default-branch HEAD at clone time
                "ZIP_REPO_PATH": "",
                "HOST_FILE_PATH": HOST_DISPATCHER,
                "CWV_MOBILE": "",
                "CWV_DESKTOP": "",
                "LCP_ENTRIES_MOBILE": "",
                "LCP_ENTRIES_DESKTOP": "",
            })

    rows.sort(key=lambda r: int(r["ID"]))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Wrote {len(rows)} rows -> {args.out}")
    if skipped:
        print(f"\nSkipped {len(skipped)} rows (no autodep script match):")
        for s in skipped[:10]:
            print(f"  - {s}")
        if len(skipped) > 10:
            print(f"  ... (+{len(skipped) - 10} more)")


if __name__ == "__main__":
    main()
