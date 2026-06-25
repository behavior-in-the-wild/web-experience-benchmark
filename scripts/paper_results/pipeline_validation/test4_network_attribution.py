#!/usr/bin/env python3
"""
TEST 4 — network-failure attribution (existing data, no fresh measurement).

Question: are CLS regressions explained by broken assets (404s) on the patched
page rather than genuine layout shifts? Using the stored network_summary in every
desktop.json, we compare, per CLS-regressed site, the patched vs baseline
failed_request_count and the composition of external domains (third-party like
maps/fonts/analytics fail in the sandbox regardless of the patch).

Expectation if failures are environmental (not the cause): patched failures are
mostly <= baseline, median delta ~ 0, and failing traffic is dominated by
third-party domains, not same-origin (localhost) assets.

Output: test4_network_attribution.json + console summary.
Usage: PYTHONPATH=src python test4_network_attribution.py
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
from statistics import median
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

BASE = V.ROOT / "final_result_dumps/baselines/cwv_baseline_scores_p20"
DUMP = V.ROOT / "final_result_dumps/main_bench_rerun_20260615"
CLS_THRESH = 0.05
THIRD_PARTY_HINTS = ("google", "gstatic", "fonts", "maps", "analytics", "doubleclick",
                     "facebook", "twitter", "cloudflare", "jsdelivr", "cdn")

def load(p):
    try:
        return json.loads(Path(p).read_text()) if Path(p).exists() and Path(p).stat().st_size > 50 else None
    except Exception:
        return None

def maxfail(j):
    fc = [(r.get("network_summary") or {}).get("failed_request_count")
          for r in (j.get("runs") or [])]
    fc = [x for x in fc if x is not None]
    return max(fc) if fc else None

def ext_domains(j):
    c = Counter()
    for r in (j.get("runs") or []):
        for d in (r.get("network_summary") or {}).get("top_external_domains", []) or []:
            c[d.get("host", "?")] += d.get("count", 0)
    return c

def main():
    deltas = []; patch_introduced = same_or_less = 0
    third_party_dominant = 0; sites = 0
    examples = []
    for md in sorted(DUMP.iterdir()):
        if not md.is_dir():
            continue
        for td in sorted(md.iterdir()):
            if not td.is_dir():
                continue
            sid = td.name.split("_")[0]
            bd = load(BASE / sid / "desktop.json"); pd = load(td / "desktop.json")
            if not bd or not pd:
                continue
            bv = (bd.get("aggregated") or {}).get("CLS_median")
            pv = (pd.get("aggregated") or {}).get("CLS_median")
            if bv is None or pv is None or (pv - bv) <= CLS_THRESH:
                continue
            bf, pf = maxfail(bd), maxfail(pd)
            if bf is None or pf is None:
                continue
            sites += 1
            deltas.append(pf - bf)
            if pf > bf:
                patch_introduced += 1
            else:
                same_or_less += 1
            ed = ext_domains(pd)
            tp = sum(v for k, v in ed.items() if any(h in k.lower() for h in THIRD_PARTY_HINTS))
            allc = sum(ed.values())
            if allc and tp / allc >= 0.5:
                third_party_dominant += 1
            if len(examples) < 6:
                examples.append({"model": md.name, "id": sid, "base_fail": bf,
                                 "patch_fail": pf, "top_ext": ed.most_common(3)})

    print(f"TEST 4 — network-failure attribution on {sites} CLS-regressed sites\n")
    print(f"  patched failures > baseline (patch-introduced): {patch_introduced} ({100*patch_introduced/sites:.0f}%)")
    print(f"  patched failures <= baseline (environmental):   {same_or_less} ({100*same_or_less/sites:.0f}%)")
    print(f"  median(patched_fail - baseline_fail):           {median(deltas):+.0f}")
    print(f"  sites where failing traffic is >=50% third-party domains: {third_party_dominant} ({100*third_party_dominant/sites:.0f}%)")
    print("\n  examples (base_fail -> patch_fail, top external domains):")
    for e in examples:
        print(f"    {e['id']:>5} {e['model'][:22]:<22} {e['base_fail']} -> {e['patch_fail']}  {e['top_ext']}")
    print("\nInterpretation: if most failures are <= baseline and dominated by third-party "
          "domains, 404s are environmental and do not explain the CLS regression.")
    out = {"sites": sites, "patch_introduced": patch_introduced, "same_or_less": same_or_less,
           "median_delta": median(deltas), "third_party_dominant": third_party_dominant,
           "examples": examples}
    Path(os.path.dirname(__file__), "test4_network_attribution.json").write_text(json.dumps(out, indent=2))

if __name__ == "__main__":
    main()
