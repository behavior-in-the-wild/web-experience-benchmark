#!/usr/bin/env python3
"""
Classify HuggingFace dataset repos into site categories using LLM prompts.

Features:
- Loads HF dataset rows
- Clones repos and selects important files locally
- Builds a prompt with a token budget and trims file contents proportionally
- Calls a chat LLM (OpenAI-compatible API) or writes prompts to JSONL
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import subprocess
import time
from threading import Lock
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from datasets import load_dataset
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed


# ---------------------------
# CONFIG
# ---------------------------
DEFAULT_DATASET = "behavior-in-the-wild/cwv-bench-v0"
DEFAULT_SPLIT = "train"
DEFAULT_MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT", os.getenv("AZURE_DEPLOYMENT", "gpt-5"))
DEFAULT_MAX_PROMPT_TOKENS = 6000
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("AZURE_OPENAI_MAX_COMPLETION_TOKENS", "2000"))
DEFAULT_MAX_FILES = 6
DEFAULT_MAX_FILE_BYTES = 50_000

AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")


# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------
# DATA MODELS
# ---------------------------
# ---------------------------
# HELPERS
# ---------------------------
def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def score_path(path: str) -> int:
    p = path.lower()
    base = p.split("/")[-1]
    score = 0
    if base in {"readme.md", "readme", "readme.txt"}:
        score += 100
    if base in {"index.html", "index.htm"}:
        score += 90
    if base in {"package.json", "pyproject.toml", "requirements.txt", "pipfile", "gemfile"}:
        score += 70
    if base in {"_config.yml", "_config.yaml", "config.toml", "config.yaml", "config.yml"}:
        score += 60
    if base in {"mkdocs.yml", "mkdocs.yaml", "docusaurus.config.js", "gatsby-config.js"}:
        score += 60
    if "docs" in p:
        score += 20
    if "blog" in p:
        score += 15
    if "posts" in p:
        score += 10
    if base.endswith(".md"):
        score += 8
    if base.endswith(".html"):
        score += 12
    if base.endswith(".vue") or base.endswith(".svelte"):
        score += 8
    return score


def select_important_files(metadata_files: Optional[List[dict]], max_files: int) -> List[dict]:
    if not metadata_files:
        return []
    scored = []
    for f in metadata_files:
        path = f.get("PATH") or f.get("path")
        if not path:
            continue
        scored.append((score_path(path), f))
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [f for _, f in scored[:max_files]]
    return selected


def read_local_file(path: Path, max_bytes: int) -> Optional[str]:
    try:
        if max_bytes and path.stat().st_size > max_bytes:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def clone_repo(repo_id: str, clone_root: Path) -> Optional[Path]:
    clone_root.mkdir(parents=True, exist_ok=True)
    safe_name = repo_id.replace("/", "__")
    dest = clone_root / safe_name
    if dest.exists() and any(dest.iterdir()):
        return dest

    url = f"https://github.com/{repo_id}.git"
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        return dest if dest.exists() and any(dest.iterdir()) else None
    except Exception:
        return None


def discover_important_files(repo_path: Path, max_files: int) -> List[Path]:
    priority_names = {
        "readme.md",
        "readme",
        "readme.txt",
        "index.html",
        "index.htm",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "pipfile",
        "gemfile",
        "_config.yml",
        "_config.yaml",
        "config.toml",
        "config.yaml",
        "config.yml",
        "mkdocs.yml",
        "mkdocs.yaml",
        "docusaurus.config.js",
        "gatsby-config.js",
    }

    candidates: List[Path] = []
    for path in repo_path.rglob("*"):
        if path.is_file():
            name = path.name.lower()
            if name in priority_names or name.endswith((".md", ".html", ".htm", ".vue", ".svelte")):
                candidates.append(path)

    scored = []
    for path in candidates:
        rel = str(path.relative_to(repo_path))
        scored.append((score_path(rel), path))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in scored[:max_files]]


def trim_contents_proportionally(
    file_contents: List[Tuple[str, str]],
    max_chars: int,
) -> List[Tuple[str, str]]:
    total_len = sum(len(c) for _, c in file_contents)
    if total_len <= max_chars:
        return file_contents
    if total_len == 0:
        return file_contents

    ratio = max_chars / total_len
    logger.debug(
        "Trimming file contents to %d chars (ratio=%.4f, total_len=%d)",
        max_chars,
        ratio,
        total_len,
    )
    trimmed = []
    for path, content in file_contents:
        allowed = int(len(content) * ratio)
        if allowed <= 0:
            trimmed.append((path, ""))
            continue
        if allowed < len(content):
            snippet = content[:allowed] + "\n...[truncated]"
        else:
            snippet = content
        trimmed.append((path, snippet))
    return trimmed


def build_prompt(
    dataset_row: dict,
    file_contents: List[Tuple[str, str]],
    max_prompt_tokens: int,
) -> Tuple[str, int]:
    repo_id = (dataset_row.get("REPO_ID") or dataset_row.get("repo_id") or dataset_row.get("repo_name") or "").strip()

    meta_lines = [f"Repo: {repo_id}"]

    dataset_lines = []
    if dataset_row.get("SOURCE") or dataset_row.get("source"):
        dataset_lines.append(f"Source: {dataset_row.get('SOURCE') or dataset_row.get('source')}")
    if dataset_row.get("checked_url"):
        dataset_lines.append(f"Checked URL: {dataset_row.get('checked_url')}")
    if dataset_row.get("METADATA"):
        metadata = dataset_row["METADATA"]
        if isinstance(metadata, dict):
            size_stats = metadata.get("SIZE_STATS")
            if size_stats:
                dataset_lines.append(f"Repo size stats: {size_stats}")

    static_prompt = """You are classifying a website repository into the smallest possible set of categories.

