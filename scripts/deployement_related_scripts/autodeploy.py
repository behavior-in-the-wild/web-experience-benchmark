
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from openai import OpenAI
from tqdm import tqdm

import readme_guided_autodeploy_azure as base


CHECKPOINT_LOCK = threading.Lock()

STATIC_CANDIDATE_DIRS = [
    ".",
    "docs",
    "public",
    "dist",
    "build",
    "out",
    "_site",
    "site",
    ".vitepress/dist",
    ".vuepress/dist",
    "storybook-static",
    "website",
    "web",
    "app",
]

CLEAN_DIR_NAMES = {
    "node_modules",
    ".autodeploy_home",
    ".cache",
    ".gatsby",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".turbo",
    ".svelte-kit",
}


def patch_base_runtime_env():
    """
    Monkey-patch base.clean_env so the existing runner gets:
    - NODE_OPTIONS=--openssl-legacy-provider for older Gatsby/Webpack
    - npm legacy-peer-deps behavior for peer-dependency conflicts
    - current PATH, including nvm Node 22 if caller ran `nvm use 22`
    """
    original_clean_env = base.clean_env

    def clean_env_v3(repo_dir: Path, log_dir: Path, port: int) -> Dict[str, str]:
        env = original_clean_env(repo_dir, log_dir, port)

        # Preserve current PATH so nvm-selected Node 22 is used.
        if os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]

        # Recover many old Gatsby/Webpack failures.
        env["NODE_OPTIONS"] = os.environ.get("NODE_OPTIONS", "--openssl-legacy-provider")

        # Recover many npm peer-dependency failures.
        env["NPM_CONFIG_LEGACY_PEER_DEPS"] = "true"
        env["npm_config_legacy_peer_deps"] = "true"
        env["NPM_CONFIG_AUDIT"] = "false"
        env["NPM_CONFIG_FUND"] = "false"

        # Make Yarn Berry less strict if present.
        env["YARN_ENABLE_IMMUTABLE_INSTALLS"] = "false"

        # Avoid update prompts.
        env["NO_UPDATE_NOTIFIER"] = "1"

        return env

    base.clean_env = clean_env_v3


def repo_safe_name(repo_url: str) -> str:
    s = repo_url.replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
    parts = s.split("/")
    if len(parts) >= 2:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{parts[0]}__{parts[1]}")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def append_jsonl(path: Path, obj: Dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def load_processed(path: Path) -> set:
    processed = set()
    if not path.exists():
        return processed
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
            if r.get("repo_id"):
                processed.add(r["repo_id"])
        except Exception:
            pass
    return processed


def load_latest_records(path: Path) -> List[Dict]:
    latest = {}
    if not path.exists():
        return []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
            if r.get("repo_id"):
                latest[r["repo_id"]] = r
        except Exception:
            pass
    return list(latest.values())


def static_candidate_dirs(repo_dir: Path) -> List[str]:
    found = []
    for d in STATIC_CANDIDATE_DIRS:
        if (repo_dir / d / "index.html").exists():
            found.append(d)

    # Add shallow nested dirs that contain index.html, but avoid huge folders.
    for p in repo_dir.glob("*"):
        if not p.is_dir():
            continue
        if p.name.startswith(".") and p.name not in {".vitepress", ".vuepress"}:
            continue
        if p.name in CLEAN_DIR_NAMES:
            continue
        if (p / "index.html").exists():
            rel = str(p.relative_to(repo_dir))
            if rel not in found:
                found.append(rel)

    return found


def static_plan(serve_dir: str) -> Dict:
    return {
        "framework": "static-html",
        "confidence": "high",
        "package_manager": "none",
        "working_directory": ".",
        "install_commands": [],
        "build_commands": [],
        "serve_commands": [f'python3 -m http.server "$PORT" -d {shlex.quote(serve_dir)}'],
        "output_dir": serve_dir,
        "evidence": [f"Static rescue: found {serve_dir}/index.html"],
        "warnings": ["Serving prebuilt static files directly."],
        "should_try": True,
    }


