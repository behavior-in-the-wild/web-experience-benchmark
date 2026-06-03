#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from datasets import load_dataset
from tqdm import tqdm


GITHUB_API = "https://api.github.com"


DEPLOY_HEADING_TERMS = [
    "deploy",
    "deployment",
    "publishing",
    "publish",
    "production",
    "hosting",
    "github pages",
    "pages",
    "vercel",
    "netlify",
    "firebase",
    "cloudflare",
    "render",
    "railway",
    "heroku",
    "surge",
    "gh-pages",
    "release",
]


SETUP_HEADING_TERMS = [
    "setup",
    "install",
    "installation",
    "getting started",
    "quick start",
    "quickstart",
    "run",
    "running",
    "local development",
    "development",
    "build",
    "serve",
    "usage",
]


HOSTING_TERMS = [
    "github pages",
    "gh-pages",
    "vercel",
    "netlify",
    "firebase",
    "firebase hosting",
    "cloudflare pages",
    "render",
    "railway",
    "heroku",
    "surge",
    "pages.dev",
    "deployment",
    "deploy",
    "publish",
    "production build",
]


COMMAND_PATTERNS = [
    r"\b npm\s+(install|ci|run\s+(dev|build|start|deploy|serve|preview)|start)\b",
    r"\b yarn\s+(install|dev|build|start|deploy|serve|preview)\b",
    r"\b pnpm\s+(install|dev|build|start|deploy|serve|preview)\b",
    r"\b bun\s+(install|run\s+)?(dev|build|start|deploy|serve|preview)\b",
    r"\b npx\s+(next|vite|astro|gatsby|eleventy|webpack|serve|gh-pages)\b",
    r"\b next\s+(dev|build|start|export)\b",
    r"\b vite\s+(--host\s+)?(dev|build|preview)?\b",
    r"\b astro\s+(dev|build|preview)\b",
    r"\b gatsby\s+(develop|build|serve)\b",
    r"\b hugo\s+(server|--minify|--gc|new|mod)\b|\bhugo\b",
    r"\b jekyll\s+(serve|build)\b|\bbundle\s+exec\s+jekyll\s+(serve|build)\b",
    r"\b mkdocs\s+(serve|build|gh-deploy)\b",
    r"\b sphinx-build\b|\b make\s+(html|docs|deploy|build)\b",
    r"\b docker\s+compose\s+up\b|\b docker-compose\s+up\b|\b docker\s+build\b|\b docker\s+run\b",
    r"\b vercel\b|\b netlify\s+(deploy|build|dev)\b|\b firebase\s+deploy\b",
    r"\b gh-pages\b|\b surge\b",
]


COMMAND_RE = re.compile(
    "|".join(f"(?:{p})" for p in COMMAND_PATTERNS),
    re.IGNORECASE,
)

HOSTING_RE = re.compile(
    "|".join(re.escape(t) for t in HOSTING_TERMS),
    re.IGNORECASE,
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)

FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)


def eprint(*args):
    print(*args, file=sys.stderr, flush=True)


def repo_to_github_url(repo_id: str) -> str:
    return f"https://github.com/{repo_id}"


def clean_repo_id(raw) -> Optional[str]:
    if raw is None:
        return None

    s = str(raw).strip()

    if not s:
        return None

    s = s.replace("https://github.com/", "")
    s = s.replace("http://github.com/", "")
    s = s.replace("git@github.com:", "")

    if s.endswith(".git"):
        s = s[:-4]

    s = s.strip("/")
    parts = s.split("/")

    if len(parts) < 2:
        return None

    owner = parts[0].strip()
    repo = parts[1].strip()

    if not owner or not repo:
        return None

    return f"{owner}/{repo}"


