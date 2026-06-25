#!/usr/bin/env python3
"""
TEST 1 — A/A null test (pipeline reproducibility).

Question: does the measurement pipeline manufacture CLS/LCP differences when the
content is identical? For each sample site we reconstruct the UNPATCHED baseline
TWICE through two fully independent clone -> host -> measure cycles (fresh temp
dir, fresh port, fresh server each time) and compare.

Expectation if the pipeline is sound: CLS delta ~ 0 and small LCP delta. A large
spurious CLS between two identical-content runs would implicate the pipeline.

Output: test1_aa_null.json + console table.
Usage: PYTHONPATH=src python test1_aa_null.py
"""
from __future__ import annotations
import json, os, sys, shutil, math
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

NUM_RUNS = int(os.environ.get("VAL_NUM_RUNS", "5"))
DEVICE = "desktop"

def measure_once(site) -> dict:
    d = V.reconstruct_site(site)               # baseline: no patch
    try:
        with V.HostedSite(d, site["framework"], site["host_file"]) as h:
            out = V.measure(h.url, device=DEVICE, num_runs=NUM_RUNS, settle_time=5000)
        agg = out["aggregated"]
        return {"cls": agg.get("CLS_median"), "lcp": agg.get("LCP_p75"),
                "cls_runs": [r.get("CLS") for r in out["runs"]]}
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main():
    sample = json.loads((Path(os.path.dirname(__file__)) / "sample.json").read_text())
    results = []
    print(f"TEST 1 — A/A null (baseline measured twice, num_runs={NUM_RUNS})\n")
    print(f"{'id':>5}  {'repo':<34} {'CLS_A':>7} {'CLS_B':>7} {'dCLS':>7}  {'LCP_A':>7} {'LCP_B':>7} {'dLCP':>7}")
    for site in sample:
        try:
            a = measure_once(site)
            b = measure_once(site)
            dcls = (a["cls"] - b["cls"]) if None not in (a["cls"], b["cls"]) else float("nan")
            dlcp = (a["lcp"] - b["lcp"]) if None not in (a["lcp"], b["lcp"]) else float("nan")
            rec = {"id": site["id"], "repo": site["repo_id"], "A": a, "B": b,
                   "abs_dcls": abs(dcls), "abs_dlcp": abs(dlcp)}
            results.append(rec)
            print(f"{site['id']:>5}  {site['repo_id']:<34} {a['cls']!s:>7} {b['cls']!s:>7} "
                  f"{dcls:>7.3f}  {a['lcp']!s:>7} {b['lcp']!s:>7} {dlcp:>7.1f}")
        except Exception as e:
            print(f"{site['id']:>5}  {site['repo_id']:<34} ERROR: {e}")
            results.append({"id": site["id"], "repo": site["repo_id"], "error": str(e)})

    ok = [r for r in results if "abs_dcls" in r and not math.isnan(r["abs_dcls"])]
    if ok:
        max_dcls = max(r["abs_dcls"] for r in ok)
        print(f"\nSUMMARY: max |CLS_A - CLS_B| across {len(ok)} sites = {max_dcls:.3f}")
        print("Interpretation: values near 0 mean the pipeline reproduces identical-content "
              "CLS; the recorded patched CLS regressions (0.1-1.0) are far larger than this noise floor."
              if max_dcls < 0.05 else
              "WARNING: non-trivial A/A CLS variance — pipeline reproducibility is suspect.")
    Path(os.path.dirname(__file__), "test1_aa_null.json").write_text(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
