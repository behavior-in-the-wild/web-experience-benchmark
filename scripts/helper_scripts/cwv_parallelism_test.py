#!/usr/bin/env python3
"""
CWV Parallelism Benchmark

Clones 32 good static-HTML repos once into /dev/shm, keeps all servers running,
then sweeps parallelism levels [4, 8, 16, 32] — measuring every repo at each level.
Shows how LCP/CLS/INP/TTFB drift as concurrent Chrome instances increase.

Usage:
    python scripts/helper_scripts/cwv_parallelism_test.py
    python scripts/helper_scripts/cwv_parallelism_test.py --device desktop
    python scripts/helper_scripts/cwv_parallelism_test.py --runs 5 --repos 16
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import signal
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import set_start_method
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SCRIPT_DIR / "src"))

from cwv_optimizer.services.performance_testing import (
    calculate_aggregated_metrics,
    measure_multiple_runs,
)

# ── Config ─────────────────────────────────────────────────────────────────────
OSS_RUNS_DIR = SCRIPT_DIR / "oss_model_runs"
CSV_PATH     = SCRIPT_DIR / "harness/SAMPLE/input_100.csv"
TMP_DIR      = Path("/dev/shm/ayush/cwv_parallel_test")
OUT_DIR      = SCRIPT_DIR / "dumps" / "cwv_parallelism"
BASE_PORT    = 13000

# ── Worker function (module-level so ProcessPoolExecutor can pickle it) ────────
def _measure_worker(args: Tuple) -> Dict:
    """
    Run in an isolated subprocess. Each call creates a fresh Playwright browser.
    Returns {"url", "id", "status", "agg", "runs"} or {"status": "error", "error": ...}
    """
    url, repo_id, num_runs, device = args
    try:
        import asyncio, sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))
        from cwv_optimizer.services.performance_testing import (
            measure_multiple_runs,
            calculate_aggregated_metrics,
        )
        # Suppress noisy playwright / cwv_optimizer logs inside workers
        import logging
        logging.getLogger().setLevel(logging.ERROR)

        runs, _settle, _ok = asyncio.run(
            measure_multiple_runs(url=url, device=device, headless=True, num_runs=num_runs)
        )
        agg = calculate_aggregated_metrics(runs)
        return {"url": url, "id": repo_id, "status": "ok", "agg": agg,
                "valid_runs": agg.get("valid_runs", 0)}
    except Exception as exc:
        return {"url": url, "id": repo_id, "status": "error", "error": str(exc), "agg": {}}


# ── Repo helpers ───────────────────────────────────────────────────────────────
def _find_good_repos(n: int) -> List[Dict]:
    csv.field_size_limit(10 ** 7)
    jobs: Dict[str, Dict] = {}
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            jobs[row["ID"]] = row

    good: Dict[str, Dict] = {}
    for model_dir in sorted(os.listdir(OSS_RUNS_DIR)):
        results_dir = OSS_RUNS_DIR / model_dir / "results"
        if not results_dir.is_dir():
            continue
        for task in sorted(os.listdir(results_dir)):
            job_id = task.split("_")[0]
            if job_id in good:
                continue
            vjson = results_dir / task / "visual.json"
            if not vjson.exists():
                continue
            try:
                d = json.load(open(vjson))
            except Exception:
                continue
            if d.get("overall_regression") is not False:
                continue
            job = jobs.get(job_id, {})
            fw = (job.get("FRAMEWORK") or "").lower().strip()
            if fw not in ("static html", "", "static"):
                continue
            good[job_id] = {
                "id": job_id,
                "repo_id": job.get("REPO_ID"),
                "commit": (job.get("COMMIT_ID") or "").strip(),
            }
            if len(good) >= n:
                return list(good.values())
    return list(good.values())


def _clone_one(args: Tuple) -> Dict:
    repo_info, dst_root = args
    dst = Path(dst_root) / repo_info["id"]
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/{repo_info['repo_id']}.git"
    r = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dst)],
        capture_output=True, timeout=120,
    )
    ok = r.returncode == 0 and dst.exists() and any(dst.iterdir())
    return {**repo_info, "clone_ok": ok, "local_path": str(dst) if ok else None}


def _start_server(port: int, path: str) -> subprocess.Popen:
    subprocess.run(["fuser", "-k", "-KILL", f"{port}/tcp"],
                   capture_output=True)
    return subprocess.Popen(
        ["python3", "-m", "http.server", str(port)],
        cwd=path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )


def _server_ready(port: int, timeout: int = 30) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen(f"http://localhost:{port}/", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


# ── Parallelism sweep ──────────────────────────────────────────────────────────
def _run_sweep(repos: List[Dict], P: int, num_runs: int, device: str) -> Tuple[List[Dict], float]:
    tasks = [
        (f"http://localhost:{r['port']}", r["id"], num_runs, device)
        for r in repos
    ]
    results: List[Optional[Dict]] = [None] * len(tasks)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=P) as ex:
        futs = {ex.submit(_measure_worker, t): i for i, t in enumerate(tasks)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:
                results[i] = {"url": tasks[i][0], "id": repos[i]["id"],
                               "status": "error", "error": str(exc), "agg": {}}
    return results, time.time() - t0


# ── Reporting ──────────────────────────────────────────────────────────────────
METRICS = ["LCP_p75", "CLS_median", "INP_p75", "TTFB_median", "FCP_p75"]
UNITS   = {"LCP_p75": "ms", "CLS_median": "", "INP_p75": "ms",
           "TTFB_median": "ms", "FCP_p75": "ms"}


def _print_table(sweep: Dict[int, List[Dict]], repos: List[Dict], levels: List[int], device: str, num_runs: int):
    print("\n" + "=" * 90)
    print(f"  CWV Parallelism Benchmark  |  device={device}  |  num_runs_per_measurement={num_runs}")
    print("=" * 90)

    # ── Per-repo LCP table ──
    col_w = 14
    print(f"\n{'Repo':>8}  {'':4}", end="")
    for P in levels:
        hdr = f"P={P} LCP(ms)"
        print(f"  {hdr:>{col_w}}", end="")
    print()
    print("-" * (14 + len(levels) * (col_w + 2)))
    for i, r in enumerate(repos):
        print(f"{r['id']:>8}  {'':4}", end="")
        for P in levels:
            res = sweep.get(P, [])[i] if i < len(sweep.get(P, [])) else None
            if res and res.get("status") == "ok":
                lcp = (res["agg"] or {}).get("LCP_p75")
                cell = f"{lcp:>6.0f}" if lcp is not None else "  N/A "
            else:
                cell = "  ERR "
            print(f"  {cell:>{col_w}}", end="")
        print()

    # ── Summary stats ──
    print(f"\n{'':=<90}")
    print("  Summary: mean ± stdev across all repos for each parallelism level")
    print(f"  (higher P = more concurrent Chrome instances on same machine)")
    print(f"{'':=<90}")

    hdr_line = f"  {'Metric':<18}"
    for P in levels:
        hdr_line += f"  {'P='+str(P):>15}"
    print(hdr_line)
    print("  " + "-" * (18 + len(levels) * 17))

    for metric in METRICS:
        unit = UNITS.get(metric, "")
        row_str = f"  {metric:<18}"
        baseline_mean = None
        for P in levels:
            vals = []
            for res in sweep.get(P, []):
                if res and res.get("status") == "ok":
                    v = (res.get("agg") or {}).get(metric)
                    if v is not None:
                        try:
                            vals.append(float(v))
                        except (TypeError, ValueError):
                            pass
            vals = [v for v in vals if not math.isnan(v)]
            if vals:
                mu = statistics.mean(vals)
                sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
                if baseline_mean is None:
                    baseline_mean = mu
                pct = f"(+{(mu/baseline_mean - 1)*100:.0f}%)" if baseline_mean and P != levels[0] else ""
                cell = f"{mu:>6.0f}±{sd:<4.0f}{unit} {pct}"
                row_str += f"  {cell:>15}"
            else:
                row_str += f"  {'N/A':>15}"
        print(row_str)

    # ── Timing ──
    print(f"\n  {'Wall time (s)':<18}", end="")
    for P in levels:
        t = sweep.get(f"_time_{P}")
        if t:
            print(f"  {t:>12.1f}s  ", end="")
    print()

    # ── Error summary ──
    print()
    for P in levels:
        errs = [r for r in sweep.get(P, []) if r and r.get("status") != "ok"]
        if errs:
            print(f"  P={P}: {len(errs)} error(s): {[e.get('id') for e in errs]}")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device",  default="mobile",  choices=["mobile", "desktop"])
    parser.add_argument("--runs",    type=int, default=3,  help="CWV runs per measurement (default 3)")
    parser.add_argument("--repos",   type=int, default=32, help="Number of repos to test (default 32)")
    parser.add_argument("--levels",  default="4,8,16,32", help="Comma-separated parallelism levels")
    parser.add_argument("--out",     default=str(OUT_DIR), help="Output directory")
    args = parser.parse_args()

    levels = [int(x) for x in args.levels.split(",")]
    n_repos = args.repos
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    log = logging.getLogger(__name__)

    # ── 1. Find candidates ──────────────────────────────────────────────────────
    print(f"[1/4] Finding {n_repos} good static-HTML repos...")
    candidates = _find_good_repos(n_repos + 10)  # +10 buffer for clone/serve failures
    print(f"      Found {len(candidates)} candidates")

    # ── 2. Clone in parallel ───────────────────────────────────────────────────
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[2/4] Cloning up to {len(candidates)} repos → {TMP_DIR}  (parallel 16)")
    t0 = time.time()
    clone_args = [(r, str(TMP_DIR)) for r in candidates]
    cloned: List[Dict] = [None] * len(candidates)
    with ProcessPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_clone_one, a): i for i, a in enumerate(clone_args)}
        for fut in as_completed(futs):
            i = futs[fut]
            cloned[i] = fut.result()

    ok_clones = [r for r in cloned if r and r.get("clone_ok")]
    fail_clones = [r for r in cloned if r and not r.get("clone_ok")]
    print(f"      Cloned {len(ok_clones)}/{len(candidates)} OK  "
          f"({len(fail_clones)} failed)  {time.time()-t0:.1f}s")
    if fail_clones:
        print(f"      Failed: {[r['id'] for r in fail_clones]}")

    repos = ok_clones[:n_repos]
    if len(repos) < n_repos:
        print(f"      WARNING: only {len(repos)} repos available (wanted {n_repos})")

    # ── 3. Start all servers ───────────────────────────────────────────────────
    print(f"[3/4] Starting {len(repos)} HTTP servers (ports {BASE_PORT}–{BASE_PORT+len(repos)-1})...")
    servers = []
    for idx, r in enumerate(repos):
        port = BASE_PORT + idx
        repos[idx]["port"] = port
        proc = _start_server(port, r["local_path"])
        servers.append(proc)

    # Wait for all servers
    ready_flags = []
    for r in repos:
        ok = _server_ready(r["port"], timeout=30)
        ready_flags.append(ok)
        status = "✓" if ok else "✗"
        print(f"      {status} port {r['port']}  ({r['id']})")

    repos = [r for r, ok in zip(repos, ready_flags) if ok]
    print(f"      {sum(ready_flags)}/{len(ready_flags)} servers ready")

    if not repos:
        print("ERROR: no servers ready — aborting")
        return 1

    # ── 4. Parallelism sweep ───────────────────────────────────────────────────
    print(f"[4/4] Sweeping parallelism {levels}  "
          f"({len(repos)} repos × {args.runs} runs each,  device={args.device})")
    sweep: Dict = {}
    try:
        for P in levels:
            print(f"\n  ── P={P} ──────────────────────────────────────")
            results, elapsed = _run_sweep(repos, P, args.runs, args.device)
            sweep[P] = results
            sweep[f"_time_{P}"] = elapsed
            ok_count = sum(1 for r in results if r and r.get("status") == "ok")
            print(f"     done: {ok_count}/{len(repos)} OK  in {elapsed:.1f}s")
            # Quick per-level summary (filter NaN before statistics)
            import math
            lcps = []
            for r in results:
                if r and r.get("status") == "ok":
                    v = (r.get("agg") or {}).get("LCP_p75")
                    if v is not None:
                        fv = float(v)
                        if not math.isnan(fv):
                            lcps.append(fv)
            if lcps:
                print(f"     LCP p75: mean={statistics.mean(lcps):.0f}ms  "
                      f"std={statistics.stdev(lcps) if len(lcps)>1 else 0:.0f}ms  "
                      f"min={min(lcps):.0f}ms  max={max(lcps):.0f}ms")

            # Checkpoint after each level so data isn't lost on later crash
            _ckpt = out_dir / f"checkpoint_P{P}_{args.device}.json"
            with open(_ckpt, "w") as _f:
                json.dump({"P": P, "repos": repos, "results": results,
                           "wall_time": elapsed, "device": args.device,
                           "num_runs": args.runs},
                          _f, indent=2,
                          default=lambda o: float(o) if hasattr(o, "item") else str(o))
            print(f"     checkpoint → {_ckpt}")

        _print_table(sweep, repos, levels, args.device, args.runs)

        # Save results
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = out_dir / f"parallelism_{args.device}_{ts}.json"
        with open(fname, "w") as f:
            json.dump({
                "device": args.device,
                "num_runs": args.runs,
                "levels": levels,
                "repos": repos,
                "results": {
                    str(P): sweep[P]
                    for P in levels
                },
                "wall_times": {str(P): sweep.get(f"_time_{P}") for P in levels},
            }, f, indent=2, default=lambda o: float(o) if hasattr(o, "item") else str(o))
        print(f"\nResults saved → {fname}")

    finally:
        print("\nStopping servers...")
        for proc in servers:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        print("Done.")

    return 0


if __name__ == "__main__":
    try:
        set_start_method("spawn")
    except RuntimeError:
        pass  # already set
    sys.exit(main())
