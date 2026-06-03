#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple, Dict, Set
from urllib.parse import quote, urlsplit, urlunsplit

import requests

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

_thread_local = threading.local()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Input shortlist JSONL")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--limit", type=int, default=None, help="Maximum number of repo_ids to check")
    p.add_argument("--state-file", default="shortlist_about_state.json")
    p.add_argument("--chunks-dir", default="shortlist_about_chunks")
    p.add_argument("--combined-jsonl", default="repos_with_about_url.jsonl")
    p.add_argument("--combined-txt", default="repo_urls_deduped_by_about_url.txt")
    p.add_argument("--combine-only", action="store_true")
    return p.parse_args()


def load_state(state_file: Path) -> dict:
    if state_file.exists():
        with state_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "committed_input_lines": 0,
        "unique_repo_checks": 0,
        "matches": 0,
        "chunks_completed": 0,
    }


def save_state(state_file: Path, state: dict) -> None:
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_file)


def split_repo_id(repo_id: str) -> Tuple[Optional[str], Optional[str]]:
    if not repo_id or "/" not in repo_id:
        return None, None
    owner, repo = repo_id.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return None, None
    return owner, repo


def canonicalize_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    url = url.strip()
    if not url:
        return None

    if url.startswith("//"):
        url = "https:" + url
    elif "://" not in url:
        url = "https://" + url

    try:
        parsed = urlsplit(url)
    except Exception:
        return None

    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.strip().lower()
    path = parsed.path or ""
    query = parsed.query or ""
    fragment = ""

    if not netloc:
        return None

    if "@" in netloc:
        _, netloc = netloc.rsplit("@", 1)

    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host

    if path == "/":
        path = ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False

    host = (parsed.netloc or "").lower().strip()
    if not host:
        return False

    if host.startswith("www."):
        host = host[4:]

    if host in {"youtube.com", "youtu.be", "youtube-nocookie.com"}:
        return True
    if host.endswith(".youtube.com"):
        return True
    if host.endswith(".youtu.be"):
        return True
    if host.endswith(".youtube-nocookie.com"):
        return True

    return False


def normalize_about_url(url: Optional[str]) -> Optional[str]:
    url = canonicalize_url(url)
    if not url:
        return None
    if is_youtube_url(url):
        return None
    return url


def normalize_repo_url(url: Optional[str]) -> Optional[str]:
    return canonicalize_url(url)


def get_session(token: str) -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token,
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": "shortlist-about-url-checker",
            }
        )
        _thread_local.session = session
    return session


def sleep_for_rate_limit(resp: requests.Response, fallback_seconds: int = 60) -> None:
    retry_after = resp.headers.get("Retry-After")
    reset = resp.headers.get("X-RateLimit-Reset")

    if retry_after:
        wait_s = max(int(float(retry_after)), 1) + 1
    elif reset:
        wait_s = max(int(reset) - int(time.time()), 1) + 1
    else:
        wait_s = fallback_seconds

    print("[rate-limit] sleeping {}s".format(wait_s), flush=True)
    time.sleep(wait_s)


def fetch_repo_about(repo_id: str, token: str) -> Optional[Dict[str, Optional[str]]]:
    owner, repo = split_repo_id(repo_id)
    if not owner or not repo:
        return None

    session = get_session(token)
    url = "{}/repos/{}/{}".format(
        GITHUB_API_BASE,
        quote(owner, safe=""),
        quote(repo, safe=""),
    )
    backoff = 3

    while True:
        try:
            resp = session.get(url, timeout=(10, 30))
        except requests.RequestException as e:
            print("[network-error] {}: {} -> retry in {}s".format(repo_id, e, backoff), flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue

        if resp.status_code == 200:
            data = resp.json()

            about_url = normalize_about_url(data.get("homepage"))
            if not about_url:
                return None

            repo_url = normalize_repo_url(data.get("html_url"))
            if not repo_url:
                return None

            return {
                "repo_id": repo_id,
                "repo_url": repo_url,
                "about_url": about_url,
                "about_description": data.get("description"),
            }

        if resp.status_code in (403, 429):
            sleep_for_rate_limit(resp)
            continue

        if resp.status_code in (500, 502, 503, 504):
            print(
                "[server-error] {}: HTTP {} -> retry in {}s".format(
                    repo_id, resp.status_code, backoff
                ),
                flush=True,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue

        if resp.status_code == 404:
            return None

        print("[skip] {}: HTTP {}".format(repo_id, resp.status_code), flush=True)
        return None


def load_repo_ids_from_jsonl(in_path: Path, skip_lines: int = 0, limit: Optional[int] = None):
    repo_ids = []
    seen: Set[str] = set()
    lines_read = 0

    with in_path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if idx < skip_lines:
                continue

            lines_read += 1

            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            repo_id = row.get("repo_id")
            if not isinstance(repo_id, str):
                continue

            repo_id = repo_id.strip()
            if not repo_id:
                continue

            if repo_id in seen:
                continue

            seen.add(repo_id)
            repo_ids.append(repo_id)

            if limit is not None and len(repo_ids) >= limit:
                break

    return lines_read, repo_ids


def process_chunk_to_file(repo_ids, token: str, workers: int, out_tmp_file: Path) -> int:
    matches = 0

    with out_tmp_file.open("w", encoding="utf-8") as out, ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_repo_about, repo_id, token): repo_id for repo_id in repo_ids}

        completed = 0
        for fut in as_completed(futures):
            repo_id = futures[fut]
            completed += 1

            try:
                result = fut.result()
            except Exception as e:
                print("[worker-error] {}: {}".format(repo_id, e), flush=True)
                continue

            if result is not None:
                out.write(json.dumps(result, ensure_ascii=False) + "\n")
                matches += 1

            if completed % 500 == 0:
                print(
                    "[chunk-progress] completed={}/{} matches={}".format(
                        completed, len(repo_ids), matches
                    ),
                    flush=True,
                )

        out.flush()
        os.fsync(out.fileno())

    return matches


