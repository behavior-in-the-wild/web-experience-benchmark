from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CWVResult:
    repo_id: str
    # Post-patch measurements (None = file missing / run failed)
    lcp_mean: float | None = None
    inp_mean: float | None = None
    cls_mean: float | None = None
    lcp_mean_desktop: float | None = None
    inp_mean_desktop: float | None = None
    cls_mean_desktop: float | None = None
    regression: bool = False
    valid: bool = True   # False when result files are missing entirely

    # Baseline values from input CSV (populated by parser)
    baseline_lcp: float = 0.0
    baseline_inp: float = 0.0
    baseline_cls: float = 0.0
    baseline_lcp_desktop: float = 0.0
    baseline_inp_desktop: float = 0.0
    baseline_cls_desktop: float = 0.0


# Denominator floors: prevent division-by-zero and cap deltas on already-fast repos
_LCP_FLOOR = 100.0    # ms
_INP_FLOOR = 50.0     # ms
_CLS_FLOOR = 0.01

# Weights (must sum to 1.0)
_W_LCP = 0.50
_W_INP = 0.35
_W_CLS = 0.15

_REGRESSION_PENALTY = -1.0


def _delta(baseline: float, result: float | None, floor: float) -> float:
    if result is None:
        return -1.0
    denom = max(baseline, floor)
    raw = (baseline - result) / denom
    return max(-1.0, min(1.0, raw))


def compute(result: CWVResult) -> float:
    """
    Returns a score in [-1, 1] where positive = improvement.
    Mobile and desktop are averaged equally.
    Missing result files → -1.0 (harness failure).
    """
    if not result.valid:
        return -1.0

    mobile_score = (
        _W_LCP * _delta(result.baseline_lcp, result.lcp_mean, _LCP_FLOOR)
        + _W_INP * _delta(result.baseline_inp, result.inp_mean, _INP_FLOOR)
        + _W_CLS * _delta(result.baseline_cls, result.cls_mean, _CLS_FLOOR)
    )

    desktop_score = (
        _W_LCP * _delta(result.baseline_lcp_desktop, result.lcp_mean_desktop, _LCP_FLOOR)
        + _W_INP * _delta(result.baseline_inp_desktop, result.inp_mean_desktop, _INP_FLOOR)
        + _W_CLS * _delta(result.baseline_cls_desktop, result.cls_mean_desktop, _CLS_FLOOR)
    )

    score = (mobile_score + desktop_score) / 2.0

    if result.regression:
        score += _REGRESSION_PENALTY

    return float(max(-1.0, min(1.0, score)))


def compute_objectives(result: CWVResult) -> tuple[float, float, float]:
    """
    Returns (lcp_delta, inp_delta, cls_delta) averaged over mobile+desktop.
    Values in [-1, 1]. Regression sets all to -1.0.
    """
    if not result.valid:
        return (-1.0, -1.0, -1.0)

    lcp = (
        _delta(result.baseline_lcp, result.lcp_mean, _LCP_FLOOR)
        + _delta(result.baseline_lcp_desktop, result.lcp_mean_desktop, _LCP_FLOOR)
    ) / 2.0
    inp = (
        _delta(result.baseline_inp, result.inp_mean, _INP_FLOOR)
        + _delta(result.baseline_inp_desktop, result.inp_mean_desktop, _INP_FLOOR)
    ) / 2.0
    cls = (
        _delta(result.baseline_cls, result.cls_mean, _CLS_FLOOR)
        + _delta(result.baseline_cls_desktop, result.cls_mean_desktop, _CLS_FLOOR)
    ) / 2.0

    if result.regression:
        return (-1.0, -1.0, -1.0)

    return (float(lcp), float(inp), float(cls))


def mean_objectives(
    objectives: list[tuple[float, float, float]],
) -> tuple[float, float, float]:
    """Element-wise mean of a list of objective tuples."""
    if not objectives:
        return (-1.0, -1.0, -1.0)
    n = len(objectives)
    return (
        sum(o[0] for o in objectives) / n,
        sum(o[1] for o in objectives) / n,
        sum(o[2] for o in objectives) / n,
    )


def compute_batch(results: list[CWVResult]) -> float:
    """Mean score over a list of results. Empty list → -1.0."""
    if not results:
        return -1.0
    return sum(compute(r) for r in results) / len(results)