def load_processed(checkpoint_path: str) -> Set[str]:
    processed = set()

    if not os.path.exists(checkpoint_path):
        return processed

    with open(checkpoint_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
                repo_id = obj.get("repo_id")

                if repo_id:
                    processed.add(repo_id)

            except Exception:
                pass

    return processed


def load_existing_links(path: str) -> Set[str]:
    links = set()

    if not os.path.exists(path):
        return links

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            link = line.strip()
            if link:
                links.add(link)

    return links


def append_unique_link(path: str, link: str, existing_links: Set[str]):
    if not link or link in existing_links:
        return

    with open(path, "a", encoding="utf-8") as f:
        f.write(link + "\n")
        f.flush()
        os.fsync(f.fileno())

    existing_links.add(link)


def backfill_confidence_link_files_from_checkpoint(
    checkpoint_path: str,
    high_links_path: str,
    medium_links_path: str,
    existing_high_links: Set[str],
    existing_medium_links: Set[str],
):
    """
    Useful when you already ran an older version of the script.

    It scans deploy_checkpoint.jsonl and fills:
    - high_confidence_repo_links.txt
    - medium_confidence_repo_links.txt

    without duplicating links.
    """

    if not os.path.exists(checkpoint_path):
        return

    high_added = 0
    medium_added = 0

    with open(checkpoint_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue

            repo_id = obj.get("repo_id")
            has_instructions = obj.get("has_instructions")
            confidence = obj.get("confidence")

            if not repo_id or not has_instructions:
                continue

            link = repo_to_github_url(repo_id)

            if confidence == "high":
                before = len(existing_high_links)
                append_unique_link(high_links_path, link, existing_high_links)
                if len(existing_high_links) > before:
                    high_added += 1

            elif confidence == "medium":
                before = len(existing_medium_links)
                append_unique_link(medium_links_path, link, existing_medium_links)
                if len(existing_medium_links) > before:
                    medium_added += 1

    if high_added or medium_added:
        eprint(
            f"[BACKFILL] Added {high_added:,} high-confidence and "
            f"{medium_added:,} medium-confidence repo link(s) from checkpoint."
        )


def get_thread_session() -> requests.Session:
    local = get_thread_session._local

    if not hasattr(local, "session"):
        session = requests.Session()

        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "cwv-readme-deploy-instruction-miner",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        if token:
            headers["Authorization"] = f"Bearer {token}"

        session.headers.update(headers)
        local.session = session

    return local.session


get_thread_session._local = threading.local()


def github_get_json(url: str, timeout: int = 30, max_retries: int = 5):
    session = get_thread_session()

    for attempt in range(max_retries):
        try:
            r = session.get(url, timeout=timeout)

        except requests.RequestException as ex:
            if attempt == max_retries - 1:
                return 0, {"error": str(ex)}, {}

            time.sleep(min(60, 2**attempt))
            continue

        headers = dict(r.headers)

        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")

        if r.status_code in (403, 429) and remaining == "0" and reset:
            sleep_for = max(5, int(reset) - int(time.time()) + 5)
            reset_clock = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(int(reset)),
            )

            eprint(f"[RATE LIMIT] Sleeping {sleep_for}s until {reset_clock} local time...")
            time.sleep(sleep_for)
            continue

        if r.status_code in (500, 502, 503, 504):
            time.sleep(min(60, 2**attempt))
            continue

        try:
            return r.status_code, r.json(), headers

        except Exception:
            return r.status_code, {"error": r.text[:500]}, headers

    return 0, {"error": "max retries exceeded"}, {}


def fetch_readme(repo_id: str) -> Dict:
    url = f"{GITHUB_API}/repos/{repo_id}/readme"

    status, data, headers = github_get_json(url)

    result = {
        "repo_id": repo_id,
        "status": status,
        "readme_path": None,
        "html_url": None,
        "download_url": None,
        "readme_text": None,
        "error": None,
        "rate_remaining": headers.get("X-RateLimit-Remaining"),
    }

    if status == 200 and isinstance(data, dict):
        result["readme_path"] = data.get("path")
        result["html_url"] = data.get("html_url")
        result["download_url"] = data.get("download_url")

        content = data.get("content")
        encoding = data.get("encoding")

        if content and encoding == "base64":
            try:
                raw = base64.b64decode(content, validate=False)
                result["readme_text"] = raw.decode("utf-8", errors="replace")

            except Exception as ex:
                result["error"] = f"README decode error: {ex}"

        else:
            result["error"] = f"Unexpected README encoding: {encoding}"

    else:
        if isinstance(data, dict):
            result["error"] = data.get("message")
        else:
            result["error"] = f"GitHub status {status}"

    return result


def markdown_sections(text: str) -> List[Tuple[int, str, str]]:
    matches = list(HEADING_RE.finditer(text))

    if not matches:
        return []

    sections = []

    for i, m in enumerate(matches):
        level = len(m.group(1))
        title = m.group(2).strip()

        start = m.end()
        end = len(text)

        for j in range(i + 1, len(matches)):
            next_level = len(matches[j].group(1))

            if next_level <= level:
                end = matches[j].start()
                break

        body = text[start:end].strip()
        sections.append((level, title, body))

    return sections


def contains_any(s: str, terms: List[str]) -> bool:
    low = s.lower()
    return any(t in low for t in terms)


def clamp_text(s: str, max_chars: int) -> str:
    s = re.sub(r"\n{4,}", "\n\n\n", s).strip()

    if len(s) <= max_chars:
        return s

    return s[:max_chars].rstrip() + "\n\n...[truncated]"


def line_snippets(text: str, window: int = 5) -> str:
    lines = text.splitlines()
    hits = []

    for i, line in enumerate(lines):
        if COMMAND_RE.search(line) or HOSTING_RE.search(line):
            hits.append(i)

    if not hits:
        return ""

    ranges = []

    for idx in hits[:20]:
        a = max(0, idx - window)
        b = min(len(lines), idx + window + 1)

        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], b))
        else:
            ranges.append((a, b))

    chunks = []

    for a, b in ranges:
        chunk = "\n".join(lines[a:b]).strip()
        if chunk:
            chunks.append(chunk)

    return "\n\n---\n\n".join(chunks).strip()


