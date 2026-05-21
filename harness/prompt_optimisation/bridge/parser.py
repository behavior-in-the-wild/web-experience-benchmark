from __future__ import annotations

import json
from pathlib import Path

from harness.prompt_optimisation.optimizer.metric import CWVResult

AGENT_NAME = "template_opencode_os"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def parse_result(results_dir: Path, repo_id: str, row_id: str) -> CWVResult:
    """
    Parse post-patch CWV result files for one repo from a harness results dir.

    File naming (from evaluate.sh):
        {row_id}_{AGENT_NAME}_mobile.json   — post-patch mobile CWV
        {row_id}_{AGENT_NAME}_desktop.json  — post-patch desktop CWV
        {row_id}_{AGENT_NAME}_visual.json   — regression check
        {row_id}_{AGENT_NAME}_cwv_data.json — baseline CWV from CSV (fallback)
    """
    prefix = f"{row_id}_{AGENT_NAME}"

    mobile = _read_json(results_dir / f"{prefix}_mobile.json")
    desktop = _read_json(results_dir / f"{prefix}_desktop.json")
    visual = _read_json(results_dir / f"{prefix}_visual.json")
    cwv_data = _read_json(results_dir / f"{prefix}_cwv_data.json")

    if mobile is None and desktop is None:
        return CWVResult(repo_id=repo_id, valid=False)

    baseline_mobile: dict = {}
    baseline_desktop: dict = {}
    if cwv_data:
        baseline_mobile = cwv_data.get("CWV_BASELINE_MOBILE") or {}
        baseline_desktop = cwv_data.get("CWV_BASELINE_DESKTOP") or {}

    return CWVResult(
        repo_id=repo_id,
        valid=True,
        # post-patch mobile
        lcp_mean=mobile.get("LCP_mean") if mobile else None,
        inp_mean=mobile.get("INP_mean") if mobile else None,
        cls_mean=mobile.get("CLS_mean") if mobile else None,
        # post-patch desktop
        lcp_mean_desktop=desktop.get("LCP_mean") if desktop else None,
        inp_mean_desktop=desktop.get("INP_mean") if desktop else None,
        cls_mean_desktop=desktop.get("CLS_mean") if desktop else None,
        # visual regression
        regression=bool((visual or {}).get("regression", False)),
        # baselines
        baseline_lcp=float(baseline_mobile.get("LCP_mean", 0) or 0),
        baseline_inp=float(baseline_mobile.get("INP_mean", 0) or 0),
        baseline_cls=float(baseline_mobile.get("CLS_mean", 0) or 0),
        baseline_lcp_desktop=float(baseline_desktop.get("LCP_mean", 0) or 0),
        baseline_inp_desktop=float(baseline_desktop.get("INP_mean", 0) or 0),
        baseline_cls_desktop=float(baseline_desktop.get("CLS_mean", 0) or 0),
    )


def parse_batch(
    results_dir: Path,
    rows: list[dict],  # each dict has "ID" and "REPO_ID" keys
) -> list[CWVResult]:
    return [parse_result(results_dir, r["REPO_ID"], str(r["ID"])) for r in rows]


def read_trace(results_dir: Path, row_id: str, max_log: int = 2000, max_patch: int = 1500) -> dict:
    """
    Return truncated agent.log + patch text for a single repo run.
    Used by GEPA reflection step. Returns empty strings if files missing.
    """
    prefix = f"{row_id}_{AGENT_NAME}"
    log_path = results_dir / f"{prefix}_agent.log"
    patch_path = results_dir / f"{prefix}.patch"

    agent_log = ""
    if log_path.exists():
        try:
            text = log_path.read_text(errors="replace")
            agent_log = text[-max_log:] if len(text) > max_log else text
        except Exception:
            pass

    patch = ""
    if patch_path.exists():
        try:
            text = patch_path.read_text(errors="replace")
            patch = text[:max_patch] if len(text) > max_patch else text
        except Exception:
            pass

    return {"agent_log": agent_log, "patch": patch}
