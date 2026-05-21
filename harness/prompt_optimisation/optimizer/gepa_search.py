"""
GEPA-style prompt optimization: Genetic-Pareto with LLM reflection.

Inspired by:
  "GEPA: Genetic-Pareto Prompt Optimization" (ICLR 2026 Oral, arXiv:2507.19457)

Key differences from plain TPE / MIPROv2:
  - Multi-objective Pareto frontier over (lcp_delta, inp_delta, cls_delta)
    instead of a single scalar score.
  - Reflection step: LLM reads execution traces (agent.log + patch) and
    diagnoses why the current instruction succeeded or failed per-repo.
  - Genetic operators: targeted mutation (from reflection) + crossover
    (combine lessons from two Pareto parents).
  - 35× fewer harness calls than random search for the same quality, because
    the reflection guides the search away from known failure modes.

Algorithm outline per generation:
  1. Evaluate current population on minibatch → Pareto objectives.
  2. Update cumulative Pareto frontier (non-dominated set).
  3. For each frontier parent, collect execution traces.
  4. Reflection call: "Given these traces and deltas, what should be changed?"
  5. Mutation call: generate N_MUTANTS mutations per parent.
  6. Crossover call: pair frontier parents, generate N_CROSS offspring.
  7. New population = deduplicated (mutations ∪ crossovers).
  8. Repeat.

Final output:
  - pareto_frontier.jsonl — all Pareto-optimal configs with objectives
  - best_prompt.json      — highest-scalar config from the frontier
"""
from __future__ import annotations

import csv as csv_mod
import json
import os
import random
import re
import time
from pathlib import Path
from statistics import mean
from typing import Optional

from openai import OpenAI
from rich.console import Console
from rich.table import Table

from harness.prompt_optimisation.bridge.cache import EvalCache
from harness.prompt_optimisation.bridge import parser as result_parser
from harness.prompt_optimisation.bridge import runner
from harness.prompt_optimisation.optimizer.bootstrap import load_demo_sets, run_bootstrap
from harness.prompt_optimisation.optimizer.metric import compute, compute_objectives, mean_objectives
from harness.prompt_optimisation.optimizer.pareto import (
    ParetoPoint,
    pareto_frontier,
    select_parents,
)
from harness.prompt_optimisation.prompts.proposer import generate_candidates, load_candidates, _jaccard
from harness.prompt_optimisation.prompts.schema import DemoExample, PromptConfig
from harness.prompt_optimisation.prompts.templates import BASELINE_PHASE1, BASELINE_PHASE2

console = Console()

# ── Hyperparameters ────────────────────────────────────────────────────────────
_N_GENERATIONS = 8
_POP_INIT_SIZE = 6       # initial random variants from proposer to seed gen-0
_N_MUTANTS_PER_PARENT = 2
_N_CROSSOVERS = 2        # total crossover offspring per generation
_MAX_FRONTIER_SIZE = 12  # cap to avoid O(n²) dominance checks blowing up
_MINIBATCH_SIZE = 10
_N_MINIBATCHES = 5
_SEED = 42
_DEDUP_THRESHOLD = 0.80  # slightly tighter than proposer.py


def _make_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
        base_url=os.environ.get("OPENCODE_OPENAI_BASE_URL", "http://localhost:8000/v1"),
    )


def _model_name() -> str:
    return os.environ.get("VLLM_SERVED_MODEL_NAME", "qwen3-coder-next")


# ── Partition helper (reused from search.py) ──────────────────────────────────