Categories (choose the minimum number necessary, usually 1):
- Personal / Portfolio
- Blog / Media
- Documentation / Docs
- E-commerce / Store
- Landing Page / Product
- Web Application / Dashboard
- Educational / Course
- Community / Forum
- News / Publication
- Entertainment / Arts
- Government / NGO / Nonprofit
- Developer / Code Showcase
- Security / Malware / Phishing (flag)
- Other / Utility

Return JSON only with:
{
  "categories": ["..."],
  "reason": "short rationale",
  "confidence": 0-1
}

Use the repo metadata and file contents below to decide.
"""

    static_sections = "\n".join([
        static_prompt,
        "\n[Repository Metadata]",
        "\n".join(meta_lines),
        "\n[Dataset Metadata]",
        "\n".join(dataset_lines) if dataset_lines else "None",
        "\n[Important File Contents]",
    ])

    base_tokens = estimate_tokens(static_sections)
    available_tokens = max(0, max_prompt_tokens - base_tokens)
    max_chars_for_files = available_tokens * 4

    trimmed_files = trim_contents_proportionally(file_contents, max_chars_for_files)
    if file_contents and max_chars_for_files < sum(len(c) for _, c in file_contents):
        logger.info(
            "Prompt budget reached, trimming file contents to %d chars", max_chars_for_files
        )

    file_sections = []
    for path, content in trimmed_files:
        file_sections.append(f"\n--- {path} ---\n{content}")

    final_prompt = static_sections + "\n" + "\n".join(file_sections)
    return final_prompt, estimate_tokens(final_prompt)


def call_azure_openai_chat(model: str, prompt: str, max_output_tokens: int) -> dict:
    try:
        from openai import AzureOpenAI
        from openai import APIError, APITimeoutError, BadRequestError, RateLimitError
    except ImportError as exc:
        raise RuntimeError("Missing openai package") from exc

    if not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_ENDPOINT:
        raise RuntimeError("Missing Azure OpenAI credentials")

    client = AzureOpenAI(
        api_key=AZURE_OPENAI_API_KEY,
        api_version=AZURE_OPENAI_API_VERSION,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
    )

    try:
        request_kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a careful classifier."},
                {"role": "user", "content": prompt},
            ],
        }

        if model.startswith("gpt-5"):
            request_kwargs["max_completion_tokens"] = max_output_tokens
        else:
            request_kwargs["temperature"] = 0.2
            request_kwargs["max_tokens"] = max_output_tokens

        response = client.chat.completions.create(**request_kwargs)
        return response.model_dump()
    except BadRequestError as exc:
        logger.warning("Azure OpenAI 400 error: %s", exc)
        raise
    except (RateLimitError, APITimeoutError, APIError) as exc:
        logger.warning("Azure OpenAI error: %s", exc)
        raise


def extract_categories(llm_response: Optional[dict]) -> List[str]:
    if not llm_response:
        return []
    try:
        choices = llm_response.get("choices") or []
        if not choices:
            return []
        content = choices[0].get("message", {}).get("content", "")
        if not content:
            return []
        parsed = json.loads(content)
        categories = parsed.get("categories")
        if isinstance(categories, list):
            return [str(c) for c in categories]
    except Exception:
        return []
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify HF repos into categories using LLM prompts")
    parser.add_argument("--dataset", default=DEFAULT_DATASET, help="HF dataset name")
    parser.add_argument("--split", default=DEFAULT_SPLIT, help="Dataset split")
    parser.add_argument("--start", type=int, default=0, help="Start index")
    parser.add_argument("--limit", type=int, default=0, help="Max number of rows to process (0 = all)")
    parser.add_argument("--output", default="repo_classifications.jsonl", help="Output JSONL path")
    parser.add_argument("--use-clone", action="store_true", help="Clone repo and read files locally")
    parser.add_argument("--no-clone", action="store_true", help="Disable cloning and use GitHub API only")
    parser.add_argument(
        "--clone-dir",
        default=str(Path(".cache") / "classify_clones"),
        help="Directory to store git clones",
    )
    parser.add_argument("--keep-clone", action="store_true", help="Keep cloned repos on disk")
    parser.add_argument(
        "--checkpoint",
        default="repo_classifications.checkpoint.json",
        help="Checkpoint file path",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument(
        "--checkpoint-interval",
        type=int,
        default=20,
        help="Save checkpoint every N completed repos",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="LLM model")
    parser.add_argument("--max-prompt-tokens", type=int, default=DEFAULT_MAX_PROMPT_TOKENS)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM call, only write prompts")
    parser.add_argument("--workers", type=int, default=4, help="Number of worker threads")
    return parser.parse_args()


def load_checkpoint(path: str) -> set[int]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return set(int(i) for i in data.get("processed_indices", []))
    except Exception as exc:
        logger.warning("Failed to load checkpoint %s: %s", path, exc)
        return set()


def save_checkpoint(path: str, processed_indices: set[int]) -> None:
    try:
        payload = {
            "processed_indices": sorted(processed_indices),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(path, "w") as f:
            json.dump(payload, f)
    except Exception as exc:
        logger.warning("Failed to save checkpoint %s: %s", path, exc)


def process_row(
    idx: int,
    row: dict,
    args: argparse.Namespace,
) -> dict:
    repo_id = (row.get("REPO_ID") or row.get("repo_id") or row.get("repo_name") or "").strip()
    logger.info("[%s] Stage: start", repo_id or f"index:{idx}")
    if not repo_id or "/" not in repo_id:
        return {
            "index": idx,
            "repo_id": repo_id,
            "error": "missing_repo_id",
        }

    file_contents = []
    repo_path: Optional[Path] = None
    use_clone = not args.no_clone
    if args.use_clone:
        use_clone = True

    if use_clone:
        logger.info("[%s] Stage: clone_repo", repo_id)
        repo_path = clone_repo(repo_id, Path(args.clone_dir))

    if use_clone and repo_path:
        files_to_read = discover_important_files(repo_path, args.max_files)
        logger.info("[%s] Stage: read_files_local (%d)", repo_id, len(files_to_read))
        for path in files_to_read:
            content = read_local_file(path, args.max_file_bytes)
            if content:
                rel = str(path.relative_to(repo_path))
                file_contents.append((rel, content))
        if not args.keep_clone:
            shutil.rmtree(repo_path, ignore_errors=True)
    else:
        logger.warning("[%s] Clone unavailable; no local files collected", repo_id)
    logger.info("[%s] Stage: fetch_files_done (%d)", repo_id, len(file_contents))

    logger.info("[%s] Stage: build_prompt", repo_id)
    prompt, prompt_tokens = build_prompt(
        dataset_row=row,
        file_contents=file_contents,
        max_prompt_tokens=args.max_prompt_tokens,
    )
    logger.debug("Prompt tokens (est): %d for %s", prompt_tokens, repo_id)

    llm_response = None
    llm_error = None
    if not args.no_llm:
        if AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT:
            try:
                logger.info("[%s] Stage: llm_call", repo_id)
                llm_response = call_azure_openai_chat(args.model, prompt, args.max_output_tokens)
            except Exception as e:
                llm_error = str(e)
                logger.warning("LLM call failed for %s: %s", repo_id, llm_error)
        else:
            llm_error = "Missing Azure OpenAI credentials"
    logger.info("[%s] Stage: done", repo_id)

    result = {
        "index": idx,
        "repo_id": repo_id,
        "prompt_tokens_est": prompt_tokens,
        "prompt": prompt if args.no_llm else None,
        "selected_files": [path for path, _ in file_contents],
        "llm_response": llm_response,
        "llm_error": llm_error,
    }

    return result


def main() -> None:
    args = parse_args()
    dataset = load_dataset(args.dataset, split=args.split)

    end = len(dataset)
    if args.limit and args.limit > 0:
        end = min(end, args.start + args.limit)

    out_path = args.output

    total = max(0, end - args.start)
    processed_indices: set[int] = set()
    if args.resume:
        processed_indices = load_checkpoint(args.checkpoint)
        if processed_indices:
            logger.info("Resuming with %d processed indices from %s", len(processed_indices), args.checkpoint)
    logger.info(
        "Starting classification: dataset=%s split=%s range=[%d,%d) total=%d",
        args.dataset,
        args.split,
        args.start,
        end,
        total,
    )

    lock = Lock()
    mode = "a" if args.resume else "w"
    completed_since_checkpoint = 0
    with open(out_path, mode) as out:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = []
            for idx in range(args.start, end):
                if idx in processed_indices:
                    continue
                row = dataset[idx]
                futures.append(executor.submit(process_row, idx, row, args))

            for future in tqdm(as_completed(futures), total=len(futures), desc="Classifying", unit="repo"):
                try:
                    result = future.result()
                except Exception as exc:
                    logger.warning("Worker failed: %s", exc)
                    continue
                categories = extract_categories(result.get("llm_response"))
                repo_id = result.get("repo_id") or ""
                if categories:
                    logger.info("%s => %s", repo_id, ", ".join(categories))
                with lock:
                    out.write(json.dumps(result) + "\n")
                    processed_indices.add(result.get("index"))
                    completed_since_checkpoint += 1
                    if args.checkpoint_interval > 0 and completed_since_checkpoint >= args.checkpoint_interval:
                        save_checkpoint(args.checkpoint, processed_indices)
                        completed_since_checkpoint = 0

    if args.checkpoint_interval > 0:
        save_checkpoint(args.checkpoint, processed_indices)

    logger.info("Done. Wrote results to %s", out_path)


if __name__ == "__main__":
    main()