#!/usr/bin/env python3
"""
Shared helpers for the CWV pipeline-validation suite.

This module does NOT reimplement hosting or measurement. It reuses the exact
production primitives:

  - cwv_tool.cwv_benchmark.git_clone        (clone + checkout COMMIT_ID)
  - docker_tool.hosting.start_host/stop_host (host any framework, local or docker)
  - cwv_tool.performance_testing.measure_cwv_metrics
        (one measurement run, with explicit settle_time / simulate_interaction)

The only thing the validation scripts add is orchestration: reconstruct a site
from its input_100.csv row, optionally apply a model patch, host it, and take
controlled measurements while varying ONE knob at a time (repeat / settle /
interaction). That is what lets us attribute the CLS signal to the page rather
than to the pipeline.

Env (set by run_all.sh):
  PYTHONPATH=src
  TMPDIR, WEB_BENCH_REPO_CACHE      -> /dev/shm scratch
  VAL_HOST_MODE=local|auto|docker   -> hosting backend (default local)
  CWV_SANDBOX=0                      -> disable slot scheduling for single-site runs
"""
from __future__ import annotations

import asyncio
import csv
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

# Reused production code -----------------------------------------------------
from cwv_tool.cwv_benchmark import git_clone, find_available_port
from cwv_tool.performance_testing import (
    measure_cwv_metrics,
    calculate_aggregated_metrics,
)
from docker_tool.hosting import start_host, stop_host

ROOT = Path("/dev/shm/ayush/web-experience-benchmark")
INPUT_CSV = ROOT / "harness/SAMPLE/input_100.csv"
HOST_MODE = os.environ.get("VAL_HOST_MODE", "local")

csv.field_size_limit(10 ** 7)


# --------------------------------------------------------------------------- #
# CSV row lookup
# --------------------------------------------------------------------------- #
def load_rows() -> dict[str, dict]:
    rows = {}
    with open(INPUT_CSV) as f:
        for r in csv.DictReader(f):
            rows[r["ID"].strip()] = r
    return rows


# --------------------------------------------------------------------------- #
# Reconstruct a site: clone @ commit, optionally apply a patch
# --------------------------------------------------------------------------- #
def reconstruct_site(row: dict, patch_file: Optional[Path] = None) -> Path:
    """Clone REPO_ID @ COMMIT_ID into a fresh temp dir; optionally git-apply a patch.

    Returns the repo dir (caller is responsible for cleanup).
    Raises RuntimeError on clone/patch failure.
    """
    repo_id = (row.get("repo_id") or row.get("REPO_ID") or "").strip()
    commit = (row.get("commit") or row.get("COMMIT_ID") or "").strip() or None
    safe = repo_id.replace("/", "_")
    dest = Path(tempfile.mkdtemp(prefix=f"val_{safe}_",
                                 dir=os.environ.get("TMPDIR") or tempfile.gettempdir()))
    clone_url = f"https://github.com/{repo_id}.git"
    if not git_clone(clone_url, dest, commit):
        raise RuntimeError(f"clone failed: {clone_url} @ {commit}")

    if patch_file is not None:
        # Mirror the harness: try git apply, then fall back to `patch -p1`.
        ok = subprocess.run(["git", "-C", str(dest), "apply", "--whitespace=nowarn",
                             str(patch_file)], capture_output=True, text=True)
        if ok.returncode != 0:
            alt = subprocess.run(["patch", "-p1", "-d", str(dest), "-i", str(patch_file)],
                                 capture_output=True, text=True)
            if alt.returncode != 0:
                raise RuntimeError(f"patch failed: {patch_file}\n{ok.stderr}\n{alt.stderr}")
    return dest


# --------------------------------------------------------------------------- #
# Host a reconstructed site -> URL  (reuses production start_host)
# --------------------------------------------------------------------------- #
class HostedSite:
    def __init__(self, repo_dir: Path, framework: str, host_file: str | None):
        self.repo_dir = repo_dir
        self.framework = framework
        self.host_file = host_file
        self.result = None

    def __enter__(self):
        port = find_available_port(start_port=8080 + (os.getpid() % 4000),
                                   use_global_alloc=False)
        self.result = start_host(
            repo_dir=self.repo_dir,
            framework=self.framework,
            host_file_path=self.host_file,
            port=port,
            log=self.repo_dir.parent / f"host_{port}.log",
            mode=HOST_MODE,
            slot=None,
        )
        if self.result.status != "success":
            raise RuntimeError(f"host failed: {self.result.error}")
        self.url = self.result.url or f"http://localhost:{port}"
        return self

    def __exit__(self, *exc):
        if self.result:
            stop_host(container_id=self.result.container_id, pid=self.result.pid)


# --------------------------------------------------------------------------- #
# Controlled measurement: N runs at a fixed settle_time / interaction flag
# --------------------------------------------------------------------------- #
def measure(url: str, device: str = "desktop", num_runs: int = 5,
            settle_time: int = 5000, simulate_interaction: bool = True) -> dict[str, Any]:
    """Take num_runs measurements at a FIXED settle_time (no fallback) and aggregate.

    Calls the production measure_cwv_metrics directly so we control the knobs the
    CLI does not expose. Returns {'runs': [...], 'aggregated': {...}}.
    """
    async def _go():
        runs = []
        for _ in range(num_runs):
            r = await measure_cwv_metrics(
                url, device=device, headless=True,
                settle_time=settle_time,
                simulate_interaction=simulate_interaction,
            )
            runs.append(r)
        return runs
    runs = asyncio.run(_go())
    return {"runs": runs, "aggregated": calculate_aggregated_metrics(runs)}


def cls_lcp(agg: dict) -> tuple[float, float]:
    return agg.get("CLS_median"), agg.get("LCP_p75")
