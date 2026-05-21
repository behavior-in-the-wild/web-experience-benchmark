"""
Subprocess interface to harness/evaluate.sh.

Key design decisions:
- CSV is passed via the CSV= env var (evaluate.sh uses this, no --input-csv flag)
- Agent is set via EVAL_AGENTS= env var
- Prompt overrides are passed via PHASE1_INSTRUCTION / PHASE2_INSTRUCTION env vars
  (expanded by envsubst inside template_opencode_os.sh)
- Results land in a temp dir controlled via EVAL_OUT_DIR=
- --skip-init-psi --skip-final-psi --skip-visual always set during optimization trials
"""
from __future__ import annotations

import csv as csv_mod
import os
import subprocess
import tempfile
from pathlib import Path

import pandas as pd

from harness.prompt_optimisation.prompts.schema import PromptConfig

ROOT = Path(__file__).parent.parent.parent.parent
EVALUATE_SH = ROOT / "harness" / "evaluate.sh"
AGENT_PATH = "agents/template_opencode_os.sh"


def _write_filtered_csv(rows: list[dict], tmp_dir: Path) -> Path:
    """Write a CSV containing only the target rows (same columns as input.csv)."""
    filtered = tmp_dir / "filtered.csv"
    if not rows:
        raise ValueError("rows must not be empty")
    fieldnames = list(rows[0].keys())
    with filtered.open("w", newline="") as f:
        writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return filtered


def run(
    config: PromptConfig,
    rows: list[dict],
    out_dir: Path,
    parallel: int = 4,
    num_runs: int = 3,
    skip_visual: bool = True,
    extra_env: dict | None = None,
) -> Path:
    """
    Run evaluate.sh for the given rows with the given PromptConfig.

    Args:
        config:       PromptConfig with phase1/phase2 instruction text
        rows:         list of CSV row dicts (must include ID, REPO_ID, FRAMEWORK, etc.)
        out_dir:      directory where results/ will be written
        parallel:     max concurrent harness jobs
        num_runs:     CWV measurement runs per repo (median used by harness)
        skip_visual:  skip screenshot + AI visual validation (faster for trials)
        extra_env:    additional env vars to merge

    Returns:
        Path to the results/ directory.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results_dir = out_dir / "results"
    results_dir.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="po_runner_", dir=os.environ.get("HARNESS_TMPDIR", tempfile.gettempdir())) as tmp:
        tmp_path = Path(tmp)
        filtered_csv = _write_filtered_csv(rows, tmp_path)

        env = os.environ.copy()
        env.update({
            "CSV": str(filtered_csv),
            "EVAL_AGENTS": AGENT_PATH,
            "EVAL_OUT_DIR": str(out_dir),
            "NUM_RUNS": str(num_runs),
            "PHASE1_INSTRUCTION": config.phase1_instruction,
            "PHASE2_INSTRUCTION": config.phase2_instruction,
        })
        if extra_env:
            env.update(extra_env)

        cmd = [
            "bash", str(EVALUATE_SH),
            "--parallel", str(parallel),
            "--skip-init-psi",
            "--skip-final-psi",
        ]
        if skip_visual:
            cmd.append("--skip-visual")

        result = subprocess.run(
            cmd,
            env=env,
            cwd=str(ROOT / "harness"),
            capture_output=False,
        )

        if result.returncode != 0:
            # Non-zero exit is common when individual jobs fail; results may still exist
            pass

    return results_dir


def run_baseline(
    rows: list[dict],
    out_dir: Path,
    parallel: int = 4,
    num_runs: int = 3,
) -> Path:
    """
    Run evaluate.sh with the unmodified template (no prompt override).
    Used by bootstrap to collect demo candidates.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results_dir = out_dir / "results"
    results_dir.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="po_baseline_", dir=os.environ.get("HARNESS_TMPDIR", tempfile.gettempdir())) as tmp:
        tmp_path = Path(tmp)
        filtered_csv = _write_filtered_csv(rows, tmp_path)

        env = os.environ.copy()
        env.update({
            "CSV": str(filtered_csv),
            "EVAL_AGENTS": AGENT_PATH,
            "EVAL_OUT_DIR": str(out_dir),
            "NUM_RUNS": str(num_runs),
        })
        # Unset any leftover override vars
        env.pop("PHASE1_INSTRUCTION", None)
        env.pop("PHASE2_INSTRUCTION", None)

        cmd = [
            "bash", str(EVALUATE_SH),
            "--parallel", str(parallel),
            "--skip-init-psi",
            "--skip-final-psi",
            "--skip-visual",
        ]
        subprocess.run(cmd, env=env, cwd=str(ROOT / "harness"), capture_output=False)

    return results_dir