def read_tail(path: Path, max_chars: int = 24000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except Exception:
        return ""


def attempt_plan(
    label: str,
    plan: Dict,
    repo_dir: Path,
    safe: str,
    output_base: Path,
    logs_base: Path,
    port: int,
    timeout: int,
) -> Dict:
    attempt_dir = output_base / safe / label
    log_dir = logs_base / safe / label
    script_path = attempt_dir / "host.sh"

    result = {
        "label": label,
        "status": "failed",
        "plan": plan,
        "script_path": str(script_path),
        "failure_reason": None,
        "validation": {},
    }

    ok, why = base.validate_plan(plan)
    if not ok:
        result["failure_reason"] = f"PLAN_INVALID: {why}"
        return result

    try:
        base.generate_host(plan, script_path)
        ok, why, meta = base.run_and_validate(
            script_path,
            repo_dir,
            log_dir,
            port,
            timeout,
        )
        result["validation"] = meta
        if ok:
            result["status"] = "success"
            result["failure_reason"] = None
        else:
            result["failure_reason"] = why
        return result
    except Exception as e:
        result["failure_reason"] = f"UNKNOWN_ERROR: {e}"
        return result


def get_azure_client() -> Optional[OpenAI]:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    if not endpoint or not api_key:
        return None
    endpoint = endpoint.rstrip("/")
    base_url = endpoint + "/openai/v1/" if "/openai/v1" not in endpoint else endpoint.rstrip("/") + "/"
    return OpenAI(api_key=api_key, base_url=base_url, timeout=240)


def parse_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    a = text.find("{")
    b = text.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            return None
    return None


def repair_plan_with_azure(
    repo_id: str,
    detection: Dict,
    readme_evidence: str,
    configs: str,
    previous_attempts: List[Dict],
    repo_dir: Path,
    logs_base: Path,
    safe: str,
) -> Optional[Dict]:
    client = get_azure_client()
    deployment = os.getenv("AZURE_OPENAI_API_DEPLOYMENT_NAME")
    if not client or not deployment:
        return None

    last_logs = []
    for attempt in previous_attempts[-2:]:
        label = attempt.get("label")
        if not label:
            continue
        run_log = logs_base / safe / label / "run_host.log"
        runtime_log = logs_base / safe / label / "host_runtime.log"
        last_logs.append(f"\n===== ATTEMPT {label} RUN LOG =====\n{read_tail(run_log)}")
        last_logs.append(f"\n===== ATTEMPT {label} RUNTIME LOG =====\n{read_tail(runtime_log)}")

    static_dirs = static_candidate_dirs(repo_dir)

    prompt = f"""
Return JSON only. Do not return markdown.

You are repairing a failed LOCAL website deployment plan.

Goal:
Serve the website locally at http://127.0.0.1:$PORT.

Evidence priority:
1. README local-development/build instructions are primary.
2. package.json scripts and lockfiles are second.
3. config files are third.
4. previous failure logs are critical.

Rules:
- Return a corrected JSON deployment plan only.
- Do not use production deployment commands.
- Do not use firebase deploy, vercel --prod, netlify deploy --prod, git push, npm publish, ssh, scp, aws, gcloud, az, sudo, rm -rf.
- If static candidate dirs contain index.html, prefer direct static serving.
- Static candidate dirs found: {static_dirs}
- If npm dependency conflict appears, use npm install --legacy-peer-deps.
- If Gatsby/Webpack has ERR_OSSL_EVP_UNSUPPORTED, assume NODE_OPTIONS=--openssl-legacy-provider is available and retry normal build.
- If VitePress build succeeds, do not use preview with bad --host path; serve .vitepress/dist statically.
- If Eleventy is used, build with npx @11ty/eleventy and serve _site statically. Do not use unsupported --host flags.
- If Jekyll/Bundler requires a specific Bundler version, you may install it locally using gem install bundler:<version> and then bundle _<version>_ install.
- If a required private dependency or missing environment variable is unavoidable, set should_try=false.
- Final serve command must keep server running.

Required JSON keys:
framework, confidence, package_manager, working_directory, install_commands, build_commands, serve_commands, output_dir, evidence, warnings, should_try

Repository:
{repo_id}

Detection:
{json.dumps(detection, indent=2)[:12000]}

README evidence:
{readme_evidence[:12000]}

Config/package evidence:
{configs[:12000]}

Previous attempts:
{json.dumps(previous_attempts, indent=2)[:12000]}

Failure logs:
{''.join(last_logs)[-24000:]}
""".strip()

    try:
        resp = client.responses.create(
            model=deployment,
            input=prompt,
            max_output_tokens=1600,
        )
        return parse_json(getattr(resp, "output_text", "") or str(resp))
    except Exception as e:
        print(f"[WARN] Azure repair failed for {repo_id}: {e}")
        return None


def cleanup_heavy_dirs(repo_dir: Path):
    for name in CLEAN_DIR_NAMES:
        for p in repo_dir.rglob(name):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)


