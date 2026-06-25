#!/usr/bin/env python3
"""
TEST 3 — simulate_interaction toggle.

Question: does the synthetic interaction the tool performs for INP (a click/scroll)
itself generate the CLS we attribute to the patch? For each sample site we
reconstruct BOTH baseline and patched, host each once, and measure CLS with
simulate_interaction = True vs False (settle fixed at 5000ms).

Expectation if the CLS is a real load-time shift: patched CLS stays above baseline
in BOTH interaction modes. If patched CLS only regresses with interaction on, the
injected interaction is the cause, not the patch.

Output: test3_interaction_toggle.json + console table.
Usage: PYTHONPATH=src python test3_interaction_toggle.py
"""
from __future__ import annotations
import json, os, sys, shutil
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

NUM_RUNS = int(os.environ.get("VAL_NUM_RUNS_INT", "3"))
DEVICE = "desktop"

def both_modes(site, patched: bool) -> dict:
    patch = Path(site["patch"]) if patched else None
    d = V.reconstruct_site(site, patch_file=patch)
    try:
        with V.HostedSite(d, site["framework"], site["host_file"]) as h:
            on = V.measure(h.url, device=DEVICE, num_runs=NUM_RUNS, settle_time=5000,
                           simulate_interaction=True)
            off = V.measure(h.url, device=DEVICE, num_runs=NUM_RUNS, settle_time=5000,
                            simulate_interaction=False)
            return {"interaction_on": on["aggregated"].get("CLS_median"),
                    "interaction_off": off["aggregated"].get("CLS_median")}
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main():
    sample = json.loads((Path(os.path.dirname(__file__)) / "sample.json").read_text())
    results = []
    print(f"TEST 3 — interaction toggle (num_runs={NUM_RUNS}); CLS_median\n")
    print(f"{'id':>5}  {'variant':<8} {'int_on':>8} {'int_off':>8}")
    for site in sample:
        try:
            base = both_modes(site, patched=False)
            patch = both_modes(site, patched=True)
            results.append({"id": site["id"], "repo": site["repo_id"],
                            "baseline": base, "patched": patch})
            print(f"{site['id']:>5}  {'baseline':<8} {base['interaction_on']!s:>8} {base['interaction_off']!s:>8}")
            print(f"{'':>5}  {'patched':<8} {patch['interaction_on']!s:>8} {patch['interaction_off']!s:>8}")
        except Exception as e:
            print(f"{site['id']:>5}  ERROR: {e}")
            results.append({"id": site["id"], "repo": site["repo_id"], "error": str(e)})

    persists = 0; total = 0
    for r in results:
        if "baseline" not in r:
            continue
        total += 1
        bp = r["patched"]["interaction_off"]; bb = r["baseline"]["interaction_off"]
        if None not in (bp, bb) and bp == bp and bb == bb and (bp - bb) > 0.02:
            persists += 1
    print(f"\nSUMMARY: patched CLS regression persists with interaction OFF on {persists}/{total} sites.")
    print("Interpretation: high count => the shift is a real load-time layout shift, not an "
          "artifact of the synthetic INP interaction." if persists >= max(1, total // 2) else
          "WARNING: regression largely disappears without interaction — the injected interaction is implicated.")
    Path(os.path.dirname(__file__), "test3_interaction_toggle.json").write_text(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
