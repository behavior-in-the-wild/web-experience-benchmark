"""
Main runner: evaluate all code patches in code_patches/ with both
regression_tool_v1 and regression_tool_v2.

Usage:
    python3 run_eval.py
    python3 run_eval.py --agents claudecode codex
    python3 run_eval.py --agents aider --limit 5
    python3 run_eval.py --skip-v1
    python3 run_eval.py --skip-v2
    python3 run_eval.py --output-dir /path/to/results

Output structure:
    results/
        eval_summary.json           ← combined results for all agents & patches
        results_claudecode.json     ← per-agent detailed results
        results_codex.json
        results_opencode.json
        results_aider.json
"""

from __future__ import annotations

# ============================================================
# TUNE THIS: number of patches evaluated simultaneously.
# Each worker clones a repo, starts a local server, and runs
# Playwright + GPT calls — so RAM is the main constraint.
# Safe defaults: 4 on a 16 GB machine, 2 on an 8 GB machine.
# ============================================================
MAX_PARALLEL = 4

import argparse
import csv
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Load .env and normalise Azure env var names  (runs in main process AND
# in each worker process because _load_env() is called at module level)
# ---------------------------------------------------------------------------

def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    env_file = root / ".env"
    try:
        from dotenv import load_dotenv
        if env_file.exists():
            load_dotenv(env_file, override=False)
    except ImportError:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if k.strip() not in os.environ:
                        os.environ[k.strip()] = v.strip()

    _alias("AZURE_OPENAI_API_VERSION",        "OPENAI_API_VERSION")
    _alias("AZURE_OPENAI_API_DEPLOYMENT_NAME", "AZURE_DEPLOYMENT")


def _alias(src: str, dst: str) -> None:
    if dst not in os.environ and src in os.environ:
        os.environ[dst] = os.environ[src]


_load_env()

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_THIS_DIR         = Path(__file__).resolve().parent
_ROOT_DIR         = _THIS_DIR.parents[1]          # repo root
_CODE_PATCHES_DIR = _ROOT_DIR / "code_patches"
_DEFAULT_OUT_DIR  = _THIS_DIR / "results"

if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from common import load_template_map  # noqa: E402

ALL_AGENTS = ["claudecode", "codex", "opencode", "aider"]


# ---------------------------------------------------------------------------
# Worker initialiser – configures logging inside each spawned process
# ---------------------------------------------------------------------------

def _worker_init() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(process)d | %(name)s | %(message)s",
    )
    _load_env()   # env vars are not inherited on macOS spawn


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def discover_patches(agent: str) -> list[Path]:
    agent_dir = _CODE_PATCHES_DIR / f"results_{agent}"
    if not agent_dir.exists():
        return []
    return sorted(agent_dir.glob("*.patch"), key=_parse_template_id)


def _parse_template_id(patch_file: Path) -> int:
    try:
        return int(patch_file.stem.split("_")[0])
    except (IndexError, ValueError):
        return 9999


# ---------------------------------------------------------------------------
# Per-patch evaluation  (called inside worker processes)
# ---------------------------------------------------------------------------

def evaluate_one(
    patch_file: Path,
    template_info: dict | None,
    output_dir: Path,
    run_v1: bool,
    run_v2: bool,
) -> dict[str, Any]:
    """Run v1 and/or v2 on a single patch. Returns result dict."""
    log = logging.getLogger(__name__)
    tid = _parse_template_id(patch_file)

    if template_info is None:
        log.warning("Template %d not in CSV (%s)", tid, patch_file.name)
        return {"template_id": tid, "error": f"template {tid} not in CSV"}

    log.info("── [%s] template %d (%s) ──",
             patch_file.name, tid, template_info["framework"])

    patch_out = output_dir / patch_file.stem
    result: dict[str, Any] = {"template_id": tid}

    # ---- v1 ----
    if run_v1:
        try:
            from regression_tool_v1.eval import evaluate_patch as _eval_v1
            r1 = _eval_v1(patch_file, template_info, patch_out / "v1")
            result["v1"] = r1
            _log_summary(log, "v1", patch_file.name, r1)
        except Exception as exc:
            log.error("[v1] crash %s: %s", patch_file.name, exc)
            result["v1"] = {"error": str(exc), "overall_regression": None}

    # ---- v2 ----
    if run_v2:
        try:
            from regression_tool_v2.eval import evaluate_patch as _eval_v2
            r2 = _eval_v2(patch_file, template_info, patch_out / "v2")
            result["v2"] = r2
            _log_summary(log, "v2", patch_file.name, r2)
        except Exception as exc:
            log.error("[v2] crash %s: %s", patch_file.name, exc)
            result["v2"] = {"error": str(exc), "overall_regression": None}

    # Bubble up metadata from whichever tool ran first
    for key in ("v1", "v2"):
        if key in result and "metadata" in result.get(key, {}):
            result["metadata"] = result[key]["metadata"]
            break

    return result