def write_reports(checkpoint: Path, report_dir: Path):
    report_dir.mkdir(parents=True, exist_ok=True)
    records = load_latest_records(checkpoint)

    success = [r for r in records if r.get("status") == "success"]
    failed = [r for r in records if r.get("status") == "failed"]

    (report_dir / "deployment_success.txt").write_text(
        "\n".join(r.get("repo_url", "") for r in success if r.get("repo_url")) + ("\n" if success else ""),
        encoding="utf-8",
    )

    (report_dir / "deployment_failed.txt").write_text(
        "\n".join(f'{r.get("repo_url", r.get("repo_id"))}\t{r.get("failure_reason")}' for r in failed) + ("\n" if failed else ""),
        encoding="utf-8",
    )

    fields = [
        "repo_id", "repo_url", "status", "failure_reason", "best_attempt",
        "detected_framework", "confidence", "port", "script_path"
    ]
    with open(report_dir / "deployment_summary.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k) for k in fields})

    reasons = {}
    for r in failed:
        code = str(r.get("failure_reason") or "UNKNOWN").split(":", 1)[0]
        reasons[code] = reasons.get(code, 0) + 1

    (report_dir / "failure_counts.txt").write_text(
        "\n".join(f"{v}\t{k}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])) + ("\n" if reasons else ""),
        encoding="utf-8",
    )

    return len(records), len(success), len(failed)


def process_one(idx: int, raw_url: str, args, processed: set) -> Dict:
    norm = base.normalize_repo(raw_url)
    if not norm:
        return {"repo_id": raw_url, "status": "failed", "failure_reason": "INVALID_REPO_LINE"}

    repo_id, repo_url, safe = norm

    if repo_id in processed and not args.force:
        return {"repo_id": repo_id, "repo_url": repo_url, "status": "skipped", "failure_reason": "already checkpointed"}

    port = args.port + idx
    clone_dir = Path(args.clone_dir) / safe
    logs_base = Path(args.logs)
    output_base = Path(args.output)
    checkpoint = Path(args.checkpoint)

    record = {
        "repo_id": repo_id,
        "repo_url": repo_url,
        "safe_name": safe,
        "status": "failed",
        "failure_reason": None,
        "attempts": [],
        "port": port,
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    try:
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        logs_base.mkdir(parents=True, exist_ok=True)
        output_base.mkdir(parents=True, exist_ok=True)

        if args.reclone and clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)

        if not clone_dir.exists():
            ok, why = base.run_cmd(
                ["git", "clone", "--depth=1", repo_url, str(clone_dir)],
                None,
                logs_base / safe / "clone.log",
                args.timeout_clone,
            )
            if not ok:
                record["failure_reason"] = f"CLONE_FAILED: {why}"
                append_jsonl(checkpoint, record)
                return record

        detection = base.detect(clone_dir)
        readme_path = base.find_readme(clone_dir)
        readme = base.read_text(readme_path, 60000) if readme_path else ""
        readme_evidence = base.extract_readme_evidence(readme)
        configs = base.collect_configs(clone_dir)

        record["detected_framework"] = detection.get("framework")
        record["confidence"] = detection.get("confidence")

        # Attempt 1: static-first rescue.
        if args.static_first:
            for d in static_candidate_dirs(clone_dir):
                result = attempt_plan(
                    label=f"attempt_static_{re.sub(r'[^A-Za-z0-9_.-]+', '_', d)}",
                    plan=static_plan(d),
                    repo_dir=clone_dir,
                    safe=safe,
                    output_base=output_base,
                    logs_base=logs_base,
                    port=port,
                    timeout=args.timeout_static,
                )
                record["attempts"].append(result)
                if result["status"] == "success":
                    record["status"] = "success"
                    record["failure_reason"] = None
                    record["best_attempt"] = result["label"]
                    record["script_path"] = result["script_path"]
                    append_jsonl(checkpoint, record)
                    if args.cleanup_deps:
                        cleanup_heavy_dirs(clone_dir)
                    return record

        # Attempt 2: README-guided Azure plan.
        if args.use_azure_openai:
            llm_plan = base.azure_plan(repo_id, detection, readme_evidence, configs)
        else:
            llm_plan = None

        if not llm_plan:
            llm_plan = base.deterministic_plan(detection)

        result = attempt_plan(
            label="attempt_llm_initial",
            plan=llm_plan,
            repo_dir=clone_dir,
            safe=safe,
            output_base=output_base,
            logs_base=logs_base,
            port=port,
            timeout=args.timeout_serve,
        )
        record["attempts"].append(result)

        if result["status"] == "success":
            record["status"] = "success"
            record["failure_reason"] = None
            record["best_attempt"] = result["label"]
            record["script_path"] = result["script_path"]
            append_jsonl(checkpoint, record)
            if args.cleanup_deps:
                cleanup_heavy_dirs(clone_dir)
            return record

        # Attempt 3: post-build static rescue. Useful for VitePress/Gatsby/etc. where build created output.
        for d in static_candidate_dirs(clone_dir):
            already_tried = any(a.get("label") == f"attempt_static_post_{re.sub(r'[^A-Za-z0-9_.-]+', '_', d)}" for a in record["attempts"])
            if already_tried:
                continue
            result = attempt_plan(
                label=f"attempt_static_post_{re.sub(r'[^A-Za-z0-9_.-]+', '_', d)}",
                plan=static_plan(d),
                repo_dir=clone_dir,
                safe=safe,
                output_base=output_base,
                logs_base=logs_base,
                port=port,
                timeout=args.timeout_static,
            )
            record["attempts"].append(result)
            if result["status"] == "success":
                record["status"] = "success"
                record["failure_reason"] = None
                record["best_attempt"] = result["label"]
                record["script_path"] = result["script_path"]
                append_jsonl(checkpoint, record)
                if args.cleanup_deps:
                    cleanup_heavy_dirs(clone_dir)
                return record

        # Attempt 4: repair prompt using failure logs.
        if args.repair_attempts > 0 and args.use_azure_openai:
            for repair_i in range(args.repair_attempts):
                repaired = repair_plan_with_azure(
                    repo_id=repo_id,
                    detection=detection,
                    readme_evidence=readme_evidence,
                    configs=configs,
                    previous_attempts=record["attempts"],
                    repo_dir=clone_dir,
                    logs_base=logs_base,
                    safe=safe,
                )

                if not repaired:
                    continue

                result = attempt_plan(
                    label=f"attempt_repair_{repair_i+1}",
                    plan=repaired,
                    repo_dir=clone_dir,
                    safe=safe,
                    output_base=output_base,
                    logs_base=logs_base,
                    port=port,
                    timeout=args.timeout_serve,
                )
                record["attempts"].append(result)

                if result["status"] == "success":
                    record["status"] = "success"
                    record["failure_reason"] = None
                    record["best_attempt"] = result["label"]
                    record["script_path"] = result["script_path"]
                    append_jsonl(checkpoint, record)
                    if args.cleanup_deps:
                        cleanup_heavy_dirs(clone_dir)
                    return record

                # Static rescue after repaired build.
                for d in static_candidate_dirs(clone_dir):
                    result2 = attempt_plan(
                        label=f"attempt_repair_{repair_i+1}_static_{re.sub(r'[^A-Za-z0-9_.-]+', '_', d)}",
                        plan=static_plan(d),
                        repo_dir=clone_dir,
                        safe=safe,
                        output_base=output_base,
                        logs_base=logs_base,
                        port=port,
                        timeout=args.timeout_static,
                    )
                    record["attempts"].append(result2)
                    if result2["status"] == "success":
                        record["status"] = "success"
                        record["failure_reason"] = None
                        record["best_attempt"] = result2["label"]
                        record["script_path"] = result2["script_path"]
                        append_jsonl(checkpoint, record)
                        if args.cleanup_deps:
                            cleanup_heavy_dirs(clone_dir)
                        return record

        last = record["attempts"][-1] if record["attempts"] else {}
        record["status"] = "failed"
        record["failure_reason"] = last.get("failure_reason") or "ALL_ATTEMPTS_FAILED"
        record["best_attempt"] = None
        record["script_path"] = last.get("script_path")
        append_jsonl(checkpoint, record)

        if args.cleanup_deps:
            cleanup_heavy_dirs(clone_dir)

        return record

    except Exception as e:
        record["status"] = "failed"
        record["failure_reason"] = f"UNKNOWN_ERROR: {e}"
        append_jsonl(Path(args.checkpoint), record)
        return record


def main():
    patch_base_runtime_env()

    ap = argparse.ArgumentParser(description="Unified README-guided auto-deployment agent V3.")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", default="generated_scripts_agent_v3")
    ap.add_argument("--clone-dir", default="cloned_repos_agent_v3")
    ap.add_argument("--logs", default="logs_agent_v3")
    ap.add_argument("--report-dir", default="reports_agent_v3")
    ap.add_argument("--checkpoint", default="checkpoint_agent_v3.jsonl")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--port", type=int, default=15000)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--timeout-clone", type=int, default=300)
    ap.add_argument("--timeout-serve", type=int, default=420)
    ap.add_argument("--timeout-static", type=int, default=60)
    ap.add_argument("--repair-attempts", type=int, default=1)
    ap.add_argument("--use-azure-openai", action="store_true")
    ap.add_argument("--static-first", action="store_true", default=True)
    ap.add_argument("--cleanup-deps", action="store_true", default=True)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reclone", action="store_true")
    args = ap.parse_args()

    if args.use_azure_openai:
        missing = [k for k in ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_DEPLOYMENT_NAME"] if not os.getenv(k)]
        if missing:
            raise SystemExit(f"Missing Azure env vars: {missing}")

    inp = Path(args.input)
    lines = [x.strip() for x in inp.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip() and not x.strip().startswith("#")]
    if args.limit:
        lines = lines[:args.limit]

    processed = load_processed(Path(args.checkpoint))

    print(f"[INFO] repos={len(lines)} already_checkpointed={len(processed)} workers={args.workers}")
    print(f"[INFO] static_first={args.static_first} repair_attempts={args.repair_attempts} cleanup_deps={args.cleanup_deps}")
    print(f"[INFO] NODE: {subprocess.getoutput('node -v 2>/dev/null || true')}")
    print(f"[INFO] NODE_OPTIONS={os.environ.get('NODE_OPTIONS', '--openssl-legacy-provider')}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(process_one, i, line, args, processed) for i, line in enumerate(lines)]
        for fut in tqdm(as_completed(futs), total=len(futs), desc="repos"):
            results.append(fut.result())

    total, success, failed = write_reports(Path(args.checkpoint), Path(args.report_dir))
    print("[DONE]")
    print(f"checkpoint_total={total} success={success} failed={failed}")
    print(f"reports={args.report_dir}")


if __name__ == "__main__":
    main()