def extract_instructions(readme: str, max_chars: int = 7000) -> Dict:
    text = readme.replace("\r\n", "\n").replace("\r", "\n")

    sections = markdown_sections(text)
    candidates = []

    for _, heading, body in sections:
        heading_l = heading.lower()
        blob = f"{heading}\n{body}"

        command_hits = len(COMMAND_RE.findall(blob))
        hosting_hits = len(HOSTING_RE.findall(blob))
        code_fence_hits = len(FENCE_RE.findall(body))

        score = 0
        signals = []

        if contains_any(heading_l, DEPLOY_HEADING_TERMS):
            score += 7
            signals.append(f"explicit deploy/hosting heading: {heading}")

        if contains_any(heading_l, SETUP_HEADING_TERMS):
            score += 3
            signals.append(f"setup/run/build heading: {heading}")

        if command_hits:
            score += min(6, command_hits * 2)
            signals.append(f"{command_hits} deploy/build/run command signal(s)")

        if hosting_hits:
            score += min(4, hosting_hits)
            signals.append(f"{hosting_hits} hosting/deploy keyword signal(s)")

        if code_fence_hits and (
            command_hits
            or contains_any(heading_l, SETUP_HEADING_TERMS + DEPLOY_HEADING_TERMS)
        ):
            score += 1
            signals.append(f"{code_fence_hits} code block(s) in relevant section")

        if score >= 5 and (
            command_hits
            or hosting_hits
            or contains_any(heading_l, DEPLOY_HEADING_TERMS)
        ):
            candidates.append((score, heading, body, signals))

    candidates.sort(key=lambda x: x[0], reverse=True)

    if candidates:
        parts = []
        merged_signals = []
        total_score = 0

        for score, heading, body, signals in candidates[:4]:
            total_score += score
            merged_signals.extend(signals)
            parts.append(f"## {heading}\n\n{body.strip()}")

        unique_signals = []

        for s in merged_signals:
            if s not in unique_signals:
                unique_signals.append(s)

        confidence = "high" if any("explicit deploy" in s for s in unique_signals) else "medium"

        return {
            "has_instructions": True,
            "confidence": confidence,
            "score": total_score,
            "signals": unique_signals,
            "instructions": clamp_text("\n\n".join(parts), max_chars),
        }

    snippets = line_snippets(text)

    if snippets:
        command_hits = len(COMMAND_RE.findall(snippets))
        hosting_hits = len(HOSTING_RE.findall(snippets))
        score = command_hits * 2 + hosting_hits

        if score >= 3:
            signals = []

            if command_hits:
                signals.append(f"{command_hits} deploy/build/run command signal(s) in README")

            if hosting_hits:
                signals.append(f"{hosting_hits} hosting/deploy keyword signal(s) in README")

            confidence = "medium" if hosting_hits else "low"

            return {
                "has_instructions": True,
                "confidence": confidence,
                "score": score,
                "signals": signals,
                "instructions": clamp_text(snippets, max_chars),
            }

    return {
        "has_instructions": False,
        "confidence": "none",
        "score": 0,
        "signals": [],
        "instructions": "",
    }


