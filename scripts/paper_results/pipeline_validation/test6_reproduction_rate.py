#!/usr/bin/env python3
"""
TEST 6 — scaled CLS-regression reproduction rate.

Quantifies what fraction of recorded desktop CLS regressions reproduce under a
clean, isolated re-measurement. For each (model, site) with a recorded regression
(patched CLS - baseline CLS > THRESH), we reconstruct BOTH the unpatched baseline
and the patched site (clone @ COMMIT_ID + git apply), host each, and measure CLS
in isolation (settle 5000ms). A regression "reproduces" if the freshly measured
patched-minus-baseline CLS still exceeds THRESH.

Models default to a mix of a CLS-careful and a CLS-reckless model so the rate is
not specific to one agent. Static HTML only (clean reconstruction + plain host).

Output: test6_reproduction_rate.json + console summary.
Usage:
  PYTHONPATH=src python test6_reproduction_rate.py [K_PER_MODEL]
Env:
  VAL_MODELS="closed_cc-opus-4.6,closed_oc_gpt-5.1-codex"
  VAL_NUM_RUNS_REPRO=3
"""
from __future__ import annotations
import json, os, sys, shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

DUMP = V.ROOT / "final_result_dumps/main_bench_rerun_20260615"
BASE = V.ROOT / "final_result_dumps/baselines/cwv_baseline_scores_p20"
CLS_THRESH = 0.05
K = int(sys.argv[1]) if len(sys.argv) > 1 else 15
MODELS = os.environ.get("VAL_MODELS",
                        "closed_cc-opus-4.6,closed_oc_gpt-5.1-codex").split(",")
NUM_RUNS = int(os.environ.get("VAL_NUM_RUNS_REPRO", "3"))

def load(p):
    try:
        return json.loads(Path(p).read_text()) if Path(p).exists() and Path(p).stat().st_size > 50 else None
    except Exception:
        return None

def find_patch(task_dir: Path) -> Path | None:
    cands = sorted(task_dir.glob("*.patch"))
    cands = [c for c in cands if c.stat().st_size > 0]
    return cands[0] if cands else None

def select(model: str, rows: dict, k: int) -> list[dict]:
    mdir = DUMP / model
    out = []
    if not mdir.is_dir():
        return out
    for td in sorted(mdir.iterdir()):
        if not td.is_dir():
            continue
        sid = td.name.split("_")[0]
        row = rows.get(sid)
        if not row or row["FRAMEWORK"].strip() != "Static HTML":
            continue
        patch = find_patch(td)
        if not patch:
            continue
        bd = load(BASE / sid / "desktop.json"); pd = load(td / "desktop.json")
        if not bd or not pd:
            continue
        bcls = (bd.get("aggregated") or {}).get("CLS_median")
        pcls = (pd.get("aggregated") or {}).get("CLS_median")
        if bcls is None or pcls is None or (pcls - bcls) <= CLS_THRESH:
            continue
        out.append({"model": model, "id": sid, "repo_id": row["REPO_ID"].strip(),
                    "commit": (row.get("COMMIT_ID") or "").strip(),
                    "framework": row["FRAMEWORK"].strip(),
                    "host_file": row.get("HOST_FILE_PATH") or None,
                    "patch": str(patch),
                    "recorded_baseline_cls": bcls, "recorded_patched_cls": pcls})
        if len(out) >= k:
            break
    return out

def measure_variant(site, patched: bool) -> float | None:
    patch = Path(site["patch"]) if patched else None
    d = V.reconstruct_site(site, patch_file=patch)
    try:
        with V.HostedSite(d, site["framework"], site["host_file"]) as h:
            m = V.measure(h.url, device="desktop", num_runs=NUM_RUNS, settle_time=5000)
        return m["aggregated"].get("CLS_median")
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main():
    rows = V.load_rows()
    targets = []
    for m in MODELS:
        sel = select(m, rows, K)
        print(f"selected {len(sel)} CLS-regressed Static-HTML sites for {m}")
        targets += sel

    out_path = Path(os.path.dirname(__file__), "test6_reproduction_rate.json")
    # Resume: load any prior results and skip (model,id) already measured.
    results = []
    done = set()
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text()).get("results", [])
            results = [r for r in prev if "error" not in r]
            done = {(r["model"], r["id"]) for r in results}
            print(f"resume: {len(done)} sites already measured, skipping them")
        except Exception:
            pass

    def write():
        per_model = {}
        for r in results:
            if "reproduces" not in r:
                continue
            pm = per_model.setdefault(r["model"], {"n": 0, "repro": 0})
            pm["n"] += 1; pm["repro"] += int(r["reproduces"])
        out_path.write_text(json.dumps({"per_model": per_model, "results": results}, indent=2))
        return per_model

    print(f"\nTEST 6 — reproduction (num_runs={NUM_RUNS}); recon CLS in isolation\n")
    print(f"{'model':<22}{'id':>6} {'rec_base':>9} {'rec_patch':>10} {'recon_d':>9} {'reproduces':>11}")
    for s in targets:
        if (s["model"], s["id"]) in done:
            continue
        try:
            rb = measure_variant(s, patched=False)
            rp = measure_variant(s, patched=True)
            d = (rp - rb) if None not in (rb, rp) else float("nan")
            repro = (d == d) and d > CLS_THRESH
            results.append({**{k: s[k] for k in ("model","id","repo_id",
                            "recorded_baseline_cls","recorded_patched_cls")},
                            "recon_baseline_cls": rb, "recon_patched_cls": rp,
                            "recon_delta": d, "reproduces": bool(repro)})
            print(f"{s['model']:<22}{s['id']:>6} {rb!s:>9} {rp!s:>10} {d:>9.3f} {str(repro):>11}")
            write()   # incremental: survive interruption
        except Exception as e:
            results.append({"model": s["model"], "id": s["id"], "error": str(e)})
            print(f"{s['model']:<22}{s['id']:>6} ERROR: {e}")
            write()

    per_model = write()
    print("\n=== REPRODUCTION RATE ===")
    tot_n = tot_r = 0
    for m, pm in per_model.items():
        tot_n += pm["n"]; tot_r += pm["repro"]
        print(f"  {m:<24} {pm['repro']}/{pm['n']} reproduce ({100*pm['repro']/pm['n']:.0f}%)")
    if tot_n:
        print(f"  {'OVERALL':<24} {tot_r}/{tot_n} reproduce ({100*tot_r/tot_n:.0f}%)")
    print("\nInterpretation: a low reproduction rate means recorded CLS regressions are "
          "largely contention/timing artifacts, not deterministic patch behaviour.")

if __name__ == "__main__":
    main()