def _log_summary(log, tool: str, name: str, result: dict) -> None:
    if result.get("error"):
        log.info("  [%s] %s → ERROR: %s", tool, name, result["error"])
        return
    overall = result.get("overall_regression")
    flag = "REGRESSION" if overall else "ok"
    details = {k: v.get("regression") for k, v in result.get("checks", {}).items()}
    log.info("  [%s] %s → %s  %s", tool, name, flag, details)


# ---------------------------------------------------------------------------
# Top-level picklable worker function
# (must be defined at module scope so ProcessPoolExecutor can pickle it)
# ---------------------------------------------------------------------------

def _worker(
    patch_file: Path,
    template_info: dict | None,
    output_dir: Path,
    run_v1: bool,
    run_v2: bool,
) -> tuple[str, str, dict]:
    """
    Executed inside a worker process.
    Returns (agent_name, tid_str, result_dict).
    """
    agent   = patch_file.parent.name.replace("results_", "")
    tid_str = str(_parse_template_id(patch_file))
    result  = evaluate_one(patch_file, template_info, output_dir, run_v1, run_v2)
    return agent, tid_str, result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run visual regression evaluation on all code patches."
    )
    parser.add_argument("--agents", nargs="+", default=ALL_AGENTS)
    parser.add_argument("--limit", type=int, default=None,
                        help="Max patches per agent (quick test)")
    parser.add_argument("--skip-v1", action="store_true")
    parser.add_argument("--skip-v2", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument("--parallel", type=int, default=MAX_PARALLEL,
                        help=f"Override MAX_PARALLEL (default {MAX_PARALLEL})")
    args = parser.parse_args()

    run_v1    = not args.skip_v1
    run_v2    = not args.skip_v2
    out_dir   = args.output_dir
    n_workers = args.parallel
    out_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    log = logging.getLogger(__name__)
    log.info("Loaded .env — Azure endpoint: %s", os.getenv("AZURE_OPENAI_ENDPOINT", "NOT SET"))

    template_map = load_template_map()
    log.info("Loaded %d templates from CSV", len(template_map))

    # ── Build task list (all agents × all patches) ──────────────────────────
    tasks: list[tuple] = []
    for agent in args.agents:
        patches = discover_patches(agent)
        if not patches:
            log.warning("No patches found for agent: %s", agent)
            continue
        if args.limit:
            patches = patches[: args.limit]
        for pf in patches:
            tid = _parse_template_id(pf)
            tasks.append((
                pf,
                template_map.get(tid),
                out_dir / agent,   # output root per agent; worker appends patch stem
                run_v1,
                run_v2,
            ))

    log.info("Total tasks: %d  |  MAX_PARALLEL: %d", len(tasks), n_workers)

    # ── Run in parallel ──────────────────────────────────────────────────────
    # all_results: { agent -> { tid_str -> result } }
    all_results: dict[str, dict] = {a: {} for a in args.agents}

    # Per-agent JSON paths — written incrementally as results arrive
    agent_json: dict[str, Path] = {
        a: out_dir / f"results_{a}.json" for a in args.agents
    }

    # Single CSV that accumulates every patch result as they complete
    csv_path = out_dir / "eval_results.csv"

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_worker_init,
    ) as executor:

        future_to_patch = {
            executor.submit(_worker, *task): task[0]
            for task in tasks
        }

        completed = 0
        for future in as_completed(future_to_patch):
            patch_file = future_to_patch[future]
            completed += 1
            try:
                agent, tid_str, result = future.result()
            except Exception as exc:
                agent   = patch_file.parent.name.replace("results_", "")
                tid_str = str(_parse_template_id(patch_file))
                log.error("Worker crash [%s/%s]: %s", agent, patch_file.name, exc)
                result  = {"template_id": int(tid_str) if tid_str.isdigit() else -1,
                           "error": str(exc)}

            all_results[agent][tid_str] = result

            # Incremental JSON write — main process is the sole writer, no lock needed
            agent_json[agent].write_text(
                json.dumps(all_results[agent], indent=2, default=str),
                encoding="utf-8",
            )

            # Incremental CSV write — one row per completed patch
            _append_csv_row(csv_path, agent, result, template_map)

            log.info("Progress: %d/%d done  (CSV → %s)", completed, len(tasks), csv_path)

    # ── Final summary ────────────────────────────────────────────────────────
    summary_path = out_dir / "eval_summary.json"
    summary_path.write_text(
        json.dumps(all_results, indent=2, default=str), encoding="utf-8"
    )
    log.info("Summary → %s", summary_path)

    _print_table(all_results, run_v1, run_v2)