def process_repo(repo_id: str, max_instruction_chars: int) -> Dict:
    fetched = fetch_readme(repo_id)

    record = {
        "repo_id": repo_id,
        "repo_url": repo_to_github_url(repo_id),
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": fetched["status"],
        "readme_path": fetched["readme_path"],
        "html_url": fetched["html_url"],
        "download_url": fetched["download_url"],
        "error": fetched["error"],
        "has_instructions": False,
        "confidence": "none",
        "score": 0,
        "signals": [],
        "instructions": "",
        "rate_remaining": fetched.get("rate_remaining"),
    }

    if fetched.get("readme_text"):
        extracted = extract_instructions(
            fetched["readme_text"],
            max_chars=max_instruction_chars,
        )
        record.update(extracted)

    return record


def append_checkpoint(path: str, record: Dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def append_txt(path: str, record: Dict):
    repo_id = record["repo_id"]
    repo_url = record.get("repo_url") or repo_to_github_url(repo_id)
    readme_url = record.get("html_url") or repo_url
    signals = "; ".join(record.get("signals") or [])

    block = f"""
================================================================================
REPO: {repo_id}
REPO URL: {repo_url}
README: {readme_url}
CONFIDENCE: {record.get("confidence")} | SCORE: {record.get("score")}
SIGNALS: {signals}
================================================================================
{record.get("instructions", "").strip()}

""".lstrip()

    with open(path, "a", encoding="utf-8") as f:
        f.write(block)
        f.flush()
        os.fsync(f.fileno())


def append_tsv(path: str, record: Dict):
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0

    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "repo_id",
                "repo_url",
                "confidence",
                "score",
                "readme",
                "signals",
            ],
            delimiter="\t",
        )

        if new_file:
            writer.writeheader()

        repo_id = record.get("repo_id")

        writer.writerow(
            {
                "repo_id": repo_id,
                "repo_url": record.get("repo_url") or repo_to_github_url(repo_id),
                "confidence": record.get("confidence"),
                "score": record.get("score"),
                "readme": record.get("html_url") or repo_to_github_url(repo_id),
                "signals": "; ".join(record.get("signals") or []),
            }
        )

        f.flush()
        os.fsync(f.fileno())


def append_confidence_link_if_needed(
    record: Dict,
    high_links_path: str,
    medium_links_path: str,
    existing_high_links: Set[str],
    existing_medium_links: Set[str],
):
    if not record.get("has_instructions"):
        return

    confidence = record.get("confidence")
    repo_id = record.get("repo_id")

    if not repo_id:
        return

    link = record.get("repo_url") or repo_to_github_url(repo_id)

    if confidence == "high":
        append_unique_link(high_links_path, link, existing_high_links)

    elif confidence == "medium":
        append_unique_link(medium_links_path, link, existing_medium_links)


def iter_dataset_repos(
    dataset: str,
    split: str,
    repo_column: str,
    hf_token: Optional[str],
    limit: Optional[int],
) -> Iterable[str]:
    ds = load_dataset(
        dataset,
        split=split,
        streaming=True,
        token=hf_token,
    )

    seen = set()
    yielded = 0

    for row in ds:
        repo_id = clean_repo_id(row.get(repo_column))

        if not repo_id:
            continue

        if repo_id in seen:
            continue

        seen.add(repo_id)
        yield repo_id
        yielded += 1

        if limit is not None and yielded >= limit:
            break


