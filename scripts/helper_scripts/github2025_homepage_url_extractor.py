#!/usr/bin/env python3
import argparse
import itertools
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Set
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from datasets import load_dataset

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

_thread_local = threading.local()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="nick007x/github-code-2025")
    p.add_argument("--split", default="train")
    p.add_argument("--rows-per-chunk", type=int, default=20000)
    p.add_argument("--workers", type=int, default=16)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of unique repos to check after adjacent dedupe",
    )
    p.add_argument("--state-file", default="gh25_about_url_state.json")
    p.add_argument("--chunks-dir", default="about_url_chunks")
    p.add_argument(
        "--combined-jsonl",
        default="repos_with_about_url.jsonl",
        help="All matching repo records before final dedupe by about_url",
    )
    p.add_argument(
        "--combined-txt",
        default="repo_urls_deduped_by_about_url.txt",
        help="Final repo URLs, deduped by normalized about_url and excluding YouTube about_urls",
    )
    p.add_argument(
        "--checkpoint-every",
        type=int,
        default=10000,
        help="Refresh combined JSONL/TXT outputs after this many unique repo checks. Use 0 to disable periodic combine.",
    )
    p.add_argument("--combine-only", action="store_true")
    return p.parse_args()


def load_state(state_file: Path) -> dict:
    if state_file.exists():
        with state_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "committed_rows": 0,
        "last_repo_id": None,
        "unique_repo_checks": 0,
        "matches": 0,
        "chunks_completed": 0,
        "last_repo_url_checkpoint": 0,
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
                "User-Agent": "gh25-about-url-checker",
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


def stream_unique_repo_ids(
    ds_iter,
    max_rows: int,
    previous_repo_id: Optional[str],
    max_unique_repos: Optional[int] = None,
):
    rows_read = 0
    last_repo_id = previous_repo_id
    repos_to_check: List[str] = []

    while rows_read < max_rows:
        if max_unique_repos is not None and len(repos_to_check) >= max_unique_repos:
            break

        try:
            row = next(ds_iter)
        except StopIteration:
            break

        rows_read += 1
        repo_id = row.get("repo_id")

        if not isinstance(repo_id, str):
            continue

        repo_id = repo_id.strip()
        if not repo_id:
            continue

        # Your exact rule:
        # skip only if current repo_id == immediately previous row repo_id
        if repo_id == last_repo_id:
            continue

        repos_to_check.append(repo_id)
        last_repo_id = repo_id

    return rows_read, last_repo_id, repos_to_check