def _partition_minibatches(train_rows: list[dict], seed: int = _SEED) -> list[list[dict]]:
    by_fw: dict[str, list[dict]] = {}
    for r in train_rows:
        fw = str(r.get("FRAMEWORK", "other")).lower().strip()
        by_fw.setdefault(fw, []).append(r)
    rng = random.Random(seed)
    for fw in by_fw:
        rng.shuffle(by_fw[fw])
    batches: list[list[dict]] = [[] for _ in range(_N_MINIBATCHES)]
    fw_iters = {fw: iter(rows) for fw, rows in by_fw.items()}
    fws = list(fw_iters.keys())
    i = 0
    while True:
        added = False
        for fw in fws:
            try:
                row = next(fw_iters[fw])
            except StopIteration:
                continue
            batches[i % _N_MINIBATCHES].append(row)
            i += 1
            added = True
        if not added:
            break
    all_rows = [r for b in batches for r in b]
    rng.shuffle(all_rows)
    idx = 0
    for b in range(_N_MINIBATCHES):
        while len(batches[b]) < _MINIBATCH_SIZE and idx < len(all_rows):
            if all_rows[idx] not in batches[b]:
                batches[b].append(all_rows[idx])
            idx += 1
        batches[b] = batches[b][:_MINIBATCH_SIZE]
    return batches


# ── Trace collection ───────────────────────────────────────────────────────────

def _collect_traces(results_dir: Path, rows: list[dict]) -> list[dict]:
    """Return one trace dict per row (agent_log + patch snippets)."""
    return [
        result_parser.read_trace(results_dir, str(r["ID"]))
        for r in rows
    ]


# ── Reflection ────────────────────────────────────────────────────────────────

def _reflect(
    client: OpenAI,
    model: str,
    phase1_text: str,
    phase2_text: str,
    rows: list[dict],
    objectives: list[tuple[float, float, float]],
    traces: list[dict],
) -> str:
    """
    Ask the LLM to diagnose why the current prompt succeeded/failed.
    Returns a free-text reflection string to be used by the mutation step.
    """
    # Build per-repo evidence blocks (top 3 worst + top 3 best by LCP delta)
    ranked = sorted(
        zip(objectives, traces, rows),
        key=lambda x: x[0][0],  # sort by lcp_delta ascending
    )
    worst_3 = ranked[:3]
    best_3 = ranked[-3:]

    def _evidence_block(label: str, items: list) -> str:
        blocks = []
        for obj, trace, row in items:
            lcp_d, inp_d, cls_d = obj
            log_snip = (trace["agent_log"] or "(no log)")[-600:]
            patch_snip = (trace["patch"] or "(no patch)")[:400]
            blocks.append(
                f"Repo: {row.get('REPO_ID', '?')} | Framework: {row.get('FRAMEWORK', '?')}\n"
                f"Outcome: LCP Δ={lcp_d:+.2f}  INP Δ={inp_d:+.2f}  CLS Δ={cls_d:+.2f}\n"
                f"Agent log (tail):\n{log_snip}\n"
                f"Patch (head):\n{patch_snip}"
            )
        return f"\n--- {label} ---\n" + "\n\n".join(blocks)

    evidence = _evidence_block("WORST OUTCOMES (failed repos)", worst_3)
    evidence += _evidence_block("BEST OUTCOMES (succeeded repos)", best_3)

    prompt = f"""You are a Core Web Vitals (CWV) prompt engineering expert.

Below are the current Phase 1 (planning) and Phase 2 (execution) instructions given to an AI agent that optimizes web repos for LCP, INP, and CLS.

## Phase 1 instruction (planning):
{phase1_text[:800]}

## Phase 2 instruction (execution):
{phase2_text[:600]}

## Evidence from recent runs:
{evidence}

## Your task:
Diagnose SPECIFICALLY why the instruction succeeded or failed based on the evidence.
Focus on:
1. What CWV-specific steps did the agent take (or miss)?
2. What pattern explains the failures (wrong element targeted, missed image attributes, etc.)?
3. What concrete change to the instruction would fix the top failure mode?

Return a concise diagnosis (3-5 bullet points). Be specific about file names, attribute names, and metric values.
"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=600,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        return f"(reflection failed: {e})"


# ── Mutation ──────────────────────────────────────────────────────────────────

def _mutate(
    client: OpenAI,
    model: str,
    phase: str,
    current_text: str,
    reflection: str,
    n: int,
) -> list[str]:
    """Generate n targeted mutations of current_text guided by reflection."""
    baseline_clause = (
        f"Keep ${'{FRAMEWORK}'}, ${'{CWV_MOBILE}'}, ${'{CWV_DESKTOP}'} placeholders intact."
        if phase == "phase1"
        else f"Keep ${'{FRAMEWORK}'} placeholder intact."
    )
    prompt = f"""You are an expert at writing LLM prompts for {phase} of a two-phase Core Web Vitals optimization agent.

