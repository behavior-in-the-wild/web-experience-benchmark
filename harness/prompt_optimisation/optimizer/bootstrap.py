"""
Bootstrap phase: run the unmodified template on all training repos,
collect successful traces as demo candidates, build 8 fixed demo sets.

Demo sets are pre-built here (before the Optuna loop) so that:
  - demo_idx in Optuna refers to a stable index
  - no re-sampling during trials
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pandas as pd

from harness.prompt_optimisation.bridge import parser as result_parser
from harness.prompt_optimisation.bridge import runner
from harness.prompt_optimisation.data.build_demo_pool import build as build_pool
from harness.prompt_optimisation.optimizer.metric import compute
from harness.prompt_optimisation.prompts.schema import DemoExample

_BOOTSTRAP_THRESHOLD = 0.10
_N_DEMO_SETS = 8
_SEED = 42


def run_bootstrap(
    train_rows: list[dict],
    run_dir: Path,
    parallel: int = 4,
    num_runs: int = 3,
) -> tuple[Path, list[list[DemoExample]]]:
    """
    1. Run baseline harness on all training repos.
    2. Collect demos from results.
    3. Build 8 fixed demo sets.

    Returns:
        results_dir  — path to baseline harness results
        demo_sets    — list of 8 demo sets (each a list[DemoExample])
    """
    bootstrap_dir = run_dir / "bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)

    print(f"[bootstrap] Running baseline on {len(train_rows)} repos …")
    results_dir = runner.run_baseline(
        rows=train_rows,
        out_dir=bootstrap_dir,
        parallel=parallel,
        num_runs=num_runs,
    )

    # Mine results for demos (appends to run-local pool)
    pool_path = run_dir / "demo_pool.jsonl"
    build_pool(results_dir=results_dir, demo_pool=pool_path, verbose=True)

    demos = _load_demos(pool_path)
    print(f"[bootstrap] {len(demos)} demos qualified (score > {_BOOTSTRAP_THRESHOLD})")

    demo_sets = _build_demo_sets(demos, run_dir)
    return results_dir, demo_sets


def _load_demos(pool_path: Path) -> list[DemoExample]:
    demos = []
    if not pool_path.exists():
        return demos
    for line in pool_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            demos.append(DemoExample(**json.loads(line)))
        except Exception:
            pass
    return demos


def _build_demo_sets(demos: list[DemoExample], run_dir: Path) -> list[list[DemoExample]]:
    """
    Build 8 fixed demo sets of varying sizes and compositions:
      0,1 — 3 demos, framework-diverse
      2,3,4 — 4 demos, mixed quality (top LCP + medium)
      5,6,7 — 5 demos, high-volume highest-delta

    Falls back gracefully when the pool is smaller than expected.
    """
    rng = random.Random(_SEED)

    # Sort by LCP delta desc
    sorted_demos = sorted(demos, key=lambda d: d.lcp_delta_pct, reverse=True)

    # Framework-diverse subsets (pick best from each framework bucket)
    by_fw: dict[str, list[DemoExample]] = {}
    for d in sorted_demos:
        by_fw.setdefault(d.framework, []).append(d)

    def diverse_pick(n: int) -> list[DemoExample]:
        """Pick n demos ensuring framework diversity."""
        picked: list[DemoExample] = []
        fws = list(by_fw.keys())
        rng.shuffle(fws)
        for fw in fws:
            if len(picked) >= n:
                break
            if by_fw[fw]:
                picked.append(by_fw[fw][0])
        # Fill remainder from top sorted
        remaining = [d for d in sorted_demos if d not in picked]
        while len(picked) < n and remaining:
            picked.append(remaining.pop(0))
        return picked[:n]

    def top_pick(n: int, offset: int = 0) -> list[DemoExample]:
        return sorted_demos[offset : offset + n] if len(sorted_demos) >= offset + n else sorted_demos[:n]

    def mixed_pick(n: int) -> list[DemoExample]:
        top = sorted_demos[: max(1, len(sorted_demos) // 3)]
        mid = sorted_demos[len(top) : max(len(top) + 1, len(sorted_demos) * 2 // 3)]
        pool = top + mid
        if not pool:
            return sorted_demos[:n]
        return rng.sample(pool, min(n, len(pool)))

    sets: list[list[DemoExample]] = [
        diverse_pick(3),
        diverse_pick(3),
        mixed_pick(4),
        mixed_pick(4),
        mixed_pick(4),
        top_pick(5),
        top_pick(5, offset=2),
        top_pick(5, offset=4),
    ]

    # Pad with empty lists if demos pool was empty
    while len(sets) < _N_DEMO_SETS:
        sets.append([])

    # Persist to disk so search.py can reload without re-running bootstrap
    demo_sets_path = run_dir / "demo_sets.jsonl"
    with demo_sets_path.open("w") as f:
        for demo_set in sets:
            f.write(json.dumps([d.model_dump() for d in demo_set]) + "\n")
    print(f"[bootstrap] Saved {len(sets)} demo sets → {demo_sets_path}")

    return sets


def load_demo_sets(run_dir: Path) -> list[list[DemoExample]]:
    """Reload demo sets saved by run_bootstrap."""
    path = run_dir / "demo_sets.jsonl"
    sets = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        sets.append([DemoExample(**d) for d in raw])
    return sets
