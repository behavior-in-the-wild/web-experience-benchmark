#!/usr/bin/env python3
import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


def repo_safe_name(repo_url: str) -> str:
    s = (repo_url or "").replace("https://github.com/", "").replace("http://github.com/", "").strip("/")
    parts = s.split("/")
    if len(parts) >= 2:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{parts[0]}__{parts[1]}")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def read_text(path: Path, max_chars: int = 120000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except Exception:
        return ""


def classify_failure(record, log_text: str):
    reason = str(record.get("failure_reason") or "")
    text = (reason + "\n" + log_text).lower()

    if "plan_invalid" in text or "should_try=false" in text:
        return "PLAN_INVALID", "Model/planner refused or could not infer a safe deployment plan"

    if "yarn: command not found" in text:
        return "MISSING_YARN", "Missing yarn package manager"

    if "pnpm: command not found" in text:
        return "MISSING_PNPM", "Missing pnpm package manager"

    if "bun: command not found" in text:
        return "MISSING_BUN", "Missing bun runtime/package manager"

    if "command not found" in text:
        return "COMMAND_NOT_FOUND", "A required CLI command was missing"

    if "bundler::permissionerror" in text or "/var/lib/gems" in text or "trying to write to `/var/lib/gems" in text:
        return "BUNDLER_PERMISSION", "Ruby/Bundler tried writing to system gem directory"

    if "we don’t know what '--host" in text or "we don't know what '--host" in text:
        return "ELEVENTY_BAD_FLAG", "Eleventy command used unsupported --host flag"

    if "eresolve" in text or "unable to resolve dependency tree" in text or "peer dep" in text:
        return "NPM_DEPENDENCY_CONFLICT", "npm dependency/peer dependency conflict"

    if "repository not found" in text and "github.com" in text:
        return "PRIVATE_OR_MISSING_GIT_DEP", "Private/missing git dependency blocked install"

    if "authentication failed" in text and "github.com" in text:
        return "PRIVATE_OR_MISSING_GIT_DEP", "Git dependency authentication failure"

    if "cannot find name" in text or "is not defined" in text or "missing env" in text:
        return "MISSING_ENV_OR_BUILD_VAR", "Build requires environment variable or compile-time constant"

    if "node-gyp" in text or "gyp err" in text or "sharp" in text:
        return "NATIVE_NODE_BUILD_FAILURE", "Native Node dependency build failed, often old Gatsby/sharp/node-gyp issue"

    if "missing script" in text:
        return "MISSING_PACKAGE_SCRIPT", "package.json script requested by plan does not exist"

    if "localhost_not_valid" in text or "connection refused" in text:
        return "LOCALHOST_NOT_REACHABLE", "Server did not become reachable on expected localhost port"

    if "serve_failed" in text:
        return "SERVE_FAILED_OTHER", "Deployment command exited early; inspect log"

    return "UNKNOWN_FAILURE", "Could not classify from available logs"


def first_relevant_line(log_text: str):
    patterns = [
        "command not found", "error", "failed", "eresolve", "permission", "not found",
        "cannot find", "missing script", "authentication failed", "repository not found",
        "gyp err", "bundler::permissionerror"
    ]
    for line in log_text.splitlines():
        low = line.lower()
        if any(p in low for p in patterns):
            return line.strip()[:500]
    return ""


