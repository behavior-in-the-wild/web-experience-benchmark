#!/usr/bin/env python3
"""
TEST 5 — visual-regression pipeline internal consistency (existing data).

Question: is the 4-signal visual regression checker self-consistent, or is it a
single noisy signal dressed up as four? We measure, across every patch:
  - per-signal valid-rate and positive-rate
  - pairwise agreement between signals when BOTH return a boolean
  - the CLS-regression vs visual-regression contingency (independence/corroboration)

A checker whose signals agree well above chance, and whose visual flags correlate
(but are not identical) with measured CLS, is behaving sensibly: CLS captures the
shift journey, the visual checker captures the final-state difference, and they
should overlap partially, not perfectly.

Output: test5_regression_agreement.json + console summary.
Usage: PYTHONPATH=src python test5_regression_agreement.py
"""
from __future__ import annotations
import json, os, sys, itertools
from pathlib import Path
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

BASE = V.ROOT / "final_result_dumps/baselines/cwv_baseline_scores_p20"
DUMP = V.ROOT / "final_result_dumps/main_bench_rerun_20260615"
SIGNALS = ["structural", "jaccard_text", "gpt_visual", "console_errors"]

def load(p):
    try:
        return json.loads(Path(p).read_text()) if Path(p).exists() and Path(p).stat().st_size > 20 else None
    except Exception:
        return None

def signal_vals(vj):
    ch = vj.get("checks", {})
    return {s: ch.get(s, {}).get("regression") for s in SIGNALS}

def main():
    valid = Counter(); positive = Counter()
    pair_agree = Counter(); pair_total = Counter()
    n_valid_hist = Counter()
    # CLS vs visual contingency
    ct = Counter()  # (cls_reg, vis_reg)
    total = 0
    for md in sorted(DUMP.iterdir()):
        if not md.is_dir():
            continue
        for td in sorted(md.iterdir()):
            if not td.is_dir():
                continue
            vj = load(td / "visual.json")
            if not vj:
                continue
            total += 1
            sv = signal_vals(vj)
            valids = {s: v for s, v in sv.items() if v is not None}
            n_valid_hist[len(valids)] += 1
            for s, v in valids.items():
                valid[s] += 1
                if v:
                    positive[s] += 1
            for a, b in itertools.combinations(SIGNALS, 2):
                if sv[a] is not None and sv[b] is not None:
                    pair_total[(a, b)] += 1
                    if sv[a] == sv[b]:
                        pair_agree[(a, b)] += 1
            # contingency vs CLS (desktop)
            sid = td.name.split("_")[0]
            bd = load(BASE / sid / "desktop.json"); pd = load(td / "desktop.json")
            visreg = bool(vj.get("overall_regression"))
            if bd and pd:
                bv = (bd.get("aggregated") or {}).get("CLS_median")
                pv = (pd.get("aggregated") or {}).get("CLS_median")
                if bv is not None and pv is not None:
                    clsreg = (pv - bv) > 0.05
                    ct[(clsreg, visreg)] += 1

    print(f"TEST 5 — visual-regression checker consistency over {total} patches\n")
    print("Per-signal valid-rate / positive-rate (positive = flagged regression):")
    for s in SIGNALS:
        vrate = 100 * valid[s] / total if total else 0
        prate = 100 * positive[s] / valid[s] if valid[s] else 0
        print(f"  {s:<15} valid {vrate:5.1f}%   positive {prate:5.1f}% (of valid)")
    print(f"\nn_valid signals per patch: {dict(sorted(n_valid_hist.items()))}")
    print("\nPairwise agreement when both signals valid:")
    for a, b in itertools.combinations(SIGNALS, 2):
        t = pair_total[(a, b)]
        if t:
            print(f"  {a:<13} ~ {b:<13} {100*pair_agree[(a,b)]/t:5.1f}%  (n={t})")

    # contingency
    n = sum(ct.values())
    print(f"\nCLS-regression vs visual-regression contingency (n={n}):")
    print(f"  CLS+ Vis+ : {ct[(True,True)]:>4}   CLS+ Vis- : {ct[(True,False)]:>4}")
    print(f"  CLS- Vis+ : {ct[(False,True)]:>4}   CLS- Vis- : {ct[(False,False)]:>4}")
    if n:
        # phi coefficient
        a, b = ct[(True, True)], ct[(True, False)]
        c, d = ct[(False, True)], ct[(False, False)]
        import math
        denom = math.sqrt((a+b)*(c+d)*(a+c)*(b+d)) or 1
        phi = (a*d - b*c) / denom
        print(f"  phi (CLS vs visual) = {phi:+.3f}  "
              f"(small-positive expected: partial overlap, not identity)")
    out = {"total": total,
           "valid": dict(valid), "positive": dict(positive),
           "pair_agree": {f"{a}~{b}": [pair_agree[(a,b)], pair_total[(a,b)]]
                          for a, b in itertools.combinations(SIGNALS, 2)},
           "n_valid_hist": dict(n_valid_hist),
           "contingency": {f"CLS{int(k[0])}_VIS{int(k[1])}": v for k, v in ct.items()}}
    Path(os.path.dirname(__file__), "test5_regression_agreement.json").write_text(json.dumps(out, indent=2))

if __name__ == "__main__":
    main()
