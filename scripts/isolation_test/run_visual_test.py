#!/usr/bin/env python3
"""
Visual regression parallel stress test.

Runs visual_validate.py on the same 42 patches at PARALLEL=20 with CPU
affinity slot scheduling. Compares results to ground truth from the
static_sugg_eval dump and reports false positives, false negatives, and errors.

Usage:
    PYTHONPATH=src python3 scripts/isolation_test/run_visual_test.py

Overrides:
    PARALLEL=20
    OUT_DIR=isolation_test/visual_p20
    DUMP_MODEL=gemma-4-31b-it
    TIMEOUT=480     (seconds per visual job)
"""
import json
import os
import shutil
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

PARALLEL    = int(os.environ.get("PARALLEL",  "20"))
OUT_DIR     = Path(os.environ.get("OUT_DIR",  ROOT / "isolation_test/visual_p20"))
DUMP_MODEL  = os.environ.get("DUMP_MODEL",    "gemma-4-31b-it")
TIMEOUT     = int(os.environ.get("TIMEOUT",   "480"))
CPUS_PER_SLOT = int(os.environ.get("CPUS_PER_SLOT", "4"))
BASE_PORT   = int(os.environ.get("BASE_PORT", "15200"))
TMPDIR      = Path(os.environ.get("TMPDIR",   "/dev/shm"))

MANIFEST    = ROOT / "isolation_test/manifest.json"
BASELINES   = ROOT / "isolation_test/baselines"
DUMP_ROOT   = ROOT / "final_result_dumps/static_sugg_eval" / DUMP_MODEL / "results"
VISUAL_SCRIPT = ROOT / "src/regression_tool/visual_validate.py"


def log(msg):
    print(f"[visual-test] {msg}", flush=True)


def make_slot(slot_id):
    start = slot_id * CPUS_PER_SLOT
    cpuset = ",".join(str(start + i) for i in range(CPUS_PER_SLOT))
    return SlotLease(slot_id=slot_id, cpuset=cpuset, cpu_count=CPUS_PER_SLOT,
                     memory="4g", queue_wait_ms=0, mode="local")


def free_port(port):
    subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)


def load_ground_truth(manifest):
    gt = {}
    for e in manifest:
        name = f"{e['id']}_s{e['sugg_idx']}_template_opencode_os_direct"
        vj = DUMP_ROOT / name / "visual.json"
        key = f"{e['id']}_s{e['sugg_idx']}"
        if vj.exists():
            try:
                d = json.load(open(vj))
                gt[key] = {
                    "regression": d.get("overall_regression"),
                    "is_valid":   d.get("is_valid"),
                    "error":      d.get("error", ""),
                }
            except Exception:
                pass
    return gt


