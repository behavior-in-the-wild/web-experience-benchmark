"""
Optuna TPE search over (phase1_idx, phase2_idx, demo_idx) space.

Key design:
- 5 fixed minibatches of 10 repos (pre-partitioned, stratified by framework)
- Trial N uses batch[N % 5] — consistent (prompt, repo) → cache key
- n_startup_trials=20 random before TPE activates
- Study stored in SQLite for resumability
"""
from __future__ import annotations

import csv as csv_mod
import json
import random
import time
from pathlib import Path
from statistics import mean

import optuna
import pandas as pd
from rich.console import Console
from rich.table import Table

from harness.prompt_optimisation.bridge.cache import EvalCache
from harness.prompt_optimisation.bridge import parser as result_parser
from harness.prompt_optimisation.bridge import runner
from harness.prompt_optimisation.optimizer.bootstrap import load_demo_sets, run_bootstrap
from harness.prompt_optimisation.optimizer.metric import compute, compute_batch
from harness.prompt_optimisation.prompts.proposer import generate_candidates, load_candidates
from harness.prompt_optimisation.prompts.schema import DemoExample, InstructionCandidate, PromptConfig

console = Console()

_N_MINIBATCHES = 5
_MINIBATCH_SIZE = 10
_N_TRIALS = 180
_N_STARTUP = 20
_SEED = 42
_PARALLEL_HARNESS = 4
_NUM_RUNS = 3


def _partition_minibatches(
    train_rows: list[dict], seed: int = _SEED
) -> list[list[dict]]:
    """
    Stratified partition into 5 fixed batches of 10.
    Each batch gets a mix of frameworks.
    Saved to run_dir/minibatches.json for reproducibility.
    """
    by_fw: dict[str, list[dict]] = {}
    for r in train_rows:
        fw = str(r.get("FRAMEWORK", "other")).lower().strip()
        by_fw.setdefault(fw, []).append(r)

    rng = random.Random(seed)
    for fw in by_fw:
        rng.shuffle(by_fw[fw])

    batches: list[list[dict]] = [[] for _ in range(_N_MINIBATCHES)]
    # Round-robin fill across frameworks
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

    # Trim / pad to exactly MINIBATCH_SIZE
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


