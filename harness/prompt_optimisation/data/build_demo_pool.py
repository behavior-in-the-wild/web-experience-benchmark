"""
Mine a past harness results directory for successful (plan, patch, delta) tuples.
Appends qualifying demos to data/demo_pool.jsonl.

A demo qualifies when:
  - lcp_delta_pct > threshold  (default 10%)
  - no visual regression
  - patch length < 300 lines
  - plan.md exists and is non-empty
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from harness.prompt_optimisation.bridge.parser import AGENT_NAME, parse_result
from harness.prompt_optimisation.optimizer.metric import compute

ROOT = Path(__file__).parent.parent.parent
DEMO_POOL = Path(__file__).parent / "demo_pool.jsonl"
INPUT_CSV = ROOT / "harness" / "SAMPLE" / "input.csv"

_MAX_PLAN_LINES = 60
_MAX_PATCH_LINES = 80
_LCP_THRESHOLD_PCT = 10.0
_MAX_PATCH_LINES_RAW = 300


def _truncate(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines)"


def _baseline_summary(row: dict) -> str:
    try:
        m = json.loads(row.get("CWV_MOBILE") or "{}")
        return (
            f"LCP={m.get('LCP_mean', '?')}ms "
            f"INP={m.get('INP_mean', '?')}ms "
            f"CLS={m.get('CLS_mean', '?')}"
        )
    except Exception:
        return str(row.get("CWV_MOBILE", ""))[:120]


def build(
    results_dir: Path,
    input_csv: Path = INPUT_CSV,
    demo_pool: Path = DEMO_POOL,
    lcp_threshold_pct: float = _LCP_THRESHOLD_PCT,
    verbose: bool = True,
) -> int:
    """
    Scan results_dir for qualified demos. Appends to demo_pool.
    Returns number of demos added.
    """
    df = pd.read_csv(input_csv)
    id_to_row = {str(r["ID"]): r for _, r in df.iterrows()}

    added = 0
    prefix_to_id = {}
    for f in results_dir.glob(f"*_{AGENT_NAME}_mobile.json"):
        row_id = f.name.split(f"_{AGENT_NAME}_")[0]
        prefix_to_id[row_id] = row_id

    demo_pool.parent.mkdir(parents=True, exist_ok=True)
    with demo_pool.open("a") as out:
        for row_id in prefix_to_id:
            csv_row = id_to_row.get(row_id)
            if csv_row is None:
                continue

            repo_id = str(csv_row["REPO_ID"])
            cwv_result = parse_result(results_dir, repo_id, row_id)

            if not cwv_result.valid:
                continue
            if cwv_result.regression:
                continue

            score = compute(cwv_result)
            if score <= 0:
                continue

            lcp_delta = 0.0
            if cwv_result.baseline_lcp > 0 and cwv_result.lcp_mean is not None:
                lcp_delta = (cwv_result.baseline_lcp - cwv_result.lcp_mean) / cwv_result.baseline_lcp * 100

            if lcp_delta < lcp_threshold_pct:
                continue

            patch_file = results_dir / f"{row_id}_{AGENT_NAME}.patch"
            plan_file = results_dir / f"{row_id}_{AGENT_NAME}_plan.md"

            if not patch_file.exists() or not plan_file.exists():
                continue

            patch_text = patch_file.read_text(errors="replace")
            if len(patch_text.splitlines()) > _MAX_PATCH_LINES_RAW:
                continue

            plan_text = plan_file.read_text(errors="replace").strip()
            if not plan_text:
                continue

            inp_delta = 0.0
            cls_delta = 0.0
            if cwv_result.baseline_inp > 0 and cwv_result.inp_mean is not None:
                inp_delta = (cwv_result.baseline_inp - cwv_result.inp_mean) / cwv_result.baseline_inp * 100
            if cwv_result.baseline_cls > 0 and cwv_result.cls_mean is not None:
                cls_delta = (cwv_result.baseline_cls - cwv_result.cls_mean) / cwv_result.baseline_cls * 100

            demo = {
                "repo_id": repo_id,
                "framework": str(csv_row.get("FRAMEWORK", "unknown")).lower().strip(),
                "baseline_summary": _baseline_summary(dict(csv_row)),
                "plan_excerpt": _truncate(plan_text, _MAX_PLAN_LINES),
                "patch_excerpt": _truncate(patch_text, _MAX_PATCH_LINES),
                "lcp_delta_pct": round(lcp_delta, 2),
                "inp_delta_pct": round(inp_delta, 2),
                "cls_delta_pct": round(cls_delta, 2),
            }
            out.write(json.dumps(demo) + "\n")
            added += 1
            if verbose:
                print(f"  + demo {repo_id}  LCP {lcp_delta:+.1f}%  score={score:.3f}")

    print(f"Added {added} demos → {demo_pool}")
    return added


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python build_demo_pool.py <results_dir>")
        sys.exit(1)
    build(Path(sys.argv[1]))
