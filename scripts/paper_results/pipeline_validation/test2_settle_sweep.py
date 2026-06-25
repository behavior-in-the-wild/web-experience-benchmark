#!/usr/bin/env python3
"""
TEST 2 — settle-window sweep.

Question: is the CLS regression an artifact of the 5000ms settle window (e.g. the
window happens to catch a late shift on the patched page)? For each sample site we
reconstruct BOTH the unpatched baseline and the Opus-patched version, host each
once, and measure CLS/LCP at settle_time in {3000, 5000, 10000} ms.

Expectation if the regression is real page behaviour: patched CLS stays clearly
above baseline CLS at every settle window (a genuine layout shift doesn't vanish
when you observe longer). If patched CLS only exceeds baseline at one window, the
window is implicated.

Output: test2_settle_sweep.json + console table.
Usage: PYTHONPATH=src python test2_settle_sweep.py
"""
from __future__ import annotations
import json, os, sys, shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

SETTLES = [int(x) for x in os.environ.get("VAL_SETTLES", "3000,5000,10000").split(",")]
NUM_RUNS = int(os.environ.get("VAL_NUM_RUNS_SWEEP", "3"))
DEVICE = "desktop"

def sweep(site, patched: bool) -> dict:
    patch = Path(site["patch"]) if patched else None
    d = V.reconstruct_site(site, patch_file=patch)
    try:
        with V.HostedSite(d, site["framework"], site["host_file"]) as h:
            out = {}
            for s in SETTLES:
                m = V.measure(h.url, device=DEVICE, num_runs=NUM_RUNS, settle_time=s)
                out[s] = {"cls": m["aggregated"].get("CLS_median"),
                          "lcp": m["aggregated"].get("LCP_p75")}
            return out
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main():
    sample = json.loads((Path(os.path.dirname(__file__)) / "sample.json").read_text())
    results = []
    print(f"TEST 2 — settle sweep (settles={SETTLES}ms, num_runs={NUM_RUNS}); CLS_median per settle\n")
    hdr = "  ".join(f"s{s//1000}k" for s in SETTLES)
    print(f"{'id':>5}  {'variant':<8} {hdr}")
    for site in sample:
        try:
            base = sweep(site, patched=False)
            patch = sweep(site, patched=True)
            results.append({"id": site["id"], "repo": site["repo_id"],
                            "baseline": base, "patched": patch})
            bvals = "  ".join(f"{base[s]['cls']!s:>4}" for s in SETTLES)
            pvals = "  ".join(f"{patch[s]['cls']!s:>4}" for s in SETTLES)
            print(f"{site['id']:>5}  {'baseline':<8} {bvals}")
            print(f"{'':>5}  {'patched':<8} {pvals}")
        except Exception as e:
            print(f"{site['id']:>5}  ERROR: {e}")
            results.append({"id": site["id"], "repo": site["repo_id"], "error": str(e)})

    # stability check: patched > baseline at ALL settle windows?
    stable = 0; total = 0
    for r in results:
        if "baseline" not in r:
            continue
        total += 1
        gaps = []
        for s in SETTLES:
            bc, pc = r["baseline"][s]["cls"], r["patched"][s]["cls"]
            if bc is None or pc is None or pc != pc:
                gaps.append(None)
            else:
                gaps.append(pc - bc)
        if all(g is not None and g > 0.02 for g in gaps):
            stable += 1
    print(f"\nSUMMARY: patched CLS exceeds baseline at EVERY settle window on {stable}/{total} sites.")
    print("Interpretation: high count => the regression is real page instability, not a "
          "settle-window artifact." if stable >= max(1, total // 2) else
          "WARNING: regression appears window-dependent on several sites — investigate timing.")
    Path(os.path.dirname(__file__), "test2_settle_sweep.json").write_text(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
