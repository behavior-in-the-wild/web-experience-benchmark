#!/usr/bin/env python3
"""
CPU-affinity isolation test.

Picks a fixed set of pre-existing patches from static_sugg_eval/gemma-4-31b-it,
clones baselines once, then re-runs CWV measurement at four parallelism levels
(PARALLEL=20, 10, 15, 5) using CPU-affinity slot scheduling.

Usage (from project root, venv active):
    PYTHONPATH=src python3 scripts/isolation_test/isolation_test.py

Environment overrides:
    TEST_ROOT        output dir              (default: isolation_test/)
    DUMP_MODEL       model dir to source patches from (default: gemma-4-31b-it)
    LIMIT            unique sites to include (default: 14 → ~42 patches)
    NUM_RUNS         Lighthouse runs per measurement (default: 3)
    PARALLELS        comma-separated levels  (default: 20,10,15,5)
    CPUS_PER_SLOT    CPUs per job slot       (default: 4)
    BASE_PORT        first port to use       (default: 15100)
    SKIP_CLONE       1 to skip re-cloning if baselines already exist
"""
import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "src"))

from docker_tool.hosting import start_host, stop_host
from docker_tool.resources import SlotLease

# ── Config from env ──────────────────────────────────────────────────────────

TEST_ROOT    = Path(os.environ.get("TEST_ROOT",   ROOT / "isolation_test"))
DUMP_MODEL   = os.environ.get("DUMP_MODEL",       "gemma-4-31b-it")
LIMIT        = int(os.environ.get("LIMIT",        "14"))
NUM_RUNS     = int(os.environ.get("NUM_RUNS",     "3"))
PARALLELS    = [int(x) for x in os.environ.get("PARALLELS", "20,10,15,5").split(",")]
CPUS_PER_SLOT= int(os.environ.get("CPUS_PER_SLOT","4"))
BASE_PORT    = int(os.environ.get("BASE_PORT",    "15100"))
SKIP_CLONE   = os.environ.get("SKIP_CLONE", "0") == "1"

DUMP_ROOT    = ROOT / "final_result_dumps/static_sugg_eval" / DUMP_MODEL / "results"
CSV_PATH     = ROOT / "harness/SAMPLE/input_100.csv"
CWV_SCRIPT   = ROOT / "src/cwv_tool/cwv_benchmark.py"
BASELINES_DIR= TEST_ROOT / "baselines"
MANIFEST_PATH= TEST_ROOT / "manifest.json"
TMPDIR       = Path(os.environ.get("TMPDIR", "/dev/shm"))

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[isolation-test] {msg}", flush=True)


def make_slot(slot_id: int, mode: str = "local") -> SlotLease:
    start = slot_id * CPUS_PER_SLOT
    cpuset = ",".join(str(start + i) for i in range(CPUS_PER_SLOT))
    return SlotLease(slot_id=slot_id, cpuset=cpuset, cpu_count=CPUS_PER_SLOT,
                     memory="4g", queue_wait_ms=0, mode=mode)


def free_port(port: int) -> None:
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)


# ── Phase 1: Build manifest ───────────────────────────────────────────────────

def build_manifest() -> list[dict]:
    csv.field_size_limit(10 ** 7)
    with open(CSV_PATH) as f:
        rows = {r["ID"]: r for r in csv.DictReader(f)}

    entries = []
    seen_ids: list[str] = []

    for result_dir in sorted(DUMP_ROOT.iterdir()):
        if not result_dir.is_dir():
            continue
        # name: {ID}_s{N}_template_opencode_os_direct
        parts = result_dir.name.split("_s")
        if len(parts) < 2:
            continue
        row_id = parts[0]
        sugg_str = parts[1].split("_")[0]

        if row_id not in rows:
            continue
        if row_id not in seen_ids:
            if len(seen_ids) >= LIMIT:
                continue
            seen_ids.append(row_id)

        try:
            sugg_idx = int(sugg_str)
        except ValueError:
            continue

        patch_files = list(result_dir.glob("*.patch"))
        if not patch_files:
            continue

        row = rows[row_id]
        entries.append({
            "id":            row_id,
            "sugg_idx":      sugg_idx,
            "patch_file":    str(patch_files[0]),
            "repo_id":       row["REPO_ID"],
            "framework":     row["FRAMEWORK"],
            "host_file_path":row.get("HOST_FILE_PATH", ""),
            "commit_id":     row["COMMIT_ID"],
        })

    log(f"Manifest: {len(entries)} patches from {len(seen_ids)} sites")
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(entries, f, indent=2)
    return entries


# ── Phase 2: Clone baselines ──────────────────────────────────────────────────

