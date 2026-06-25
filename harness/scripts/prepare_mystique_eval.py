#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MODEL_JSONL_PATTERNS = {
    "sonnet": (
        "*sonnet*merged*.jsonl",
        "*sonnet*force_full*.jsonl",
        "*sonnet*patch_only*.jsonl",
    ),
    "opus": (
        "*opus*merged*.jsonl",
        "*opus*patch_only*.jsonl",
    ),
}

MODEL_LABELS = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


@dataclass
class PreparedPatch:
    model: str
    site_id: str
    url: str
    domain: str
    suggestion_index_raw: int
    suggestion_index_zero: int
    job_id: str
    job_label: str
    workspace: Path
    branch: str
    base_branch: str
    patch_path: Path
    mirror_dir: str
    files_changed: list[str]


def run_git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.stdout


def run_git_bytes(args: list[str], cwd: Path) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout


def domain_slug(url_or_domain: str) -> str:
    parsed = urlparse(url_or_domain)
    host = parsed.netloc or parsed.path
    host = host.strip().rstrip("/")
    return host[4:] if host.startswith("www.") else host


def page_slug(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/") or "home"
    return path.replace("/", "__")


def load_manifest_jsonl(dump_root: Path, model: str) -> Path:
    manifest = dump_root / "MANIFEST.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        rel = (((data.get("runs") or {}).get(model) or {}).get("jsonl") or "").strip()
        if rel:
            path = dump_root / rel
            if path.is_file():
                return path

    outputs = dump_root / "cwv_pipeline" / "outputs"
    for pattern in MODEL_JSONL_PATTERNS[model]:
        matches = sorted(outputs.glob(pattern))
        if matches:
            return matches[-1]
    raise FileNotFoundError(f"no JSONL found for model={model!r} under {dump_root}")


def localize_workspace(dump_root: Path, workspace_dir: str) -> Path:
    raw = Path(workspace_dir)
    if raw.exists():
        return raw

    marker = "app/agents/crews/coding_agent/dumps/"
    text = workspace_dir.replace("\\", "/")
    if marker in text:
        suffix = text.split(marker, 1)[1]
        candidate = dump_root / marker / suffix
        if candidate.exists():
            return candidate

    candidate = dump_root / workspace_dir
    if candidate.exists():
        return candidate

    raise FileNotFoundError(f"workspace not found: {workspace_dir}")


def git_branches(workspace: Path) -> list[str]:
    out = run_git(["branch", "--format=%(refname:short)"], workspace)
    return [line.strip() for line in out.splitlines() if line.strip()]


def choose_branch(entry: dict[str, Any], workspace: Path) -> str:
    fixes = (((entry.get("result") or {}).get("report_data") or {}).get("accepted_fixes") or [])
    for fix in fixes:
        branch = (fix.get("branch_name") or "").strip()
        if branch:
            return branch

    branches = [b for b in git_branches(workspace) if b not in {"main", "master"}]
    if len(branches) == 1:
        return branches[0]
    if not branches:
        raise RuntimeError(f"no suggestion branch found in {workspace}")
    raise RuntimeError(f"ambiguous suggestion branches in {workspace}: {branches}")


def choose_base_branch(entry: dict[str, Any], workspace: Path) -> str:
    raw = ((entry.get("result") or {}).get("base_branch") or "").strip()
    branches = set(git_branches(workspace))
    if raw in branches:
        return raw
    if "main" in branches:
        return "main"
    if "master" in branches:
        return "master"
    raise RuntimeError(f"no main/master base branch in {workspace}")


def extract_patch(workspace: Path, base_branch: str, branch: str) -> str:
    # Do not use subprocess text mode here: universal newline decoding would
    # strip CRLF from hunks and can make otherwise valid patches fail to apply.
    return run_git_bytes(["diff", "--binary", f"{base_branch}..{branch}", "--"], workspace).decode(
        "utf-8", errors="surrogateescape"
    )


def changed_files_from_patch(patch: str) -> list[str]:
    files: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) >= 4 and parts[2].startswith("a/"):
            files.append(parts[2][2:])
    return files


