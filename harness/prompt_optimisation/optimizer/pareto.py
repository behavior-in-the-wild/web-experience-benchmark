"""
Pareto frontier utilities for multi-objective prompt optimization.

Objectives: (lcp_delta, inp_delta, cls_delta) — all in [-1, 1], higher = better.
Non-dominated sorting + NSGA-II crowding distance for parent selection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from harness.prompt_optimisation.prompts.schema import PromptConfig


@dataclass
class ParetoPoint:
    config: PromptConfig
    # Per-repo objective tuples — averaged to get mean_objectives
    repo_objectives: list[Tuple[float, float, float]] = field(default_factory=list)
    # Aggregated objectives (mean across minibatch)
    mean_objectives: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    scalar_score: float = -1.0
    generation: int = 0
    # Paths to trace files: {repo_id: (agent_log_path, patch_path)}
    trace_paths: dict[str, Tuple[str | None, str | None]] = field(default_factory=dict)

    @property
    def config_hash(self) -> str:
        return self.config.config_hash


def dominates(a: Tuple[float, float, float], b: Tuple[float, float, float]) -> bool:
    """Returns True if b dominates a (b >= a on all dims, > on at least one)."""
    return all(bv >= av for av, bv in zip(a, b)) and any(bv > av for av, bv in zip(a, b))


def pareto_frontier(points: list[ParetoPoint]) -> list[ParetoPoint]:
    """Return non-dominated subset, deduplicated by config_hash."""
    # Deduplicate by hash (keep highest scalar when tied)
    seen: dict[str, ParetoPoint] = {}
    for p in points:
        if p.config_hash not in seen or p.scalar_score > seen[p.config_hash].scalar_score:
            seen[p.config_hash] = p
    unique = list(seen.values())

    frontier = []
    for candidate in unique:
        dominated = False
        for other in unique:
            if other is candidate:
                continue
            if dominates(candidate.mean_objectives, other.mean_objectives):
                dominated = True
                break
        if not dominated:
            frontier.append(candidate)
    return frontier


def crowding_distance(frontier: list[ParetoPoint]) -> list[float]:
    """
    NSGA-II crowding distance for diversity-preserving parent selection.
    Returns distances aligned with frontier list.
    """
    n = len(frontier)
    if n <= 2:
        return [float("inf")] * n

    distances = [0.0] * n
    n_obj = 3  # lcp, inp, cls

    for obj_idx in range(n_obj):
        vals = [p.mean_objectives[obj_idx] for p in frontier]
        sorted_indices = sorted(range(n), key=lambda i: vals[i])
        # Boundary points get infinite distance
        distances[sorted_indices[0]] = float("inf")
        distances[sorted_indices[-1]] = float("inf")
        obj_range = vals[sorted_indices[-1]] - vals[sorted_indices[0]]
        if obj_range == 0.0:
            continue
        for rank in range(1, n - 1):
            i = sorted_indices[rank]
            prev_val = vals[sorted_indices[rank - 1]]
            next_val = vals[sorted_indices[rank + 1]]
            distances[i] += (next_val - prev_val) / obj_range

    return distances


def select_parents(frontier: list[ParetoPoint], n: int) -> list[ParetoPoint]:
    """
    Select n parents from the frontier using crowding distance (NSGA-II style).
    Larger distance = more diverse = preferred.
    """
    if len(frontier) <= n:
        return list(frontier)
    distances = crowding_distance(frontier)
    ranked = sorted(zip(distances, frontier), key=lambda x: -x[0])
    return [p for _, p in ranked[:n]]