def clone_baseline(row_id: str, repo_id: str, commit_id: str) -> bool:
    target = BASELINES_DIR / row_id
    if target.exists():
        return True
    tmp = BASELINES_DIR / (row_id + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        url = f"https://github.com/{repo_id}.git"
        r = subprocess.run(
            ["git", "-C", str(tmp), "init", "-q"],
            capture_output=True, timeout=30
        )
        subprocess.run(
            ["git", "-C", str(tmp), "remote", "add", "origin", url],
            capture_output=True, timeout=10
        )
        r = subprocess.run(
            ["git", "-C", str(tmp), "-c", "credential.helper=",
             "-c", "http.version=HTTP/1.1",
             "fetch", "--depth", "1", "--no-tags", "origin", commit_id],
            capture_output=True, timeout=120
        )
        if r.returncode != 0:
            log(f"  WARN: fetch failed for {repo_id}: {r.stderr.decode()[:200]}")
            return False
        subprocess.run(
            ["git", "-C", str(tmp), "checkout", "--detach", "FETCH_HEAD"],
            capture_output=True, timeout=30
        )
        # Stage everything so git apply works cleanly
        subprocess.run(["git", "-C", str(tmp), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "baseline",
                        "--allow-empty"], capture_output=True)
        tmp.rename(target)
        return True
    except Exception as e:
        log(f"  WARN: clone exception for {repo_id}: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def clone_all_baselines(entries: list[dict]) -> None:
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    unique = {e["id"]: e for e in entries}
    for row_id, entry in sorted(unique.items()):
        target = BASELINES_DIR / row_id
        if SKIP_CLONE and target.exists():
            log(f"  baseline {row_id}: cached")
            continue
        log(f"  cloning {entry['repo_id']}...", )
        ok = clone_baseline(row_id, entry["repo_id"], entry["commit_id"])
        print("OK" if ok else "FAILED")


# ── Phase 3: CWV measurement worker ──────────────────────────────────────────

def measure_one(entry: dict, slot_id: int, out_dir: Path) -> dict:
    entry_id = f"{entry['id']}_s{entry['sugg_idx']}"
    work_dir = TMPDIR / f"isol_{entry_id}_{slot_id}_{os.getpid()}"
    port = BASE_PORT + slot_id
    result = {"entry_id": entry_id, "slot_id": slot_id, "port": port,
              "status": "error", "error": None}

    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. Copy baseline
        baseline = BASELINES_DIR / entry["id"]
        if not baseline.exists():
            result["error"] = f"baseline missing: {baseline}"
            return result
        site_dir = work_dir / "site"
        shutil.copytree(str(baseline), str(site_dir))

        # 2. Apply patch
        patch_file = Path(entry["patch_file"])
        r = subprocess.run(
            ["git", "-C", str(site_dir), "apply", "--whitespace=fix", str(patch_file)],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            # Try with --reject (partial apply) rather than hard-failing
            subprocess.run(
                ["git", "-C", str(site_dir), "apply", "--reject", str(patch_file)],
                capture_output=True
            )

        # 3. Start host
        slot = make_slot(slot_id)
        free_port(port)
        host = start_host(
            repo_dir=site_dir,
            framework=entry["framework"],
            host_file_path=entry["host_file_path"] or None,
            port=port,
            log=work_dir / "host.log",
            slot=slot,
            mode="local",
        )
        if host.status != "success":
            result["error"] = f"host failed: {host.error}"
            return result

        try:
            # 4. Measure CWV
            slot_json = json.dumps({
                "slot_id": slot.slot_id, "cpuset": slot.cpuset,
                "cpu_count": slot.cpu_count, "memory": slot.memory,
                "queue_wait_ms": 0, "mode": "cwv",
            })
            out_json = out_dir / f"{entry_id}.json"
            err_log  = work_dir / "cwv_stderr.txt"
            t0 = time.time()
            cwv = subprocess.run(
                [sys.executable, str(CWV_SCRIPT),
                 "--url", host.url,
                 "--device", "mobile",
                 "--num-runs", str(NUM_RUNS),
                 "--slot-json", slot_json],
                env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                stdout=open(out_json, "w"),
                stderr=open(err_log, "w"),
                timeout=600,
            )
            elapsed = round(time.time() - t0, 1)
            if cwv.returncode != 0 or not out_json.exists():
                result["error"] = f"cwv rc={cwv.returncode}"
                return result
            with open(out_json) as f:
                data = json.load(f)
            agg = data.get("aggregated") or data  # single-URL path nests under "aggregated"
            result.update({
                "status":      "success",
                "elapsed_s":   elapsed,
                "lcp_ms":      agg.get("LCP_median"),
                "fcp_ms":      agg.get("FCP_median"),
                "lcp_stdev":   agg.get("LCP_stdev"),
                "valid_runs":  data.get("num_runs"),
            })
        finally:
            stop_host(pid=host.pid)
    finally:
        shutil.rmtree(str(work_dir), ignore_errors=True)

    return result


def run_parallel(entries: list[dict], parallel: int, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Slot pool: ensures each concurrent task gets a unique slot
    slot_q: Queue = Queue()
    for i in range(parallel):
        slot_q.put(i)

    results = []
    t0 = time.time()

    def task(entry):
        slot_id = slot_q.get()
        try:
            return measure_one(entry, slot_id, out_dir)
        finally:
            slot_q.put(slot_id)

    with ThreadPoolExecutor(max_workers=parallel) as exe:
        futs = {exe.submit(task, e): e for e in entries}
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            lcp = res.get("lcp_ms", "N/A")
            err = f" err={res['error']}" if res.get("error") else ""
            print(f"  {res['entry_id']:40s} slot={res['slot_id']:>2}  "
                  f"LCP={str(lcp):>7}{err}", flush=True)

    elapsed = round(time.time() - t0, 1)
    summary = {"parallel": parallel, "num_runs": NUM_RUNS, "elapsed_s": elapsed,
               "results": results}
    with open(out_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    n_ok = sum(1 for r in results if r["status"] == "success")
    log(f"P{parallel}: {n_ok}/{len(results)} OK in {elapsed:.0f}s")
    return summary


# ── Phase 4: Analysis ─────────────────────────────────────────────────────────

def analyze() -> None:
    runs = []
    for p in sorted(PARALLELS, reverse=True):
        d = TEST_ROOT / f"p{p:02d}"
        sf = d / "summary.json"
        if not sf.exists():
            continue
        with open(sf) as f:
            s = json.load(f)
        lcps = [r["lcp_ms"] for r in s["results"]
                if r.get("status") == "success" and r.get("lcp_ms") is not None]
        runs.append({"parallel": p, "elapsed": s["elapsed_s"], "lcps": lcps,
                     "n": len(s["results"]),
                     "n_ok": sum(1 for r in s["results"] if r["status"] == "success")})

    if not runs:
        log("No completed runs found for analysis.")
        return

    print("\n" + "=" * 72)
    print("  ISOLATION TEST RESULTS — LCP (mobile, ms)")
    print("=" * 72)
    print(f"  {'P':>4}  {'N_ok':>5}  {'mean':>7}  {'stdev':>7}  "
          f"{'p50':>7}  {'p95':>7}  {'elapsed':>8}")
    print("  " + "-" * 68)
    for run in runs:
        lcps = sorted(run["lcps"])
        if not lcps:
            print(f"  P{run['parallel']:>2}   (no data)")
            continue
        mean  = statistics.mean(lcps)
        stdev = statistics.stdev(lcps) if len(lcps) > 1 else 0.0
        p50   = lcps[len(lcps) // 2]
        p95   = lcps[min(int(len(lcps) * 0.95), len(lcps) - 1)]
        print(f"  P{run['parallel']:>2}   {run['n_ok']:>5}  {mean:>7.0f}  "
              f"{stdev:>7.0f}  {p50:>7.0f}  {p95:>7.0f}  {run['elapsed']:>7.0f}s")

    # Per-patch comparison
    patch_data: dict[str, dict[int, float]] = {}
    for run in runs:
        p = run["parallel"]
        d = TEST_ROOT / f"p{p:02d}" / "summary.json"
        with open(d) as f:
            s = json.load(f)
        for r in s["results"]:
            if r.get("status") == "success" and r.get("lcp_ms") is not None:
                patch_data.setdefault(r["entry_id"], {})[p] = r["lcp_ms"]

    ps = sorted({r["parallel"] for r in runs}, reverse=True)
    print(f"\n  {'patch':40s}  " + "  ".join(f"P{p:>2}" for p in ps))
    print("  " + "-" * (44 + 6 * len(ps)))
    for pid, vals in sorted(patch_data.items()):
        row_vals = "  ".join(f"{vals.get(p, '---'):>5}" for p in ps)
        print(f"  {pid:40s}  {row_vals}")

    print("\n  Isolation working if stdev at P20 ≈ stdev at P5.")
    print("=" * 72 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    TEST_ROOT.mkdir(parents=True, exist_ok=True)

    log(f"Test root:    {TEST_ROOT}")
    log(f"Dump model:   {DUMP_MODEL}")
    log(f"Limit:        {LIMIT} sites")
    log(f"Parallelisms: {PARALLELS}")
    log(f"Runs/measure: {NUM_RUNS}")
    log(f"CPUs/slot:    {CPUS_PER_SLOT}")
    log(f"Base port:    {BASE_PORT}")
    print()

    # Phase 1: manifest
    if MANIFEST_PATH.exists():
        log("Manifest already exists, loading...")
        with open(MANIFEST_PATH) as f:
            entries = json.load(f)
    else:
        log("Building manifest...")
        entries = build_manifest()
    log(f"  {len(entries)} patch entries")
    print()

    # Phase 2: baselines
    log("Preparing baselines...")
    clone_all_baselines(entries)
    print()

    # Phase 3: run at each parallelism level
    for parallel in PARALLELS:
        out_dir = TEST_ROOT / f"p{parallel:02d}"
        if (out_dir / "summary.json").exists():
            log(f"P{parallel}: already done (delete {out_dir}/summary.json to re-run)")
            continue
        log(f"Running P{parallel}...")
        run_parallel(entries, parallel, out_dir)
        print()

    # Phase 4: analysis
    analyze()


if __name__ == "__main__":
    main()