# ---------------------------------------------------------------------------
# Output table
# ---------------------------------------------------------------------------

_CSV_HEADER = [
    "timestamp", "agent", "template_id", "framework",
    # v1
    "v1_gpt", "v1_dom_lsh", "v1_jaccard", "v1_console", "v1_overall",
    # v2
    "v2_structural", "v2_jaccard", "v2_gpt", "v2_console", "v2_overall",
    # metadata
    "cwv_desktop_lcp", "cwv_desktop_cls", "cwv_desktop_fid",
    "cwv_mobile_lcp",  "cwv_mobile_cls",  "cwv_mobile_fid",
    "error",
]


def _yn(val) -> str:
    return "YES" if val is True else ("NO" if val is False else "ERR")


def _append_csv_row(
    csv_path: Path,
    agent: str,
    result: dict,
    template_map: dict,
) -> None:
    """Append one row to the CSV after a patch evaluation completes."""
    tid = result.get("template_id", -1)
    tinfo = template_map.get(tid, {})
    framework = tinfo.get("framework", "")

    v1 = result.get("v1", {})
    v1c = v1.get("checks", {})
    v2 = result.get("v2", {})
    v2c = v2.get("checks", {})

    meta = result.get("metadata", {})
    desk = meta.get("desktop", {})
    mob  = meta.get("mobile",  {})

    row = {
        "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent":        agent,
        "template_id":  tid,
        "framework":    framework,
        "v1_gpt":       _yn(v1c.get("gpt_visual",    {}).get("regression")),
        "v1_dom_lsh":   _yn(v1c.get("dom_lsh",        {}).get("regression")),
        "v1_jaccard":   _yn(v1c.get("jaccard_text",   {}).get("regression")),
        "v1_console":   _yn(v1c.get("console_errors", {}).get("regression")),
        "v1_overall":   _yn(v1.get("overall_regression")),
        "v2_structural":_yn(v2c.get("structural",     {}).get("regression")),
        "v2_jaccard":   _yn(v2c.get("jaccard_text",   {}).get("regression")),
        "v2_gpt":       _yn(v2c.get("gpt_visual",     {}).get("regression")),
        "v2_console":   _yn(v2c.get("console_errors", {}).get("regression")),
        "v2_overall":   _yn(v2.get("overall_regression")),
        "cwv_desktop_lcp": desk.get("lcp", ""),
        "cwv_desktop_cls": desk.get("cls", ""),
        "cwv_desktop_fid": desk.get("fid", ""),
        "cwv_mobile_lcp":  mob.get("lcp",  ""),
        "cwv_mobile_cls":  mob.get("cls",  ""),
        "cwv_mobile_fid":  mob.get("fid",  ""),
        "error":           result.get("error", ""),
    }

    write_header = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_HEADER)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def _print_table(all_results: dict, run_v1: bool, run_v2: bool) -> None:
    cols = ["Agent", "Template"]
    if run_v1:
        cols += ["v1:gpt", "v1:dom_lsh", "v1:jaccard", "v1:console", "v1:overall"]
    if run_v2:
        cols += ["v2:struct", "v2:gpt", "v2:console", "v2:overall"]

    W = 14
    header = "  ".join(f"{c:<{W}}" for c in cols)
    sep    = "=" * len(header)
    print(f"\n{sep}\n{header}\n{sep}")

    def yn(val) -> str:
        return "YES" if val is True else ("NO" if val is False else "ERR")

    for agent, ares in sorted(all_results.items()):
        for tid_str, res in sorted(ares.items(),
                                   key=lambda x: int(x[0]) if x[0].isdigit() else 9999):
            if "error" in res and "v1" not in res and "v2" not in res:
                continue
            row = [agent, tid_str]
            if run_v1:
                v1 = res.get("v1", {})
                c  = v1.get("checks", {})
                row += [
                    yn(c.get("gpt_visual",    {}).get("regression")),
                    yn(c.get("dom_lsh",        {}).get("regression")),
                    yn(c.get("jaccard_text",   {}).get("regression")),
                    yn(c.get("console_errors", {}).get("regression")),
                    yn(v1.get("overall_regression")),
                ]
            if run_v2:
                v2 = res.get("v2", {})
                c  = v2.get("checks", {})
                row += [
                    yn(c.get("structural",     {}).get("regression")),
                    yn(c.get("gpt_visual",     {}).get("regression")),
                    yn(c.get("console_errors", {}).get("regression")),
                    yn(v2.get("overall_regression")),
                ]
            print("  ".join(f"{cell:<{W}}" for cell in row))

    print(f"{sep}\n")


if __name__ == "__main__":
    main()
