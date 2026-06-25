#!/usr/bin/env python3
"""
Select the validation sample: Static-HTML sites that show a recorded desktop
CLS regression under Opus 4.6 (baseline CLS - patched CLS such that patched is
worse by > THRESH). Static HTML is chosen so the patched site reconstructs
cleanly (clone @ commit + git apply) and hosts with a plain HTTP server, keeping
the validation about the *measurement*, not about framework build flakiness.

Writes sample.json consumed by tests 01-03.

Usage: PYTHONPATH=src python 00_select_sample.py [N]
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

MODEL_DIR = V.ROOT / "final_result_dumps/main_bench_rerun_20260615/closed_cc-opus-4.6"
BASE_DIR = V.ROOT / "final_result_dumps/baselines/cwv_baseline_scores_p20"
CLS_THRESH = 0.05
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5

def safe_load(p):
    try:
        return json.loads(Path(p).read_text()) if Path(p).exists() else None
    except Exception:
        return None

def main():
    rows = V.load_rows()
    picked = []
    for task_dir in sorted(MODEL_DIR.iterdir()):
        if not task_dir.is_dir():
            continue
        sid = task_dir.name.split("_")[0]
        row = rows.get(sid)
        if not row or row["FRAMEWORK"].strip() != "Static HTML":
            continue
        patch = task_dir / f"{task_dir.name}.patch"
        if not patch.exists() or patch.stat().st_size == 0:
            continue
        bd = safe_load(BASE_DIR / sid / "desktop.json")
        pd = safe_load(task_dir / "desktop.json")
        if not bd or not pd:
            continue
        bcls = (bd.get("aggregated") or {}).get("CLS_median")
        pcls = (pd.get("aggregated") or {}).get("CLS_median")
        if bcls is None or pcls is None or (pcls - bcls) <= CLS_THRESH:
            continue
        picked.append({
            "id": sid,
            "repo_id": row["REPO_ID"].strip(),
            "commit": (row.get("COMMIT_ID") or "").strip(),
            "framework": row["FRAMEWORK"].strip(),
            "host_file": row.get("HOST_FILE_PATH") or None,
            "patch": str(patch),
            "recorded_baseline_cls": bcls,
            "recorded_patched_cls": pcls,
        })
        if len(picked) >= N:
            break

    out = Path(os.path.dirname(__file__)) / "sample.json"
    out.write_text(json.dumps(picked, indent=2))
    print(f"Selected {len(picked)} sites -> {out}")
    for p in picked:
        print(f"  {p['id']:>5}  {p['repo_id']:<40} recorded CLS {p['recorded_baseline_cls']:.3f} -> {p['recorded_patched_cls']:.3f}")

if __name__ == "__main__":
    main()