def combine_chunks(chunks_dir: Path, combined_jsonl: Path, combined_txt: Path) -> None:
    chunk_files = sorted(chunks_dir.glob("matches_*.jsonl"))

    tmp_jsonl = combined_jsonl.with_suffix(combined_jsonl.suffix + ".tmp")
    tmp_txt = combined_txt.with_suffix(combined_txt.suffix + ".tmp")

    total_records = 0
    unique_about_urls_seen: Set[str] = set()
    repo_urls_written = 0

    with tmp_jsonl.open("w", encoding="utf-8") as jout, tmp_txt.open("w", encoding="utf-8") as tout:
        for chunk_file in chunk_files:
            with chunk_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue

                    jout.write(line + "\n")
                    total_records += 1

                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    about_url = normalize_about_url(rec.get("about_url"))
                    if not about_url:
                        continue

                    if about_url in unique_about_urls_seen:
                        continue

                    unique_about_urls_seen.add(about_url)

                    repo_url = normalize_repo_url(rec.get("repo_url"))
                    if not repo_url:
                        continue

                    tout.write(repo_url + "\n")
                    repo_urls_written += 1

        jout.flush()
        tout.flush()
        os.fsync(jout.fileno())
        os.fsync(tout.fileno())

    os.replace(tmp_jsonl, combined_jsonl)
    os.replace(tmp_txt, combined_txt)

    print(
        "[combine] wrote {} repo records to {} and {} final repo URLs to {} (deduped by about_url)".format(
            total_records, combined_jsonl, repo_urls_written, combined_txt
        ),
        flush=True,
    )


def main():
    args = parse_args()

    in_path = Path(args.in_path)
    state_file = Path(args.state_file)
    chunks_dir = Path(args.chunks_dir)
    combined_jsonl = Path(args.combined_jsonl)
    combined_txt = Path(args.combined_txt)

    chunks_dir.mkdir(parents=True, exist_ok=True)

    if args.combine_only:
        combine_chunks(chunks_dir, combined_jsonl, combined_txt)
        return

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    state = load_state(state_file)

    if args.limit is not None and state["unique_repo_checks"] >= args.limit:
        print(
            "[done] limit already reached in existing state: unique_repo_checks={} limit={}".format(
                state["unique_repo_checks"], args.limit
            ),
            flush=True,
        )
        combine_chunks(chunks_dir, combined_jsonl, combined_txt)
        return

    remaining_limit = None
    if args.limit is not None:
        remaining_limit = args.limit - state["unique_repo_checks"]
        if remaining_limit <= 0:
            print("[done] reached requested limit", flush=True)
            combine_chunks(chunks_dir, combined_jsonl, combined_txt)
            return

    print(
        "[start] input={} committed_input_lines={} workers={} limit={}".format(
            in_path,
            state["committed_input_lines"],
            args.workers,
            args.limit,
        ),
        flush=True,
    )

    lines_read, repo_ids = load_repo_ids_from_jsonl(
        in_path,
        skip_lines=state["committed_input_lines"],
        limit=remaining_limit,
    )

    if not repo_ids:
        print("[done] no repo_ids found to process", flush=True)
        combine_chunks(chunks_dir, combined_jsonl, combined_txt)
        return

    chunk_start = state["committed_input_lines"]
    chunk_end = chunk_start + lines_read

    tmp_chunk = chunks_dir / "matches_{:012d}_{:012d}.jsonl.tmp".format(chunk_start, chunk_end)
    final_chunk = chunks_dir / "matches_{:012d}_{:012d}.jsonl".format(chunk_start, chunk_end)

    print(
        "[chunk] input lines {} -> {} | unique repos to check={}".format(
            chunk_start, chunk_end, len(repo_ids)
        ),
        flush=True,
    )

    matches_in_chunk = process_chunk_to_file(repo_ids, token, args.workers, tmp_chunk)
    os.replace(tmp_chunk, final_chunk)

    state["committed_input_lines"] = chunk_end
    state["unique_repo_checks"] += len(repo_ids)
    state["matches"] += matches_in_chunk
    state["chunks_completed"] += 1
    save_state(state_file, state)

    print(
        "[committed] committed_input_lines={} unique_repo_checks={} matches={} chunks_completed={}".format(
            state["committed_input_lines"],
            state["unique_repo_checks"],
            state["matches"],
            state["chunks_completed"],
        ),
        flush=True,
    )

    combine_chunks(chunks_dir, combined_jsonl, combined_txt)
    print("[final] JSONL: {} | FINAL REPO URL TXT: {}".format(combined_jsonl, combined_txt), flush=True)


if __name__ == "__main__":
    main()