def suggestion_payload(row: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    result = entry.get("result") or {}
    suggestions_data = result.get("suggestions_data") or {}
    suggestions = suggestions_data.get("suggestions") or []
    if suggestions:
        payload = dict(suggestions[0])
    else:
        payload = {
            "title": entry.get("title"),
            "metric": entry.get("metric"),
        }
    payload.setdefault("url", row.get("url"))
    payload.setdefault("domain", row.get("domain"))
    payload.setdefault("suggestion_index", entry.get("suggestion_index"))
    return payload


def write_index_from_raw_dom(mirror: Path) -> None:
    raw_dom = mirror / "assets" / "raw_dom.html"
    if raw_dom.is_file():
        shutil.copy2(raw_dom, mirror / "index.html")


def add_aem_root_links(mirror: Path) -> None:
    assets = mirror / "assets"
    if not assets.is_dir():
        return
    for name in ["scripts", "styles", "blocks", "dist", "commons", "lib"]:
        target = assets / name
        link = mirror / name
        if not target.exists() or link.exists():
            continue
        rel_target = os.path.relpath(target, start=mirror)
        try:
            link.symlink_to(rel_target, target_is_directory=target.is_dir())
        except OSError:
            if target.is_dir():
                shutil.copytree(target, link)
            else:
                shutil.copy2(target, link)


def stage_baseline_workspace(workspace: Path, base_branch: str, mirror: Path) -> None:
    if mirror.exists():
        shutil.rmtree(mirror)
    mirror.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(
        ["git", "-C", str(workspace), "archive", base_branch],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    extract = subprocess.Popen(
        ["tar", "-x", "-C", str(mirror)],
        stdin=archive.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    archive.stdout.close()
    _, extract_err = extract.communicate()
    _, archive_err = archive.communicate()
    if archive.returncode != 0:
        raise RuntimeError(archive_err.decode("utf-8", errors="replace"))
    if extract.returncode != 0:
        raise RuntimeError(extract_err.decode("utf-8", errors="replace"))
    write_index_from_raw_dom(mirror)
    add_aem_root_links(mirror)


def verify_patch_applies(mirror: Path, patch_path: Path) -> tuple[bool, str]:
    subprocess.run(["git", "-C", str(mirror), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(mirror), "add", "-A"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "-C", str(mirror), "commit", "-qm", "baseline"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc = subprocess.run(
        ["git", "-C", str(mirror), "apply", "--check", "--whitespace=nowarn", str(patch_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            ["git", "-C", str(mirror), "apply", "--check", "--3way", "--whitespace=nowarn", str(patch_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    shutil.rmtree(mirror / ".git", ignore_errors=True)
    msg = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, msg


def prepare_model(
    dump_root: Path,
    output_root: Path,
    model: str,
    template_name: str,
    include_empty: bool,
    limit_patches: int | None,
    verify: bool,
) -> dict[str, Any]:
    jsonl = load_manifest_jsonl(dump_root, model)
    model_label = MODEL_LABELS.get(model, model)
    model_root = output_root / model_label
    results_root = model_root / "results"
    mirrors_root = model_root / "mirrors"
    eval_jsonl = model_root / "eval_input.jsonl"
    manifest_path = model_root / "manifest.json"

    if model_root.exists():
        shutil.rmtree(model_root)
    results_root.mkdir(parents=True)
    mirrors_root.mkdir(parents=True)

    summary: dict[str, Any] = {
        "model": model,
        "model_label": model_label,
        "source_jsonl": str(jsonl),
        "template_name": template_name,
        "rows": 0,
        "codefix_entries": 0,
        "prepared_patches": 0,
        "empty_diffs": 0,
        "errors": [],
        "patches": [],
        "eval_jsonl": str(eval_jsonl),
        "results_root": str(results_root),
        "mirrors_root": str(mirrors_root),
    }

    eval_rows: list[dict[str, Any]] = []
    prepared_count = 0

    with jsonl.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            summary["rows"] += 1
            for entry in row.get("codefix") or []:
                summary["codefix_entries"] += 1
                try:
                    raw_idx = int(entry.get("suggestion_index"))
                    zero_idx = raw_idx - 1 if raw_idx > 0 else raw_idx
                    site_id = str(row.get("site_id") or row.get("id") or row.get("row_id") or "")
                    if not site_id:
                        raise RuntimeError(f"missing site_id on line {line_no}")
                    url = str(row.get("url") or "")
                    domain = str(row.get("domain") or domain_slug(url))
                    result = entry.get("result") or {}
                    workspace = localize_workspace(dump_root, str(result.get("workspace_dir") or ""))
                    branch = choose_branch(entry, workspace)
                    base_branch = choose_base_branch(entry, workspace)
                    patch = extract_patch(workspace, base_branch, branch)
                    if not patch.strip():
                        summary["empty_diffs"] += 1
                        if not include_empty:
                            continue

                    job_id = f"{site_id}_s{zero_idx}"
                    job_label = f"{job_id}_{template_name}"
                    job_dir = results_root / job_label
                    job_dir.mkdir(parents=True, exist_ok=True)
                    patch_path = job_dir / f"{job_label}.patch"
                    patch_path.write_text(patch, encoding="utf-8")

                    payload = suggestion_payload(row, entry)
                    (job_dir / "input_suggestion.json").write_text(
                        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )

                    mirror_dir = f"{job_id}/{domain_slug(domain)}/{page_slug(url)}"
                    mirror = mirrors_root / mirror_dir
                    stage_baseline_workspace(workspace, base_branch, mirror)

                    files_changed = changed_files_from_patch(patch)
                    verify_ok = None
                    verify_msg = ""
                    if verify and patch.strip():
                        verify_ok, verify_msg = verify_patch_applies(mirror, patch_path)

                    meta = {
                        "model": model,
                        "model_label": model_label,
                        "site_id": site_id,
                        "url": url,
                        "domain": domain,
                        "suggestion_index_raw": raw_idx,
                        "suggestion_index_zero": zero_idx,
                        "job_id": job_id,
                        "job_label": job_label,
                        "workspace": str(workspace),
                        "base_branch": base_branch,
                        "branch": branch,
                        "mirror_dir": mirror_dir,
                        "files_changed": files_changed,
                        "empty_diff": not patch.strip(),
                        "verify_patch_applies": verify_ok,
                        "verify_message": verify_msg,
                    }
                    (job_dir / "mystique_meta.json").write_text(
                        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )

                    eval_rows.append(
                        {
                            "id": job_id,
                            "row_id": job_id,
                            "site_id": site_id,
                            "page_url": url,
                            "url": url,
                            "domain": domain,
                            "mirror_dir": mirror_dir,
                            "mystique": meta,
                        }
                    )
                    summary["patches"].append(meta)
                    prepared_count += 1
                    summary["prepared_patches"] = prepared_count
                    if limit_patches is not None and prepared_count >= limit_patches:
                        break
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(
                        {
                            "line": line_no,
                            "site_id": row.get("site_id"),
                            "domain": row.get("domain"),
                            "suggestion_index": entry.get("suggestion_index"),
                            "error": str(exc),
                        }
                    )
            if limit_patches is not None and prepared_count >= limit_patches:
                break

    with eval_jsonl.open("w", encoding="utf-8") as handle:
        for row in eval_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def parse_models(raw: str) -> list[str]:
    models = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [m for m in models if m not in MODEL_JSONL_PATTERNS]
    if unknown:
        raise ValueError(f"unknown models: {unknown}; expected one of {sorted(MODEL_JSONL_PATTERNS)}")
    return models


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize Mystique patch-only dumps into evaluate.sh --skip-agent inputs."
    )
    parser.add_argument(
        "--dump-root",
        default="final_result_dumps/mystique_run/claude_opus_sonnet_dumps",
        help="Mystique dump root containing MANIFEST.json and app/agents/... workspaces.",
    )
    parser.add_argument(
        "--output-root",
        default="final_result_dumps/mystique_run/eval_ready",
        help="Output directory for normalized patch results, staged mirrors, manifests, and eval JSONL.",
    )
    parser.add_argument("--models", default="sonnet,opus", help="Comma-separated models: sonnet,opus.")
    parser.add_argument(
        "--template-name",
        default="template_claudecode",
        help="Agent template basename used in evaluate.sh job labels, without .sh.",
    )
    parser.add_argument("--include-empty", action="store_true", help="Write empty patch jobs too.")
    parser.add_argument("--limit-patches", type=int, default=None, help="Prepare at most N patches per model.")
    parser.add_argument("--verify-patches", action="store_true", help="Run git apply --check on staged mirrors.")
    args = parser.parse_args()

    dump_root = Path(args.dump_root).resolve()
    output_root = Path(args.output_root).resolve()
    models = parse_models(args.models)

    output_root.mkdir(parents=True, exist_ok=True)
    summaries = []
    for model in models:
        summary = prepare_model(
            dump_root=dump_root,
            output_root=output_root,
            model=model,
            template_name=args.template_name,
            include_empty=args.include_empty,
            limit_patches=args.limit_patches,
            verify=args.verify_patches,
        )
        summaries.append(summary)
        print(
            f"{summary['model_label']}: rows={summary['rows']} "
            f"codefix={summary['codefix_entries']} prepared={summary['prepared_patches']} "
            f"empty={summary['empty_diffs']} errors={len(summary['errors'])}"
        )

    combined = {
        "dump_root": str(dump_root),
        "output_root": str(output_root),
        "models": summaries,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(combined, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
