import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from urllib.parse import urlparse

import requests
from tqdm import tqdm


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
PSI_KEY = os.environ.get("GOOGLE_PAGESPEED_INSIGHTS_API_KEY", "").strip()

GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "batch-crux-checker",
}

if GITHUB_TOKEN:
    GITHUB_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


SUMMARY_FIELDS = [
    "key",
    "input",
    "input_kind",
    "repo_id",
    "repo_url",
    "homepage",
    "checked_url",
    "status",
    "github_status",
    "psi_status",
    "url_crux_found",
    "origin_crux_found",
    "url_p75_lcp",
    "url_p75_cls",
    "url_p75_inp",
    "url_p75_fcp",
    "url_p75_ttfb",
    "origin_p75_lcp",
    "origin_p75_cls",
    "origin_p75_inp",
    "origin_p75_fcp",
    "origin_p75_ttfb",
    "error",
]


def now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_dir(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def safe_str(x):
    if x is None:
        return ""
    return str(x).strip()


def normalize_url(url):
    url = safe_str(url)

    if not url:
        return ""

    if url.startswith("git@github.com:"):
        return url

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return url.rstrip("/")


def parse_repo_id(value):
    """
    Accepts:
    https://github.com/owner/repo
    https://github.com/owner/repo.git
    git@github.com:owner/repo.git
    owner/repo
    """
    s = safe_str(value)

    if not s or s.startswith("#"):
        return None

    s = s.split()[0].strip()

    if s.startswith("git@github.com:"):
        s = s.replace("git@github.com:", "", 1).removesuffix(".git")
        parts = [p for p in s.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"

    if s.startswith(("http://", "https://")):
        parsed = urlparse(s)

        if "github.com" not in parsed.netloc.lower():
            return None

        parts = [p for p in parsed.path.strip("/").split("/") if p]

        if len(parts) >= 2:
            owner = parts[0]
            repo = parts[1].removesuffix(".git")
            return f"{owner}/{repo}"

        return None

    if "/" in s and "github.com" not in s:
        parts = [p for p in s.strip("/").split("/") if p]
        if len(parts) >= 2:
            owner = parts[0]
            repo = parts[1].removesuffix(".git")
            return f"{owner}/{repo}"

    return None


def repo_url(repo_id):
    return f"https://github.com/{repo_id}"


def make_key(row):
    if row["input_kind"] == "repo":
        return row["repo_id"]
    return row["candidate_url"]


def append_line(path, line, fsync=False):
    with open(path, "a", encoding="utf-8") as f:
        f.write(str(line).rstrip() + "\n")
        f.flush()
        if fsync:
            os.fsync(f.fileno())


def append_jsonl(path, obj, fsync=False):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        if fsync:
            os.fsync(f.fileno())


def append_csv(path, fieldnames, row, fsync=False):
    exists = os.path.exists(path)

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not exists:
            writer.writeheader()

        safe_row = {k: row.get(k, "") for k in fieldnames}
        writer.writerow(safe_row)

        f.flush()
        if fsync:
            os.fsync(f.fileno())


def read_jsonl_done_key(path):
    done = set()

    if not os.path.exists(path):
        return done

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except Exception:
                continue

            key = obj.get("key") or obj.get("repo_id") or obj.get("checked_url") or obj.get("input")
            if key:
                done.add(str(key))

    return done


def read_summary_done_key(path):
    done = set()

    if not os.path.exists(path):
        return done

    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = row.get("key") or row.get("repo_id") or row.get("checked_url")
                if key:
                    done.add(str(key))
    except Exception:
        pass

    return done


def load_done(output_dir):
    """
    Resume logic:
    1. checkpoint.jsonl is primary.
    2. summary.csv is fallback, in case result was written but checkpoint write was interrupted.
    """
    out = Path(output_dir)
    done = set()

    done |= read_jsonl_done_key(out / "checkpoint.jsonl")
    done |= read_summary_done_key(out / "summary.csv")

    return done


def load_input_rows(input_path, input_kind, limit=None):
    rows = []
    seen = set()

    with open(input_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            raw = line.strip()

            if not raw or raw.startswith("#"):
                continue

            raw = raw.split()[0].strip()

            if input_kind == "repo":
                rid = parse_repo_id(raw)

                if not rid:
                    continue

                key = rid

                if key in seen:
                    continue

                seen.add(key)

                rows.append(
                    {
                        "input": raw,
                        "input_kind": "repo",
                        "repo_id": rid,
                        "repo_url": repo_url(rid),
                        "candidate_url": "",
                    }
                )

            elif input_kind == "url":
                url = normalize_url(raw)

                if not url:
                    continue

                key = url

                if key in seen:
                    continue

                seen.add(key)

                rows.append(
                    {
                        "input": raw,
                        "input_kind": "url",
                        "repo_id": "",
                        "repo_url": "",
                        "candidate_url": url,
                    }
                )

            else:
                raise ValueError(f"Invalid input_kind: {input_kind}")

            if limit is not None and len(rows) >= limit:
                break

    return rows


def request_with_retries(url, headers=None, params=None, timeout=30, max_retries=4, sleep_base=2):
    last_error = None

    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=timeout)

            if r.status_code in [429, 500, 502, 503, 504]:
                time.sleep(sleep_base * (attempt + 1))
                continue

            return r

        except requests.RequestException as e:
            last_error = e
            time.sleep(sleep_base * (attempt + 1))

    raise RuntimeError(f"request_failed_after_retries: {repr(last_error)}")


def fetch_github_repo_info(repo_id):
    out = {
        "github_status": None,
        "homepage": "",
        "description": "",
        "error": None,
    }

    try:
        owner, repo = repo_id.split("/", 1)
    except ValueError:
        out["error"] = "bad_repo_id"
        return out

    api_url = f"https://api.github.com/repos/{owner}/{repo}"

    try:
        r = request_with_retries(api_url, headers=GITHUB_HEADERS, timeout=30)
    except Exception as e:
        out["error"] = f"github_request_failed: {repr(e)}"
        return out

    out["github_status"] = r.status_code

    if r.status_code != 200:
        out["error"] = f"github_status_{r.status_code}: {r.text[:300]}"
        return out

    try:
        data = r.json()
    except Exception as e:
        out["error"] = f"github_json_failed: {repr(e)}"
        return out

    if not isinstance(data, dict):
        out["error"] = "github_json_not_dict"
        return out

    homepage = safe_str(data.get("homepage"))
    description = safe_str(data.get("description"))

    out["homepage"] = normalize_url(homepage) if homepage else ""
    out["description"] = description

    return out


def build_candidate_urls_from_repo(repo_id, github_info, try_github_pages=False):
    candidates = []

    homepage = safe_str(github_info.get("homepage"))
    if homepage:
        candidates.append(homepage)

    if try_github_pages:
        try:
            owner, repo = repo_id.split("/", 1)
            candidates.append(f"https://{owner}.github.io/{repo}")
            candidates.append(f"https://{owner}.github.io")
        except Exception:
            pass

    cleaned = []
    seen = set()

    for url in candidates:
        url = normalize_url(url)
        if not url:
            continue

        parsed = urlparse(url)

        # Do not check GitHub repo page itself as the deployed website.
        if parsed.netloc.lower() == "github.com":
            continue

        if url in seen:
            continue

        seen.add(url)
        cleaned.append(url)

    return cleaned


def extract_metric_percentiles(crux_obj):
    """
    Accepts loadingExperience or originLoadingExperience object from PSI.
    Safely handles None.
    """
    crux_obj = crux_obj or {}

    if not isinstance(crux_obj, dict):
        return {}

    metrics = crux_obj.get("metrics") or {}

    if not isinstance(metrics, dict):
        return {}

    out = {}

    metric_map = {
        "LARGEST_CONTENTFUL_PAINT_MS": "lcp",
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls",
        "INTERACTION_TO_NEXT_PAINT": "inp",
        "FIRST_CONTENTFUL_PAINT_MS": "fcp",
        "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "ttfb",
        "TIME_TO_FIRST_BYTE": "ttfb",
    }

    for raw_name, short_name in metric_map.items():
        m = metrics.get(raw_name) or {}

        if not isinstance(m, dict):
            continue

        percentile = m.get("percentile")

        if percentile is not None:
            out[short_name] = percentile

    return out


def extract_crux_from_psi_json(data):
    data = data or {}

    if not isinstance(data, dict):
        data = {}

    loading = data.get("loadingExperience") or {}
    origin = data.get("originLoadingExperience") or {}

    if not isinstance(loading, dict):
        loading = {}

    if not isinstance(origin, dict):
        origin = {}

    loading_metrics = loading.get("metrics") or {}
    origin_metrics = origin.get("metrics") or {}

    if not isinstance(loading_metrics, dict):
        loading_metrics = {}

    if not isinstance(origin_metrics, dict):
        origin_metrics = {}

    url_crux_found = bool(loading_metrics)
    origin_crux_found = bool(origin_metrics)

    return {
        "url_crux_found": url_crux_found,
        "origin_crux_found": origin_crux_found,
        "url_crux": loading if url_crux_found else None,
        "origin_crux": origin if origin_crux_found else None,
        "url_metrics_p75": extract_metric_percentiles(loading),
        "origin_metrics_p75": extract_metric_percentiles(origin),
    }


def check_psi_crux(url, strategy):
    result = {
        "checked_url": url,
        "psi_status": None,
        "psi_error": None,
        "url_crux_found": False,
        "origin_crux_found": False,
        "url_crux": None,
        "origin_crux": None,
        "url_metrics_p75": {},
        "origin_metrics_p75": {},
    }

    params = {
        "url": url,
        "strategy": strategy,
        "category": "performance",
    }

    if PSI_KEY:
        params["key"] = PSI_KEY

    try:
        r = request_with_retries(
            PSI_ENDPOINT,
            headers={"User-Agent": "batch-crux-checker"},
            params=params,
            timeout=90,
            max_retries=4,
            sleep_base=3,
        )
    except Exception as e:
        result["psi_error"] = f"psi_request_failed: {repr(e)}"
        return result

    result["psi_status"] = r.status_code

    if r.status_code != 200:
        result["psi_error"] = f"psi_status_{r.status_code}: {r.text[:500]}"
        return result

    try:
        data = r.json()
    except Exception as e:
        result["psi_error"] = f"psi_json_failed: {repr(e)}"
        return result

    crux = extract_crux_from_psi_json(data)
    result.update(crux)

    return result


def process_one(row, strategy, try_github_pages):
    key = make_key(row)

    result = {
        "key": key,
        "input": row.get("input", ""),
        "input_kind": row.get("input_kind", ""),
        "repo_id": row.get("repo_id", ""),
        "repo_url": row.get("repo_url", ""),
        "homepage": "",
        "checked_url": "",
        "status": "UNKNOWN",
        "github_status": None,
        "psi_status": None,
        "url_crux_found": False,
        "origin_crux_found": False,
        "url_crux": None,
        "origin_crux": None,
        "url_metrics_p75": {},
        "origin_metrics_p75": {},
        "checked_candidates": [],
        "error": None,
        "created_at": now_ts(),
    }

    candidates = []

    if row["input_kind"] == "repo":
        rid = row["repo_id"]

        gh = fetch_github_repo_info(rid)

        result["github_status"] = gh.get("github_status")
        result["homepage"] = gh.get("homepage") or ""

        if gh.get("error"):
            result["status"] = "REPO_LOOKUP_FAILED"
            result["error"] = gh.get("error")
            return result

        candidates = build_candidate_urls_from_repo(
            rid,
            gh,
            try_github_pages=try_github_pages,
        )

        if not candidates:
            result["status"] = "NO_HOMEPAGE_URL"
            result["error"] = "No homepage URL found for repo"
            return result

    else:
        direct_url = row.get("candidate_url", "")
        if not direct_url:
            result["status"] = "NO_INPUT_URL"
            result["error"] = "No input URL"
            return result

        candidates = [direct_url]

    last_error = None
    last_psi_status = None

    for candidate in candidates:
        psi = check_psi_crux(candidate, strategy=strategy)

        candidate_record = {
            "candidate_url": candidate,
            "psi_status": psi.get("psi_status"),
            "psi_error": psi.get("psi_error"),
            "url_crux_found": bool(psi.get("url_crux_found")),
            "origin_crux_found": bool(psi.get("origin_crux_found")),
            "url_metrics_p75": psi.get("url_metrics_p75") or {},
            "origin_metrics_p75": psi.get("origin_metrics_p75") or {},
        }

        result["checked_candidates"].append(candidate_record)

        last_error = psi.get("psi_error")
        last_psi_status = psi.get("psi_status")

        if psi.get("url_crux_found") or psi.get("origin_crux_found"):
            result["checked_url"] = candidate
            result["psi_status"] = psi.get("psi_status")
            result["url_crux_found"] = bool(psi.get("url_crux_found"))
            result["origin_crux_found"] = bool(psi.get("origin_crux_found"))
            result["url_crux"] = psi.get("url_crux")
            result["origin_crux"] = psi.get("origin_crux")
            result["url_metrics_p75"] = psi.get("url_metrics_p75") or {}
            result["origin_metrics_p75"] = psi.get("origin_metrics_p75") or {}

            if result["url_crux_found"]:
                result["status"] = "URL_CRUX_FOUND"
            else:
                result["status"] = "ORIGIN_CRUX_FOUND"

            result["error"] = None
            return result

    result["checked_url"] = candidates[0] if candidates else ""
    result["psi_status"] = last_psi_status
    result["status"] = "NO_CRUX_FOUND"
    result["error"] = last_error

    return result


def get_metric(metrics, key):
    metrics = metrics or {}
    if not isinstance(metrics, dict):
        return ""
    return metrics.get(key, "")


def result_to_summary_row(result):
    result = result or {}

    url_m = result.get("url_metrics_p75") or {}
    origin_m = result.get("origin_metrics_p75") or {}

    return {
        "key": result.get("key", ""),
        "input": result.get("input", ""),
        "input_kind": result.get("input_kind", ""),
        "repo_id": result.get("repo_id", ""),
        "repo_url": result.get("repo_url", ""),
        "homepage": result.get("homepage", ""),
        "checked_url": result.get("checked_url", ""),
        "status": result.get("status", ""),
        "github_status": result.get("github_status", ""),
        "psi_status": result.get("psi_status", ""),
        "url_crux_found": bool(result.get("url_crux_found")),
        "origin_crux_found": bool(result.get("origin_crux_found")),
        "url_p75_lcp": get_metric(url_m, "lcp"),
        "url_p75_cls": get_metric(url_m, "cls"),
        "url_p75_inp": get_metric(url_m, "inp"),
        "url_p75_fcp": get_metric(url_m, "fcp"),
        "url_p75_ttfb": get_metric(url_m, "ttfb"),
        "origin_p75_lcp": get_metric(origin_m, "lcp"),
        "origin_p75_cls": get_metric(origin_m, "cls"),
        "origin_p75_inp": get_metric(origin_m, "inp"),
        "origin_p75_fcp": get_metric(origin_m, "fcp"),
        "origin_p75_ttfb": get_metric(origin_m, "ttfb"),
        "error": result.get("error", ""),
    }


def append_outputs(out_dir, result, fsync=False):
    out_dir = Path(out_dir)

    result = result or {}

    key = (
        result.get("key")
        or result.get("repo_id")
        or result.get("checked_url")
        or result.get("input")
        or "UNKNOWN"
    )

    append_jsonl(out_dir / "all_results.jsonl", result, fsync=fsync)

    append_csv(
        out_dir / "summary.csv",
        SUMMARY_FIELDS,
        result_to_summary_row(result),
        fsync=fsync,
    )

    checked_url = result.get("checked_url") or ""

    if result.get("url_crux_found") and checked_url:
        append_line(out_dir / "url_crux_found.txt", checked_url, fsync=fsync)

    if result.get("origin_crux_found") and checked_url:
        append_line(out_dir / "origin_crux_found.txt", checked_url, fsync=fsync)

    if result.get("error"):
        append_jsonl(out_dir / "errors.jsonl", result, fsync=fsync)

    # Checkpoint is written LAST, only after all outputs are written.
    append_jsonl(
        out_dir / "checkpoint.jsonl",
        {
            "key": key,
            "repo_id": result.get("repo_id", ""),
            "checked_url": checked_url,
            "status": result.get("status", ""),
            "url_crux_found": bool(result.get("url_crux_found")),
            "origin_crux_found": bool(result.get("origin_crux_found")),
            "error": result.get("error", ""),
            "created_at": now_ts(),
        },
        fsync=fsync,
    )


def process_finished_futures(futures, out_dir, fsync=False):
    completed = 0

    for fut in list(futures):
        if not fut.done():
            continue

        futures.remove(fut)

        try:
            result = fut.result()
        except Exception as e:
            result = {
                "key": "UNKNOWN",
                "input": "",
                "input_kind": "",
                "repo_id": "",
                "repo_url": "",
                "homepage": "",
                "checked_url": "",
                "status": "FATAL_WORKER_ERROR",
                "github_status": "",
                "psi_status": "",
                "url_crux_found": False,
                "origin_crux_found": False,
                "url_metrics_p75": {},
                "origin_metrics_p75": {},
                "error": repr(e),
                "created_at": now_ts(),
            }

        append_outputs(out_dir, result, fsync=fsync)

        status = result.get("status", "UNKNOWN")
        target = (
            result.get("checked_url")
            or result.get("homepage")
            or result.get("repo_url")
            or result.get("input")
            or ""
        )

        print(f"\nDONE: {status} | {target}", flush=True)

        completed += 1

    return completed


def dedupe_text_file(path):
    path = Path(path)

    if not path.exists():
        return

    seen = set()
    lines = []

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            lines.append(s)

    with open(path, "w", encoding="utf-8") as f:
        for s in lines:
            f.write(s + "\n")


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", default="crux_out")
    parser.add_argument("--input-kind", choices=["repo", "url"], default="repo")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-pending", type=int, default=16)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--strategy", choices=["mobile", "desktop"], default="mobile")
    parser.add_argument("--try-github-pages", action="store_true")
    parser.add_argument("--fsync", action="store_true")
    parser.add_argument("--dedupe-output", action="store_true")

    args = parser.parse_args()

    ensure_dir(args.output_dir)

    rows = load_input_rows(args.input, args.input_kind, limit=args.limit)
    done = load_done(args.output_dir)

    pending = []
    for row in rows:
        key = make_key(row)
        if key in done:
            continue
        pending.append(row)

    print("=== Batch CrUX Check ===")
    print("Input rows unique:", len(rows))
    print("Already processed:", len(done))
    print("Pending:", len(pending))
    print("Workers:", args.workers)
    print("Max pending:", args.max_pending)
    print("Output dir:", args.output_dir)
    print("Input kind:", args.input_kind)
    print("Strategy:", args.strategy)
    print("GitHub token present:", bool(GITHUB_TOKEN))
    print("PSI key present:", bool(PSI_KEY))
    print("URL_CRUX_FOUND file:", Path(args.output_dir) / "url_crux_found.txt")
    print()

    futures = set()
    submitted = 0
    completed_total = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        pbar = tqdm(total=len(pending))

        for row in pending:
            futures.add(
                executor.submit(
                    process_one,
                    row,
                    args.strategy,
                    args.try_github_pages,
                )
            )

            submitted += 1

            if len(futures) >= args.max_pending:
                wait(futures, return_when=FIRST_COMPLETED)

                completed = process_finished_futures(
                    futures,
                    args.output_dir,
                    fsync=args.fsync,
                )

                completed_total += completed
                pbar.update(completed)

        while futures:
            wait(futures, return_when=FIRST_COMPLETED)

            completed = process_finished_futures(
                futures,
                args.output_dir,
                fsync=args.fsync,
            )

            completed_total += completed
            pbar.update(completed)

        pbar.close()

    if args.dedupe_output:
        dedupe_text_file(Path(args.output_dir) / "url_crux_found.txt")
        dedupe_text_file(Path(args.output_dir) / "origin_crux_found.txt")

    print()
    print("=== Finished ===")
    print("Submitted this run:", submitted)
    print("Completed this run:", completed_total)
    print("All results:", Path(args.output_dir) / "all_results.jsonl")
    print("Checkpoint:", Path(args.output_dir) / "checkpoint.jsonl")
    print("Summary CSV:", Path(args.output_dir) / "summary.csv")
    print("URL_CRUX_FOUND:", Path(args.output_dir) / "url_crux_found.txt")
    print("ORIGIN_CRUX_FOUND:", Path(args.output_dir) / "origin_crux_found.txt")
    print("Errors:", Path(args.output_dir) / "errors.jsonl")


if __name__ == "__main__":
    main()