def load_latest_records(checkpoint: Path):
    latest = {}
    for line in checkpoint.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
            if r.get("repo_id"):
                latest[r["repo_id"]] = r
        except Exception:
            pass
    return list(latest.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--logs", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    checkpoint = Path(args.checkpoint)
    logs_dir = Path(args.logs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    records = load_latest_records(checkpoint)

    total = len(records)
    successes = [r for r in records if r.get("status") == "success"]
    failures = [r for r in records if r.get("status") == "failed"]

    success_fw = Counter(r.get("detected_framework") for r in successes)
    fail_fw = Counter(r.get("detected_framework") for r in failures)
    fail_reason_code = Counter()
    root_cause = Counter()

    rows = []
    tails_dir = out / "failure_log_tails"
    tails_dir.mkdir(exist_ok=True)

    for r in failures:
        repo_url = r.get("repo_url")
        safe = repo_safe_name(repo_url)
        log_path = logs_dir / safe / "run_host.log"
        runtime_log_path = logs_dir / safe / "host_runtime.log"
        log_text = read_text(log_path) + "\n" + read_text(runtime_log_path)

        code, explanation = classify_failure(r, log_text)
        fail_reason_code[str(r.get("failure_reason") or "").split(":", 1)[0]] += 1
        root_cause[code] += 1

        tail_file = tails_dir / f"{safe}.txt"
        tail_file.write_text(log_text[-12000:], encoding="utf-8", errors="replace")

        v = r.get("validation") or {}
        rows.append({
            "repo_url": repo_url,
            "status": r.get("status"),
            "framework": r.get("detected_framework"),
            "confidence": r.get("confidence"),
            "plan_source": r.get("plan_source"),
            "failure_reason": r.get("failure_reason"),
            "root_cause_code": code,
            "root_cause_explanation": explanation,
            "first_relevant_log_line": first_relevant_line(log_text),
            "http_status": v.get("status_code"),
            "body_len": v.get("body_len"),
            "title": v.get("title"),
            "log_path": str(log_path),
            "tail_file": str(tail_file),
        })

    # Failure records CSV
    with open(out / "failure_records.csv", "w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "repo_url", "status", "framework", "confidence", "plan_source",
            "failure_reason", "root_cause_code", "root_cause_explanation",
            "first_relevant_log_line", "http_status", "body_len", "title",
            "log_path", "tail_file"
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # Summary CSVs
    with open(out / "summary_by_root_cause.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["root_cause_code", "count"])
        for k, v in root_cause.most_common():
            w.writerow([k, v])

    with open(out / "summary_success_by_framework.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["framework", "success_count"])
        for k, v in success_fw.most_common():
            w.writerow([k, v])

    with open(out / "summary_failure_by_framework.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["framework", "failure_count"])
        for k, v in fail_fw.most_common():
            w.writerow([k, v])

    # Markdown report
    md = []
    md.append("# Auto-deployment Failure Analysis\n")
    md.append("## Overall\n")
    md.append(f"- Total checkpointed repositories: **{total}**")
    md.append(f"- Successful local deployments: **{len(successes)}**")
    md.append(f"- Failed deployments: **{len(failures)}**")
    md.append("")
    md.append("## Failure root causes\n")
    for k, v in root_cause.most_common():
        md.append(f"- **{k}**: {v}")
    md.append("")
    md.append("## Success by framework\n")
    for k, v in success_fw.most_common():
        md.append(f"- {k}: {v}")
    md.append("")
    md.append("## Failure by framework\n")
    for k, v in fail_fw.most_common():
        md.append(f"- {k}: {v}")
    md.append("")
    md.append("## Top failed examples\n")
    by_code = defaultdict(list)
    for row in rows:
        by_code[row["root_cause_code"]].append(row)
    for code, items in sorted(by_code.items(), key=lambda kv: len(kv[1]), reverse=True):
        md.append(f"\n### {code} ({len(items)})")
        for row in items[:5]:
            md.append(f"- {row['repo_url']} | framework={row['framework']} | reason={row['failure_reason']}")
            if row["first_relevant_log_line"]:
                md.append(f"  - log: `{row['first_relevant_log_line']}`")

    (out / "failure_analysis_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print("ANALYSIS COMPLETE")
    print("=" * 80)
    print(f"total:   {total}")
    print(f"success: {len(successes)}")
    print(f"failed:  {len(failures)}")
    print()
    print("ROOT CAUSES")
    for k, v in root_cause.most_common():
        print(f"{v:4d}  {k}")
    print()
    print(f"Files written to: {out}")


if __name__ == "__main__":
    main()