## Current instruction:
{current_text}

## Diagnosis from recent runs (what needs to change):
{reflection}

## Your task:
Generate {n} improved variants of the instruction that address the diagnosis above.

Rules:
1. Each variant must be complete and self-contained (not a diff).
2. Each variant must explicitly mention at least one specific CWV mechanism (LCP, INP, CLS, lazy-load, preload, etc.).
3. {baseline_clause}
4. Variants must be meaningfully different from each other and from the original.
5. Apply the diagnostic insights directly — do not make cosmetic changes.

Output format: JSON array of {n} strings. No markdown wrapper, no explanation.
"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.85,
            max_tokens=4000,
        )
        raw = resp.choices[0].message.content or "[]"
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())
        variants = json.loads(raw)
        if not isinstance(variants, list):
            return []
        return [str(v).strip() for v in variants if v and len(v) > 50]
    except Exception:
        return []


# ── Crossover ─────────────────────────────────────────────────────────────────

def _crossover(
    client: OpenAI,
    model: str,
    parent_a: PromptConfig,
    parent_b: PromptConfig,
) -> Optional[PromptConfig]:
    """
    Combine lessons from two Pareto-optimal prompts to produce an offspring.
    Crosses both phase1 and phase2 instructions together.
    """
    prompt = f"""You are an expert at writing LLM prompts for Core Web Vitals (CWV) optimization.

Two Pareto-optimal Phase 1 instructions performed well on different CWV axes.
Combine their strongest elements into a single improved instruction.

## Parent A (Phase 1):
{parent_a.phase1_instruction[:600]}

## Parent B (Phase 1):
{parent_b.phase1_instruction[:600]}

Produce ONE combined Phase 1 instruction that takes the best specifics from both.
Rules: must be complete, self-contained, mention at least one CWV mechanism,
keep ${'{FRAMEWORK}'} / ${'{CWV_MOBILE}'} / ${'{CWV_DESKTOP}'} placeholders intact.
Return only the instruction text, no explanation.
"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=1200,
        )
        p1_text = (resp.choices[0].message.content or "").strip()
        if not p1_text or len(p1_text) < 50:
            return None
        # Use whichever parent's phase2 instruction has higher scalar score
        # (we don't always have it here, so just pick parent_a's)
        return PromptConfig.build(
            phase1_text=p1_text,
            phase2_text=parent_a.phase2_instruction,
            demos=parent_a.demos,
        )
    except Exception:
        return None


# ── Evaluation helper ─────────────────────────────────────────────────────────

def _evaluate_config(
    config: PromptConfig,
    batch: list[dict],
    results_dir: Path,
    cache: EvalCache,
    num_runs: int,
    gen: int,
    trial_idx: int,
) -> tuple[list[tuple[float, float, float]], float, list[dict]]:
    """
    Evaluate a PromptConfig on a minibatch.
    Returns (objectives_per_repo, scalar_score, traces).
    """
    objectives: list[tuple[float, float, float]] = []
    scalars: list[float] = []
    traces: list[dict] = []

    trial_dir = results_dir / f"gen{gen:02d}_t{trial_idx:03d}"

    # Check per-repo cache first
    uncached_rows = []
    cached_map: dict[str, tuple] = {}
    for row in batch:
        cached = cache.get(config.config_hash, str(row["REPO_ID"]))
        if cached is not None:
            cached_map[str(row["REPO_ID"])] = (cached, (cached, cached * 0.7, cached * 0.3))
        else:
            uncached_rows.append(row)

    # Run harness for uncached repos
    run_traces: dict[str, dict] = {}
    if uncached_rows:
        results_path = runner.run(
            config=config,
            rows=uncached_rows,
            out_dir=trial_dir,
            parallel=1,
            num_runs=num_runs,
        )
        cwv_results = result_parser.parse_batch(results_path, uncached_rows)
        for row, cwv in zip(uncached_rows, cwv_results):
            repo_id = str(row["REPO_ID"])
            obj = compute_objectives(cwv)
            sc = compute(cwv)
            cache.put(config.config_hash, repo_id, sc, {})
            cached_map[repo_id] = (sc, obj)
            run_traces[str(row["ID"])] = result_parser.read_trace(results_path, str(row["ID"]))

    # Assemble results in batch order
    for row in batch:
        repo_id = str(row["REPO_ID"])
        sc, obj = cached_map.get(repo_id, (-1.0, (-1.0, -1.0, -1.0)))
        scalars.append(sc)
        objectives.append(obj)
        traces.append(run_traces.get(str(row["ID"]), {"agent_log": "", "patch": ""}))

    scalar_score = mean(scalars) if scalars else -1.0
    return objectives, scalar_score, traces


# ── Trial log ─────────────────────────────────────────────────────────────────

_LOG_FIELDS = ["gen", "trial", "config_hash", "lcp_mean", "inp_mean", "cls_mean", "scalar", "on_frontier"]


def _log_trial(
    log_path: Path,
    gen: int,
    trial: int,
    point: ParetoPoint,
    on_frontier: bool,
) -> None:
    row = {
        "gen": gen,
        "trial": trial,
        "config_hash": point.config_hash,
        "lcp_mean": round(point.mean_objectives[0], 4),
        "inp_mean": round(point.mean_objectives[1], 4),
        "cls_mean": round(point.mean_objectives[2], 4),
        "scalar": round(point.scalar_score, 4),
        "on_frontier": int(on_frontier),
    }
    with log_path.open("a", newline="") as f:
        csv_mod.DictWriter(f, fieldnames=_LOG_FIELDS).writerow(row)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_gepa(
    run_dir: Path,
    training_csv,
    parallel: int = 4,
    num_runs: int = 3,
    n_generations: int = _N_GENERATIONS,
    resume: bool = False,
) -> PromptConfig:
    """
    Run the GEPA evolutionary search loop.

    Steps:
      1. Bootstrap + candidate generation (same as TPE path)
      2. Seed population from initial candidates
      3. For each generation:
         a. Evaluate on minibatch
         b. Update Pareto frontier
         c. Reflect on traces
         d. Mutate + crossover → next population
      4. Validate best config on held-out validation set
      5. Save pareto_frontier.jsonl + best_prompt.json

    Returns best PromptConfig (highest scalar on frontier).
    """
    import pandas as pd
    from harness.prompt_optimisation.optimizer.metric import compute as scalar_compute

    run_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(training_csv)
    train_rows = df[df["split"] == "train"].to_dict("records")
    val_rows = df[df["split"] == "validation"].to_dict("records")
    console.print(f"[bold]Train:[/bold] {len(train_rows)}  [bold]Val:[/bold] {len(val_rows)}")

    # ── Bootstrap ──────────────────────────────────────────────────────────────
    demo_sets_path = run_dir / "demo_sets.jsonl"
    if demo_sets_path.exists() and not resume:
        console.print("[yellow]Skipping bootstrap (demo_sets.jsonl exists)[/yellow]")
        demo_sets = load_demo_sets(run_dir)
    else:
        _, demo_sets = run_bootstrap(train_rows, run_dir, parallel=parallel, num_runs=num_runs)

    # ── Initial candidates from proposer ──────────────────────────────────────
    candidates_path = run_dir / "candidates.jsonl"
    if candidates_path.exists() and not resume:
        console.print("[yellow]Skipping proposal (candidates.jsonl exists)[/yellow]")
        p1_candidates, p2_candidates = load_candidates(run_dir)
    else:
        top_demos: list[DemoExample] = []
        for ds in demo_sets:
            for d in ds:
                if d not in top_demos:
                    top_demos.append(d)
        top_demos.sort(key=lambda d: d.lcp_delta_pct, reverse=True)
        p1_candidates, p2_candidates = generate_candidates(top_demos[:3], run_dir)

    # ── Minibatch partition ────────────────────────────────────────────────────
    mb_path = run_dir / "minibatches.json"
    if mb_path.exists() and not resume:
        minibatches = json.loads(mb_path.read_text())
    else:
        minibatches = _partition_minibatches(train_rows)
        mb_path.write_text(json.dumps(
            [[{k: v for k, v in r.items()} for r in b] for b in minibatches]
        ))

    # ── Cache + trial log ──────────────────────────────────────────────────────
    cache = EvalCache(run_dir / "eval_cache.db")
    log_path = run_dir / "gepa_trial_log.csv"
    if not log_path.exists():
        with log_path.open("w", newline="") as f:
            csv_mod.DictWriter(f, fieldnames=_LOG_FIELDS).writeheader()

    # ── LLM client ────────────────────────────────────────────────────────────
    client = _make_client()
    model = _model_name()

    # ── Seed population: baseline + first _POP_INIT_SIZE candidates ───────────
    rng = random.Random(_SEED)
    init_configs: list[PromptConfig] = []

    # Always include baseline
    init_configs.append(PromptConfig.build(
        phase1_text=BASELINE_PHASE1,
        phase2_text=BASELINE_PHASE2,
        demos=demo_sets[0],
    ))

    # Add cross-product samples from initial candidates
    p1_pool = p1_candidates[:min(4, len(p1_candidates))]
    p2_pool = p2_candidates[:min(3, len(p2_candidates))]
    ds_pool = demo_sets[:min(3, len(demo_sets))]
    combos = [
        (p1, p2, ds)
        for p1 in p1_pool for p2 in p2_pool for ds in ds_pool
    ]
    rng.shuffle(combos)
    for p1c, p2c, ds in combos[:_POP_INIT_SIZE]:
        cfg = PromptConfig.build(
            phase1_text=p1c.text,
            phase2_text=p2c.text,
            demos=ds,
        )
        if all(cfg.config_hash != ic.config_hash for ic in init_configs):
            init_configs.append(cfg)

    # ── GEPA loop ─────────────────────────────────────────────────────────────
    frontier: list[ParetoPoint] = []
    all_points: list[ParetoPoint] = []  # cumulative history for final frontier
    current_population = init_configs

    trial_counter = 0

    for gen in range(n_generations):
        gen_batch = minibatches[gen % _N_MINIBATCHES]
        results_dir = run_dir / f"gen{gen:02d}_results"

        console.print(f"\n[bold cyan]Generation {gen}/{n_generations - 1}[/bold cyan] "
                      f"| population={len(current_population)} "
                      f"| frontier={len(frontier)}")

        gen_points: list[ParetoPoint] = []
        last_good_traces: list[dict] = []
        last_good_rows: list[dict] = []
        best_parent_for_reflection: Optional[ParetoPoint] = None

        for cfg in current_population:
            t0 = time.time()
            objectives, scalar, traces = _evaluate_config(
                config=cfg,
                batch=gen_batch,
                results_dir=results_dir,
                cache=cache,
                num_runs=num_runs,
                gen=gen,
                trial_idx=trial_counter,
            )
            trial_counter += 1
            m_obj = mean_objectives(objectives)
            point = ParetoPoint(
                config=cfg,
                repo_objectives=objectives,
                mean_objectives=m_obj,
                scalar_score=scalar,
                generation=gen,
            )
            gen_points.append(point)
            all_points.append(point)
            console.print(
                f"  cfg={cfg.config_hash} | "
                f"LCP={m_obj[0]:+.3f} INP={m_obj[1]:+.3f} CLS={m_obj[2]:+.3f} "
                f"| scalar={scalar:+.3f} | {time.time()-t0:.0f}s"
            )
            if scalar > (best_parent_for_reflection.scalar_score if best_parent_for_reflection else -2.0):
                best_parent_for_reflection = point
                last_good_traces = traces
                last_good_rows = gen_batch

        # Update cumulative Pareto frontier
        frontier = pareto_frontier(all_points)
        # Cap frontier size
        if len(frontier) > _MAX_FRONTIER_SIZE:
            from harness.prompt_optimisation.optimizer.pareto import crowding_distance
            dists = crowding_distance(frontier)
            ranked = sorted(zip(dists, frontier), key=lambda x: -x[0])
            frontier = [p for _, p in ranked[:_MAX_FRONTIER_SIZE]]

        # Log each gen_point with frontier status
        frontier_hashes = {p.config_hash for p in frontier}
        for point in gen_points:
            _log_trial(log_path, gen, trial_counter, point, point.config_hash in frontier_hashes)

        _print_frontier_table(frontier, gen)

        if gen == n_generations - 1:
            break  # no need to generate offspring after the last generation

        # ── Reflection ────────────────────────────────────────────────────────
        # Use the best parent from this generation (or best frontier member)
        reflect_parent = best_parent_for_reflection or (frontier[0] if frontier else None)
        reflection = ""
        if reflect_parent and last_good_traces and last_good_rows:
            console.print(f"  [dim]Reflecting on traces from cfg={reflect_parent.config_hash}…[/dim]")
            reflection = _reflect(
                client, model,
                reflect_parent.config.phase1_instruction,
                reflect_parent.config.phase2_instruction,
                last_good_rows,
                reflect_parent.repo_objectives,
                last_good_traces,
            )
            (run_dir / f"gen{gen:02d}_reflection.txt").write_text(reflection)

        # ── Select parents from frontier ───────────────────────────────────────
        n_parents = min(3, len(frontier)) if frontier else 0
        parents = select_parents(frontier, n_parents) if frontier else []

        next_population: list[PromptConfig] = []

        # Mutation: for each parent, generate mutations guided by reflection
        for parent in parents:
            p1_variants = _mutate(
                client, model, "phase1",
                parent.config.phase1_instruction,
                reflection,
                _N_MUTANTS_PER_PARENT,
            )
            p2_variants = _mutate(
                client, model, "phase2",
                parent.config.phase2_instruction,
                reflection,
                _N_MUTANTS_PER_PARENT,
            )
            # Pair each p1 variant with parent's phase2 and vice versa
            for p1v in p1_variants:
                cfg = PromptConfig.build(
                    phase1_text=p1v,
                    phase2_text=parent.config.phase2_instruction,
                    demos=parent.config.demos,
                )
                next_population.append(cfg)
            for p2v in p2_variants:
                cfg = PromptConfig.build(
                    phase1_text=parent.config.phase1_instruction,
                    phase2_text=p2v,
                    demos=parent.config.demos,
                )
                next_population.append(cfg)

        # Crossover: pair frontier parents
        if len(parents) >= 2:
            for _ in range(_N_CROSSOVERS):
                pa, pb = rng.sample(parents, 2)
                offspring = _crossover(client, model, pa.config, pb.config)
                if offspring:
                    next_population.append(offspring)

        # Deduplicate next population (against each other + all historical hashes)
        historical_hashes = {p.config_hash for p in all_points}
        deduped: list[PromptConfig] = []
        seen_hashes = set(historical_hashes)
        for cfg in next_population:
            if cfg.config_hash in seen_hashes:
                continue
            if any(_jaccard(cfg.phase1_instruction, d.phase1_instruction) > _DEDUP_THRESHOLD
                   for d in deduped):
                continue
            seen_hashes.add(cfg.config_hash)
            deduped.append(cfg)

        if not deduped:
            # Fallback: re-seed with fresh proposer variants using random demo set
            console.print("  [yellow]No novel offspring — re-seeding from candidates[/yellow]")
            ds_idx = gen % len(demo_sets)
            deduped = [
                PromptConfig.build(
                    phase1_text=p1_candidates[rng.randint(0, len(p1_candidates) - 1)].text,
                    phase2_text=p2_candidates[rng.randint(0, len(p2_candidates) - 1)].text,
                    demos=demo_sets[ds_idx],
                )
                for _ in range(3)
            ]

        current_population = deduped
        console.print(f"  Next population: {len(current_population)} configs")

    # ── Final frontier ─────────────────────────────────────────────────────────
    frontier = pareto_frontier(all_points)
    best_point = max(frontier, key=lambda p: p.scalar_score)
    best_config = best_point.config

    console.print(f"\n[bold green]GEPA complete.[/bold green]")
    console.print(f"Final frontier size: {len(frontier)}")
    console.print(f"Best config: {best_config.config_hash} (scalar={best_point.scalar_score:.4f})")

    # ── Save Pareto frontier ───────────────────────────────────────────────────
    frontier_path = run_dir / "pareto_frontier.jsonl"
    with frontier_path.open("w") as f:
        for p in sorted(frontier, key=lambda x: -x.scalar_score):
            f.write(json.dumps({
                "config_hash": p.config_hash,
                "phase1_instruction": p.config.phase1_instruction,
                "phase2_instruction": p.config.phase2_instruction,
                "mean_lcp_delta": p.mean_objectives[0],
                "mean_inp_delta": p.mean_objectives[1],
                "mean_cls_delta": p.mean_objectives[2],
                "scalar_score": p.scalar_score,
                "generation": p.generation,
            }) + "\n")

    # ── Validation on held-out set ─────────────────────────────────────────────
    console.print(f"\n[bold]Validating on {len(val_rows)} held-out repos …[/bold]")
    val_out = run_dir / "validation"
    val_results_dir = runner.run(
        config=best_config,
        rows=val_rows,
        out_dir=val_out,
        parallel=parallel,
        num_runs=num_runs,
        skip_visual=True,
    )
    val_cwv = result_parser.parse_batch(val_results_dir, val_rows)
    val_scores = [scalar_compute(r) for r in val_cwv]
    val_mean = mean(val_scores) if val_scores else -1.0

    gap = best_point.scalar_score - val_mean
    if gap > 0.15:
        console.print(f"[bold red]WARNING: generalization gap={gap:.3f} > 0.15[/bold red]")
    else:
        console.print(f"[green]Generalization gap={gap:.3f} ✓[/green]")

    # ── Save best_prompt.json (compatible with inject.py) ─────────────────────
    best_path = run_dir / "best_prompt.json"
    best_path.write_text(json.dumps({
        "phase1_instruction": best_config.phase1_instruction,
        "phase2_instruction": best_config.phase2_instruction,
        "config_hash": best_config.config_hash,
        "train_score": best_point.scalar_score,
        "validation_score": val_mean,
        "generalization_gap": gap,
        "pareto_objectives": {
            "lcp": best_point.mean_objectives[0],
            "inp": best_point.mean_objectives[1],
            "cls": best_point.mean_objectives[2],
        },
        "algorithm": "gepa",
        "n_generations": n_generations,
        "frontier_size": len(frontier),
    }, indent=2))
    console.print(f"[bold]Saved best_prompt.json → {best_path}[/bold]")
    console.print(f"[bold]Saved pareto_frontier.jsonl → {frontier_path}[/bold]")

    return best_config


def _print_frontier_table(frontier: list[ParetoPoint], gen: int) -> None:
    table = Table(title=f"Pareto frontier after gen {gen}", show_header=True)
    table.add_column("Hash", style="cyan", width=18)
    table.add_column("LCP Δ", justify="right")
    table.add_column("INP Δ", justify="right")
    table.add_column("CLS Δ", justify="right")
    table.add_column("Scalar", justify="right")
    table.add_column("Gen", justify="right")
    for p in sorted(frontier, key=lambda x: -x.scalar_score):
        table.add_row(
            p.config_hash,
            f"{p.mean_objectives[0]:+.3f}",
            f"{p.mean_objectives[1]:+.3f}",
            f"{p.mean_objectives[2]:+.3f}",
            f"{p.scalar_score:+.3f}",
            str(p.generation),
        )
    console.print(table)