def run_one(entry, slot_id, out_dir):
    entry_id = f"{entry['id']}_s{entry['sugg_idx']}"
    port = BASE_PORT + slot_id
    work_dir = TMPDIR / f"vistest_{entry_id}_{slot_id}_{os.getpid()}"
    result = {"entry_id": entry_id, "slot_id": slot_id, "status": "error",
              "regression": None, "is_valid": None, "error": None, "elapsed_s": None}

    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. Copy baseline + apply patch
        baseline = BASELINES / entry["id"]
        if not baseline.exists():
            result["error"] = "baseline missing"
            return result
        site_dir = work_dir / "site"
        shutil.copytree(str(baseline), str(site_dir))
        subprocess.run(
            ["git", "-C", str(site_dir), "apply", "--whitespace=fix",
             entry["patch_file"]],
            capture_output=True
        )

        # 2. Start host for patched site
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
            # 3. Run visual regression
            out_json  = out_dir / f"{entry_id}.json"
            slot_json = json.dumps({
                "slot_id": slot.slot_id, "cpuset": slot.cpuset,
                "cpu_count": slot.cpu_count, "memory": slot.memory,
                "queue_wait_ms": 0, "mode": "local",
            })
            t0 = time.time()
            proc = subprocess.run(
                [sys.executable, str(VISUAL_SCRIPT),
                 "--url",           host.url,
                 "--screenshot-path", str(out_dir / f"{entry_id}.png"),
                 "--repo-id",       entry["repo_id"],
                 "--commit-id",     entry["commit_id"],
                 "--framework",     entry["framework"],
                 "--host-file-path",entry.get("host_file_path", ""),
                 "--patch-file",    entry["patch_file"],
                 "--baseline-dir",  str(BASELINES / entry["id"]),
                 "--output-json",   str(out_json),
                 "--slot-json",     slot_json],
                env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
                     "TMPDIR": str(TMPDIR)},
                capture_output=True,
                timeout=TIMEOUT,
            )
            elapsed = round(time.time() - t0, 1)
            result["elapsed_s"] = elapsed

            if out_json.exists():
                try:
                    d = json.load(open(out_json))
                    result.update({
                        "status":     "success",
                        "regression": d.get("overall_regression"),
                        "is_valid":   d.get("is_valid"),
                        "error":      d.get("error", "") or "",
                    })
                except Exception as e:
                    result["error"] = f"parse error: {e}"
            else:
                result["error"] = f"no output json (rc={proc.returncode})"
                if proc.stderr:
                    result["error"] += " | " + proc.stderr.decode()[-200:]
        finally:
            stop_host(pid=host.pid)
    except subprocess.TimeoutExpired:
        result["error"] = f"timeout ({TIMEOUT}s)"
    except Exception as e:
        result["error"] = str(e)
    finally:
        shutil.rmtree(str(work_dir), ignore_errors=True)

    return result


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest = json.load(open(MANIFEST))
    gt = load_ground_truth(manifest)

    log(f"parallel={PARALLEL}, patches={len(manifest)}, timeout={TIMEOUT}s")
    log(f"Ground truth: {sum(1 for v in gt.values() if v['regression'] is True)} regressed, "
        f"{sum(1 for v in gt.values() if v['regression'] is False)} clean, "
        f"{sum(1 for v in gt.values() if v['regression'] is None)} errors")
    log(f"Output: {OUT_DIR}")
    print()

    slot_q = Queue()
    for i in range(PARALLEL):
        slot_q.put(i)

    results = []
    t0 = time.time()

    def task(entry):
        sid = slot_q.get()
        try:
            return run_one(entry, sid, OUT_DIR)
        finally:
            slot_q.put(sid)

    with ThreadPoolExecutor(max_workers=PARALLEL) as exe:
        futs = {exe.submit(task, e): e for e in manifest}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            reg  = r.get("regression")
            reg_s = "REGRESSED" if reg else ("clean" if reg is False else "ERROR")
            err = f"  [{r['error'][:60]}]" if r.get("error") else ""
            print(f"  {r['entry_id']:35s} slot={r['slot_id']:>2}  "
                  f"{reg_s:10s}  {r.get('elapsed_s', '---')}s{err}", flush=True)

    elapsed = round(time.time() - t0, 1)

    # Save summary
    summary = {"parallel": PARALLEL, "elapsed_s": elapsed, "results": results}
    with open(OUT_DIR / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    # ── Analysis ──────────────────────────────────────────────────────────────
    print(f"\n{'='*68}")
    print(f"  VISUAL REGRESSION TEST — P{PARALLEL}, {len(manifest)} patches")
    print(f"{'='*68}")

    n_ok       = sum(1 for r in results if r["status"] == "success")
    n_err      = sum(1 for r in results if r["status"] == "error")
    n_regressed= sum(1 for r in results if r.get("regression") is True)
    n_clean    = sum(1 for r in results if r.get("regression") is False)
    n_vis_err  = sum(1 for r in results if r["status"] == "success" and r.get("regression") is None)

    print(f"\n  TABLE 1 — Overall Results")
    print(f"  +{'─'*28}+{'─'*8}+")
    print(f"  | {'Metric':<26} | {'Count':>6} |")
    print(f"  +{'═'*28}+{'═'*8}+")
    rows = [
        ("Total patches",           len(results)),
        ("Tool completed (no crash)",n_ok),
        ("Tool crashed/timeout",    n_err),
        ("  → Regression detected", n_regressed),
        ("  → Clean (no regression)",n_clean),
        ("  → Tool error (in JSON)",n_vis_err),
    ]
    for label, val in rows:
        print(f"  | {label:<26} | {val:>6} |")
        print(f"  +{'─'*28}+{'─'*8}+")

    # Compare to ground truth
    print(f"\n  TABLE 2 — Agreement with Ground Truth")
    print(f"  (GT from existing dump run; None GT = tool crashed in orig run)")
    print()

    agree = fp = fn = gt_err = new_err = 0
    confusion = {"TP":0,"TN":0,"FP":0,"FN":0,"GT_ERR":0,"NEW_ERR":0,"BOTH_ERR":0}

    for r in results:
        eid = r["entry_id"]
        new_reg = r.get("regression")
        new_crash = r["status"] == "error" or (r["status"] == "success" and new_reg is None and r.get("error"))

        if eid not in gt:
            continue
        gt_reg = gt[eid]["regression"]
        gt_crash = gt_reg is None

        if new_crash and gt_crash:
            confusion["BOTH_ERR"] += 1
        elif new_crash:
            confusion["NEW_ERR"] += 1
            new_err += 1
        elif gt_crash:
            confusion["GT_ERR"] += 1
            gt_err += 1
        elif new_reg == gt_reg:
            if new_reg:
                confusion["TP"] += 1
            else:
                confusion["TN"] += 1
            agree += 1
        elif new_reg and not gt_reg:
            confusion["FP"] += 1
            fp += 1
        elif not new_reg and gt_reg:
            confusion["FN"] += 1
            fn += 1

    print(f"  +{'─'*28}+{'─'*8}+")
    print(f"  | {'Outcome':<26} | {'Count':>6} |")
    print(f"  +{'═'*28}+{'═'*8}+")
    crows = [
        ("TP (both say regressed)",  confusion["TP"]),
        ("TN (both say clean)",      confusion["TN"]),
        ("FP (new=regressed, GT=clean)", confusion["FP"]),
        ("FN (new=clean, GT=regressed)", confusion["FN"]),
        ("GT errored, new succeeded",confusion["GT_ERR"]),
        ("New errored, GT succeeded",confusion["NEW_ERR"]),
        ("Both errored",             confusion["BOTH_ERR"]),
    ]
    for label, val in crows:
        print(f"  | {label:<26} | {val:>6} |")
        print(f"  +{'─'*28}+{'─'*8}+")

    n_comparable = confusion["TP"] + confusion["TN"] + confusion["FP"] + confusion["FN"]
    if n_comparable:
        acc = (confusion["TP"] + confusion["TN"]) / n_comparable * 100
        fpr = confusion["FP"] / max(confusion["TN"] + confusion["FP"], 1) * 100
        print(f"\n  Agreement rate (excl errors): {acc:.1f}%")
        print(f"  False positive rate:          {fpr:.1f}%")
        print(f"  Wall time at P{PARALLEL}:              {elapsed:.0f}s")

    # Error breakdown
    err_types = {}
    for r in results:
        if r.get("error"):
            key = r["error"][:60]
            err_types[key] = err_types.get(key, 0) + 1
    if err_types:
        print(f"\n  TABLE 3 — Error breakdown")
        print(f"  +{'─'*52}+{'─'*6}+")
        for msg, cnt in sorted(err_types.items(), key=lambda x: -x[1])[:10]:
            print(f"  | {msg:<50} | {cnt:>4} |")
            print(f"  +{'─'*52}+{'─'*6}+")

    print(f"{'='*68}\n")


if __name__ == "__main__":
    main()
