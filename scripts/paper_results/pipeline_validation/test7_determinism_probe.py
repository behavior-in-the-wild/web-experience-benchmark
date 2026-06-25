#!/usr/bin/env python3
"""TEST 7 — determinism probe: re-measure patched sites with per-run detail to
see (a) whether CLS varies run-to-run NOW, and (b) what external content loads.
Distinguishes volatile-external sites from fully-local ones."""
import json, os, sys, shutil
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
import _val_lib as V

PROBE = {"1294": "ad/live-origin", "1465": "ad", "136": "fully-local", "17": "fully-local"}
MODEL = "closed_cc-opus-4.6"
DUMP = V.ROOT / "final_result_dumps/main_bench_rerun_20260615" / MODEL

def patch_for(sid):
    cand = list(DUMP.glob(f"{sid}_*/*.patch"))
    return cand[0] if cand else None

def main():
    rows = V.load_rows()
    for sid, kind in PROBE.items():
        row = rows.get(sid)
        site = {"repo_id": row["REPO_ID"].strip(), "commit": (row.get("COMMIT_ID") or "").strip(),
                "framework": row["FRAMEWORK"].strip(), "host_file": row.get("HOST_FILE_PATH")}
        try:
            d = V.reconstruct_site(site, patch_file=patch_for(sid))
            with V.HostedSite(d, site["framework"], site["host_file"]) as h:
                out = V.measure(h.url, device="desktop", num_runs=5, settle_time=5000)
            runs = out["runs"]
            cls = [round(r.get("CLS", 0), 3) for r in runs]
            ext = [ (r.get("network_summary") or {}).get("third_party_request_count", 0) for r in runs ]
            fail = [ (r.get("network_summary") or {}).get("failed_request_count", 0) for r in runs ]
            print(f"{sid:>5} [{kind:<14}] per-run CLS={cls}  3rdparty={ext}  failed={fail}")
            shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            print(f"{sid:>5} [{kind:<14}] ERROR: {e}")

if __name__ == "__main__":
    main()