def main():
    parser = argparse.ArgumentParser(
        description="Extract likely deployment instructions from GitHub README files listed in a Hugging Face dataset."
    )

    parser.add_argument(
        "--dataset",
        default="behavior-in-the-wild/cwv-bench-v0",
        help="Hugging Face dataset name.",
    )

    parser.add_argument(
        "--split",
        default="train",
        help="Dataset split.",
    )

    parser.add_argument(
        "--repo-column",
        default="REPO_ID",
        help="Column containing owner/repo GitHub repo IDs.",
    )

    parser.add_argument(
        "--out",
        default="deploy_instructions.txt",
        help="Human-readable extracted deployment instructions.",
    )

    parser.add_argument(
        "--checkpoint",
        default="deploy_checkpoint.jsonl",
        help="JSONL checkpoint file for resume.",
    )

    parser.add_argument(
        "--tsv",
        default="deploy_candidates.tsv",
        help="TSV summary of repos with likely instructions.",
    )

    parser.add_argument(
        "--high-links",
        default="high_confidence_repo_links.txt",
        help="TXT file containing only high-confidence GitHub repo links.",
    )

    parser.add_argument(
        "--medium-links",
        default="medium_confidence_repo_links.txt",
        help="TXT file containing only medium-confidence GitHub repo links.",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Number of parallel GitHub API workers.",
    )

    parser.add_argument(
        "--max-in-flight",
        type=int,
        default=128,
        help="Maximum number of submitted futures waiting at once.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit for testing.",
    )

    parser.add_argument(
        "--max-instruction-chars",
        type=int,
        default=7000,
        help="Maximum extracted README instruction text per repo.",
    )

    args = parser.parse_args()

    if not (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")):
        eprint("[WARN] No GITHUB_TOKEN/GH_TOKEN found. GitHub API will be heavily rate-limited.")

    processed = load_processed(args.checkpoint)

    existing_high_links = load_existing_links(args.high_links)
    existing_medium_links = load_existing_links(args.medium_links)

    eprint(f"[RESUME] Loaded {len(processed):,} processed repo(s) from {args.checkpoint}")
    eprint(f"[LINKS] Existing high-confidence links: {len(existing_high_links):,}")
    eprint(f"[LINKS] Existing medium-confidence links: {len(existing_medium_links):,}")

    backfill_confidence_link_files_from_checkpoint(
        checkpoint_path=args.checkpoint,
        high_links_path=args.high_links,
        medium_links_path=args.medium_links,
        existing_high_links=existing_high_links,
        existing_medium_links=existing_medium_links,
    )

    futures = {}
    found = 0
    high_found = 0
    medium_found = 0
    completed = 0
    submitted = 0

    lock = threading.Lock()

    pbar = tqdm(desc="Processed repos", unit="repo")

    def handle_done(done_future):
        nonlocal found
        nonlocal high_found
        nonlocal medium_found
        nonlocal completed

        repo = futures.pop(done_future)

        try:
            record = done_future.result()

        except Exception as ex:
            record = {
                "repo_id": repo,
                "repo_url": repo_to_github_url(repo),
                "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "status": 0,
                "readme_path": None,
                "html_url": None,
                "download_url": None,
                "error": f"worker exception: {ex}",
                "has_instructions": False,
                "confidence": "none",
                "score": 0,
                "signals": [],
                "instructions": "",
            }

        with lock:
            append_checkpoint(args.checkpoint, record)

            if record.get("has_instructions"):
                found += 1

                if record.get("confidence") == "high":
                    high_found += 1

                elif record.get("confidence") == "medium":
                    medium_found += 1

                append_txt(args.out, record)
                append_tsv(args.tsv, record)

                append_confidence_link_if_needed(
                    record=record,
                    high_links_path=args.high_links,
                    medium_links_path=args.medium_links,
                    existing_high_links=existing_high_links,
                    existing_medium_links=existing_medium_links,
                )

            completed += 1
            pbar.update(1)
            pbar.set_postfix(
                found=found,
                high=high_found,
                medium=medium_found,
                active=len(futures),
            )

    hf_token = os.getenv("HF_TOKEN")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for repo_id in iter_dataset_repos(
            dataset=args.dataset,
            split=args.split,
            repo_column=args.repo_column,
            hf_token=hf_token,
            limit=args.limit,
        ):
            if repo_id in processed:
                continue

            future = executor.submit(
                process_repo,
                repo_id,
                args.max_instruction_chars,
            )

            futures[future] = repo_id
            submitted += 1

            if len(futures) >= args.max_in_flight:
                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)

                for f in done:
                    handle_done(f)

        while futures:
            done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)

            for f in done:
                handle_done(f)

    pbar.close()

    eprint(
        f"[DONE] Submitted {submitted:,}; completed {completed:,}; "
        f"found {found:,} repo(s) with likely instructions."
    )

    eprint(f"[FILES] Instructions TXT: {args.out}")
    eprint(f"[FILES] Checkpoint JSONL: {args.checkpoint}")
    eprint(f"[FILES] Candidate TSV: {args.tsv}")
    eprint(f"[FILES] High-confidence repo links: {args.high_links}")
    eprint(f"[FILES] Medium-confidence repo links: {args.medium_links}")


if __name__ == "__main__":
    main()