def run_search(
    run_dir: Path,
    training_csv: Path,
    parallel: int = _PARALLEL_HARNESS,
    num_runs: int = _NUM_RUNS,
    n_trials: int = _N_TRIALS,
    n_startup: int = _N_STARTUP,
    resume: bool = False,
) -> PromptConfig:
    """
    Full optimization run.

    Steps:
      1. Load train/val split from training_csv
      2. Bootstrap (or skip if run_dir/demo_sets.jsonl exists)
      3. Generate candidates (or skip if run_dir/candidates.jsonl exists)
      4. Partition minibatches
      5. Run Optuna TPE study
      6. Validate best on held-out validation rows
      7. Save best_prompt.json

    Returns best PromptConfig.
    """
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

    # ── Candidate generation ───────────────────────────────────────────────────
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

    n_p1 = len(p1_candidates)
    n_p2 = len(p2_candidates)
    n_ds = len(demo_sets)
    console.print(f"Search space: {n_p1}×{n_p2}×{n_ds} = {n_p1 * n_p2 * n_ds} combinations")

    # ── Minibatch partition ────────────────────────────────────────────────────
    mb_path = run_dir / "minibatches.json"
    if mb_path.exists() and not resume:
        minibatches = json.loads(mb_path.read_text())
    else:
        minibatches = _partition_minibatches(train_rows)
        mb_path.write_text(json.dumps(
            [[{k: v for k, v in r.items()} for r in b] for b in minibatches]
        ))

    # ── Cache ──────────────────────────────────────────────────────────────────
    cache = EvalCache(run_dir / "eval_cache.db")

    # ── Trial log ─────────────────────────────────────────────────────────────
    trial_log_path = run_dir / "trial_log.csv"
    trial_log_fields = ["trial", "p1_idx", "p2_idx", "demo_idx", "score", "duration_s", "cache_hits"]
    if not trial_log_path.exists():
        with trial_log_path.open("w", newline="") as f:
            csv_mod.DictWriter(f, fieldnames=trial_log_fields).writeheader()

    # ── Optuna study ───────────────────────────────────────────────────────────
    study_path = run_dir / "study.db"
    storage = f"sqlite:///{study_path}"
    study_name = "cwv_prompt_opt"

    sampler = optuna.samplers.TPESampler(n_startup_trials=n_startup, seed=_SEED)

    if resume and study_path.exists():
        study = optuna.load_study(study_name=study_name, storage=storage, sampler=sampler)
        console.print(f"[yellow]Resuming study ({len(study.trials)} existing trials)[/yellow]")
    else:
        try:
            study = optuna.create_study(
                study_name=study_name,
                direction="maximize",
                sampler=sampler,
                storage=storage,
            )
        except optuna.exceptions.DuplicatedStudyError:
            optuna.delete_study(study_name=study_name, storage=storage)
            study = optuna.create_study(
                study_name=study_name,
                direction="maximize",
                sampler=sampler,
                storage=storage,
            )

    def objective(trial: optuna.Trial) -> float:
        p1_idx = trial.suggest_int("phase1", 0, n_p1 - 1)
        p2_idx = trial.suggest_int("phase2", 0, n_p2 - 1)
        d_idx = trial.suggest_int("demos", 0, n_ds - 1)

        config = PromptConfig.build(
            phase1_text=p1_candidates[p1_idx].text,
            phase2_text=p2_candidates[p2_idx].text,
            demos=demo_sets[d_idx],
        )

        batch = minibatches[trial.number % _N_MINIBATCHES]
        trial_out_dir = run_dir / f"trial_{trial.number:04d}"

        scores = []
        cache_hits = 0
        t0 = time.time()

        for row in batch:
            cached = cache.get(config.config_hash, str(row["REPO_ID"]))
            if cached is not None:
                scores.append(cached)
                cache_hits += 1
                continue

            # Run harness for this single repo (batch of 1)
            results_dir = runner.run(
                config=config,
                rows=[row],
                out_dir=trial_out_dir / str(row["ID"]),
                parallel=1,
                num_runs=num_runs,
            )
            cwv = result_parser.parse_result(results_dir, str(row["REPO_ID"]), str(row["ID"]))
            score = compute(cwv)
            cache.put(config.config_hash, str(row["REPO_ID"]), score, {})
            scores.append(score)

        trial_score = mean(scores) if scores else -1.0
        duration = time.time() - t0

        # Log trial
        with trial_log_path.open("a", newline="") as f:
            csv_mod.DictWriter(f, fieldnames=trial_log_fields).writerow({
                "trial": trial.number,
                "p1_idx": p1_idx,
                "p2_idx": p2_idx,
                "demo_idx": d_idx,
                "score": round(trial_score, 4),
                "duration_s": round(duration, 1),
                "cache_hits": cache_hits,
            })

        console.print(
            f"  Trial {trial.number:3d} | p1={p1_idx:2d} p2={p2_idx:2d} d={d_idx} "
            f"| score={trial_score:+.3f} | cache={cache_hits}/{len(batch)} | {duration:.0f}s"
        )
        return trial_score

    study.optimize(objective, n_trials=n_trials)

    # ── Best config ────────────────────────────────────────────────────────────
    best = study.best_trial
    console.print(f"\n[bold green]Best trial {best.number}: score={best.value:.4f}[/bold green]")
    best_config = PromptConfig.build(
        phase1_text=p1_candidates[best.params["phase1"]].text,
        phase2_text=p2_candidates[best.params["phase2"]].text,
        demos=demo_sets[best.params["demos"]],
    )

    # ── Validation ────────────────────────────────────────────────────────────
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
    val_scores = [compute(r) for r in val_cwv]
    val_mean = mean(val_scores) if val_scores else -1.0

    val_summary = {
        "best_trial": best.number,
        "train_score": best.value,
        "validation_score": val_mean,
        "generalization_gap": best.value - val_mean,
        "val_scores": val_scores,
        "params": best.params,
    }
    (run_dir / "validation.json").write_text(json.dumps(val_summary, indent=2))

    gap = best.value - val_mean
    if gap > 0.15:
        console.print(f"[bold red]WARNING: generalization gap={gap:.3f} > 0.15 — consider more diverse training set[/bold red]")
    else:
        console.print(f"[green]Generalization gap={gap:.3f} ✓[/green]")

    # ── Save best prompt ───────────────────────────────────────────────────────
    best_path = run_dir / "best_prompt.json"
    best_path.write_text(json.dumps({
        "phase1_instruction": best_config.phase1_instruction,
        "phase2_instruction": best_config.phase2_instruction,
        "config_hash": best_config.config_hash,
        "train_score": best.value,
        "validation_score": val_mean,
        "params": best.params,
    }, indent=2))
    console.print(f"[bold]Saved best_prompt.json → {best_path}[/bold]")

    return best_config