def process_chunk_to_file(repo_ids: List[str], token: str, workers: int, out_tmp_file: Path) -> int:
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

    # Backward compatibility for older state files created before this checkpoint field existed.
    state.setdefault("last_repo_url_checkpoint", 0)

    committed_rows = int(state["committed_rows"])
    last_repo_id = state["last_repo_id"]

    checkpoint_every = max(int(args.checkpoint_every), 0)

    # If resuming from an old/interrupted run where chunks exist but combined outputs may be stale,
    # refresh repo_urls_deduped_by_about_url.txt once before continuing.
    if checkpoint_every > 0 and int(state["unique_repo_checks"]) > int(state.get("last_repo_url_checkpoint", 0)):
        print(
            "[checkpoint-resume] refreshing combined repo URL file before continuing",
            flush=True,
        )
        combine_chunks(chunks_dir, combined_jsonl, combined_txt)
        state["last_repo_url_checkpoint"] = int(state["unique_repo_checks"])
        save_state(state_file, state)

    if args.limit is not None and state["unique_repo_checks"] >= args.limit:
        print(
            "[done] limit already reached in existing state: unique_repo_checks={} limit={}".format(
                state["unique_repo_checks"], args.limit
            ),
            flush=True,
        )
        combine_chunks(chunks_dir, combined_jsonl, combined_txt)
        state["last_repo_url_checkpoint"] = int(state["unique_repo_checks"])
        save_state(state_file, state)
        return

    print(
        "[start] dataset={} split={} committed_rows={} workers={} rows_per_chunk={} limit={}".format(
            args.dataset,
            args.split,
            committed_rows,
            args.workers,
            args.rows_per_chunk,
            args.limit,
        ),
        flush=True,
    )

    ds = load_dataset(args.dataset, split=args.split, streaming=True)
    ds_iter = iter(ds)

    if committed_rows > 0:
        print("[resume] skipping first {} streamed rows".format(committed_rows), flush=True)
        ds_iter = itertools.islice(ds_iter, committed_rows, None)

    while True:
        remaining_limit = None
        if args.limit is not None:
            remaining_limit = args.limit - state["unique_repo_checks"]
            if remaining_limit <= 0:
                print("[done] reached requested limit", flush=True)
                break

        chunk_unique_cap = args.rows_per_chunk

        if remaining_limit is not None:
            chunk_unique_cap = min(chunk_unique_cap, remaining_limit)

        # Do not let one work unit cross the repo-URL checkpoint boundary.
        # This makes --checkpoint-every 10000 mean roughly every 10k checked unique repos,
        # even if --rows-per-chunk is larger.
        if checkpoint_every > 0:
            checked_since_last_checkpoint = int(state["unique_repo_checks"]) - int(
                state.get("last_repo_url_checkpoint", 0)
            )
            repos_until_next_checkpoint = checkpoint_every - checked_since_last_checkpoint
            if repos_until_next_checkpoint <= 0:
                repos_until_next_checkpoint = checkpoint_every
            chunk_unique_cap = min(chunk_unique_cap, repos_until_next_checkpoint)

        chunk_start = committed_rows

        rows_read, chunk_last_repo_id, repo_ids = stream_unique_repo_ids(
            ds_iter,
            args.rows_per_chunk,
            last_repo_id,
            max_unique_repos=chunk_unique_cap,
        )

        if rows_read == 0:
            print("[done] no more rows", flush=True)
            break

        chunk_end = chunk_start + rows_read
        tmp_chunk = chunks_dir / "matches_{:012d}_{:012d}.jsonl.tmp".format(chunk_start, chunk_end)
        final_chunk = chunks_dir / "matches_{:012d}_{:012d}.jsonl".format(chunk_start, chunk_end)

        print(
            "[chunk] rows {} -> {} | unique repos to check={}".format(
                chunk_start, chunk_end, len(repo_ids)
            ),
            flush=True,
        )

        matches_in_chunk = process_chunk_to_file(repo_ids, token, args.workers, tmp_chunk)
        os.replace(tmp_chunk, final_chunk)

        committed_rows = chunk_end
        last_repo_id = chunk_last_repo_id
        state["committed_rows"] = committed_rows
        state["last_repo_id"] = last_repo_id
        state["unique_repo_checks"] += len(repo_ids)
        state["matches"] += matches_in_chunk
        state["chunks_completed"] += 1
        save_state(state_file, state)

        print(
            "[committed] rows={} unique_repo_checks={} matches={} chunks_completed={}".format(
                state["committed_rows"],
                state["unique_repo_checks"],
                state["matches"],
                state["chunks_completed"],
            ),
            flush=True,
        )

        if checkpoint_every > 0:
            checked_since_last_checkpoint = int(state["unique_repo_checks"]) - int(
                state.get("last_repo_url_checkpoint", 0)
            )
            if checked_since_last_checkpoint >= checkpoint_every:
                print(
                    "[checkpoint] refreshing {} and {} after {} unique repo checks".format(
                        combined_jsonl,
                        combined_txt,
                        state["unique_repo_checks"],
                    ),
                    flush=True,
                )
                combine_chunks(chunks_dir, combined_jsonl, combined_txt)
                state["last_repo_url_checkpoint"] = int(state["unique_repo_checks"])
                save_state(state_file, state)

        if args.limit is not None and state["unique_repo_checks"] >= args.limit:
            print("[done] reached requested limit", flush=True)
            break

        # rows_read < rows_per_chunk can also happen because we intentionally stopped
        # at checkpoint/limit unique-repo cap. Only treat it as end-of-stream if we did
        # not fill the unique cap.
        if rows_read < args.rows_per_chunk and len(repo_ids) < chunk_unique_cap:
            print("[done] reached end of stream", flush=True)
            break

    combine_chunks(chunks_dir, combined_jsonl, combined_txt)
    state["last_repo_url_checkpoint"] = int(state["unique_repo_checks"])
    save_state(state_file, state)
    print("[final] JSONL: {} | FINAL REPO URL TXT: {}".format(combined_jsonl, combined_txt), flush=True)


if __name__ == "__main__":
    main()
