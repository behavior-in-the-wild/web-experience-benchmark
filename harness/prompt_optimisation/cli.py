"""
Command-line interface for the prompt optimization system.

Usage:
  po select-training-set
  po build-demo-pool RESULTS_DIR
  po optimize [--resume STUDY_DB] [--config CONFIG]
  po show --run RUN_ID
  po evaluate --run RUN_ID [--repos-csv CSV]
  po inject --run RUN_ID [--force]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="po", add_completion=False, pretty_exceptions_show_locals=False)
console = Console()

RUNS_DIR = Path(__file__).parent / "runs"
ROOT = Path(__file__).parent.parent.parent


@app.command("select-training-set")
def select_training_set(
    input_csv: Path = typer.Option(
        ROOT / "harness" / "SAMPLE" / "input.csv", help="Source CSV"
    ),
    output_csv: Path = typer.Option(
        Path(__file__).parent / "data" / "training_set.csv", help="Output path"
    ),
    train_size: int = typer.Option(50),
    val_size: int = typer.Option(10),
    seed: int = typer.Option(42),
) -> None:
    """Stratified sample of training + validation repos from input.csv."""
    from harness.prompt_optimisation.data.select_training_set import select
    df = select(input_csv, output_csv, train_size, val_size, seed)
    console.print(f"[green]✓[/green] {len(df)} rows → {output_csv}")


@app.command("build-demo-pool")
def build_demo_pool(
    results_dir: Path = typer.Argument(..., help="Past harness run results/ directory"),
    input_csv: Path = typer.Option(ROOT / "harness" / "SAMPLE" / "input.csv"),
    pool_file: Path = typer.Option(
        Path(__file__).parent / "data" / "demo_pool.jsonl"
    ),
    lcp_threshold: float = typer.Option(10.0, help="Min LCP delta %% to qualify"),
) -> None:
    """Mine a past harness results dir for successful demo examples."""
    from harness.prompt_optimisation.data.build_demo_pool import build
    n = build(results_dir, input_csv, pool_file, lcp_threshold)
    console.print(f"[green]✓[/green] {n} demos added → {pool_file}")


@app.command("optimize")
def optimize(
    training_csv: Path = typer.Option(
        Path(__file__).parent / "data" / "training_set.csv"
    ),
    n_trials: int = typer.Option(180, help="[optuna] Number of TPE trials"),
    n_generations: int = typer.Option(8, help="[gepa] Number of evolutionary generations"),
    parallel: int = typer.Option(4),
    num_runs: int = typer.Option(3),
    resume: Optional[Path] = typer.Option(None, help="Path to run dir or study.db to resume"),
    algo: str = typer.Option("gepa", help="Search algorithm: gepa | optuna"),
) -> None:
    """Run the full prompt optimization loop (bootstrap → propose → search → validate).

    --algo gepa   (default) Genetic-Pareto search with LLM reflection and
                  Pareto frontier tracking over (LCP, INP, CLS) deltas.

    --algo optuna Bayesian TPE search over (phase1_idx, phase2_idx, demo_idx)
                  discrete space. Faster for large candidate pools.
    """
    import time

    if not training_csv.exists():
        console.print(f"[red]training_set.csv not found: {training_csv}[/red]")
        console.print("Run: po select-training-set")
        raise typer.Exit(1)

    algo = algo.lower().strip()
    if algo not in ("gepa", "optuna"):
        console.print(f"[red]Unknown --algo '{algo}'. Choose: gepa | optuna[/red]")
        raise typer.Exit(1)

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Run directory:[/bold] {run_dir}")
    console.print(f"[bold]Algorithm:[/bold] {algo.upper()}")

    is_resume = False
    if resume:
        run_dir = resume.parent if resume.name in ("study.db", "gepa_trial_log.csv") else resume
        is_resume = True
        console.print(f"[yellow]Resuming from {run_dir}[/yellow]")

    if algo == "gepa":
        from harness.prompt_optimisation.optimizer.gepa_search import run_gepa
        best = run_gepa(
            run_dir=run_dir,
            training_csv=training_csv,
            parallel=parallel,
            num_runs=num_runs,
            n_generations=n_generations,
            resume=is_resume,
        )
    else:
        from harness.prompt_optimisation.optimizer.search import run_search
        best = run_search(
            run_dir=run_dir,
            training_csv=training_csv,
            parallel=parallel,
            num_runs=num_runs,
            n_trials=n_trials,
            resume=is_resume,
        )

    console.print(f"\n[bold green]Optimization complete.[/bold green]")
    console.print(f"Best prompt hash: {best.config_hash}")
    console.print(f"Results in: {run_dir}")


@app.command("show")
def show(
    run_id: str = typer.Option(..., "--run", help="Run timestamp, e.g. 20260521_140000"),
    pareto: bool = typer.Option(False, "--pareto", help="Show full Pareto frontier table"),
) -> None:
    """Print the winning phase1 and phase2 instruction text for a run."""
    best_path = RUNS_DIR / run_id / "best_prompt.json"
    if not best_path.exists():
        console.print(f"[red]best_prompt.json not found in runs/{run_id}[/red]")
        raise typer.Exit(1)

    best = json.loads(best_path.read_text())

    # Summary table
    summary = Table(title=f"Run {run_id}", show_header=False)
    summary.add_column("Key", style="cyan")
    summary.add_column("Value", justify="right")
    summary.add_row("Algorithm", best.get("algorithm", "optuna"))
    summary.add_row("Config hash", best.get("config_hash", "?"))
    summary.add_row("Train score", f"{best.get('train_score', 0.0):.4f}")
    summary.add_row("Val score", f"{best.get('validation_score', 0.0):.4f}")
    summary.add_row("Gap", f"{best.get('generalization_gap', 0.0):.4f}")
    if "pareto_objectives" in best:
        obj = best["pareto_objectives"]
        summary.add_row("LCP Δ (Pareto)", f"{obj['lcp']:+.4f}")
        summary.add_row("INP Δ (Pareto)", f"{obj['inp']:+.4f}")
        summary.add_row("CLS Δ (Pareto)", f"{obj['cls']:+.4f}")
        summary.add_row("Frontier size", str(best.get("frontier_size", "?")))
    console.print(summary)

    # Pareto frontier table (--pareto flag)
    if pareto:
        frontier_path = RUNS_DIR / run_id / "pareto_frontier.jsonl"
        if not frontier_path.exists():
            console.print("[yellow]No pareto_frontier.jsonl found (optuna run?)[/yellow]")
        else:
            table = Table(title="Pareto frontier", show_header=True)
            table.add_column("Hash", style="cyan", width=18)
            table.add_column("LCP Δ", justify="right")
            table.add_column("INP Δ", justify="right")
            table.add_column("CLS Δ", justify="right")
            table.add_column("Scalar", justify="right")
            table.add_column("Gen", justify="right")
            for line in frontier_path.read_text().splitlines():
                if not line.strip():
                    continue
                p = json.loads(line)
                table.add_row(
                    p["config_hash"],
                    f"{p['mean_lcp_delta']:+.3f}",
                    f"{p['mean_inp_delta']:+.3f}",
                    f"{p['mean_cls_delta']:+.3f}",
                    f"{p['scalar_score']:+.3f}",
                    str(p["generation"]),
                )
            console.print(table)

    console.rule("Phase 1 instruction")
    console.print(best["phase1_instruction"])
    console.rule("Phase 2 instruction")
    console.print(best["phase2_instruction"])


@app.command("evaluate")
def evaluate(
    run_id: str = typer.Option(..., "--run"),
    repos_csv: Optional[Path] = typer.Option(None, help="CSV of repos to evaluate (defaults to full input.csv minus training set)"),
    parallel: int = typer.Option(4),
    num_runs: int = typer.Option(3),
) -> None:
    """Evaluate the best prompt on a repo set and report aggregate metrics."""
    import time
    import pandas as pd
    from harness.prompt_optimisation.bridge import parser as result_parser
    from harness.prompt_optimisation.bridge import runner
    from harness.prompt_optimisation.optimizer.metric import compute
    from harness.prompt_optimisation.prompts.schema import PromptConfig

    best_path = RUNS_DIR / run_id / "best_prompt.json"
    if not best_path.exists():
        console.print(f"[red]best_prompt.json not found[/red]")
        raise typer.Exit(1)

    best = json.loads(best_path.read_text())
    config = PromptConfig(
        phase1_instruction=best["phase1_instruction"],
        phase2_instruction=best["phase2_instruction"],
        demos=[],
    )

    if repos_csv is None:
        # Default: full input.csv minus the training set
        input_df = pd.read_csv(ROOT / "harness" / "SAMPLE" / "input.csv")
        train_path = Path(__file__).parent / "data" / "training_set.csv"
        if train_path.exists():
            train_ids = set(pd.read_csv(train_path)["ID"].astype(str))
            eval_df = input_df[~input_df["ID"].astype(str).isin(train_ids)]
        else:
            eval_df = input_df
    else:
        eval_df = pd.read_csv(repos_csv)

    rows = eval_df.to_dict("records")
    console.print(f"Evaluating {len(rows)} repos …")

    ts = time.strftime("%Y%m%d_%H%M%S")
    eval_out = RUNS_DIR / run_id / f"eval_{ts}"
    results_dir = runner.run(config, rows, eval_out, parallel, num_runs, skip_visual=True)

    cwv_results = result_parser.parse_batch(results_dir, rows)
    scores = [compute(r) for r in cwv_results]
    valid_scores = [s for s in scores if s > -0.99]

    table = Table(title=f"Evaluation results — run {run_id}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Repos evaluated", str(len(rows)))
    table.add_row("Valid results", str(len(valid_scores)))
    table.add_row("Mean score", f"{mean(valid_scores):.4f}" if valid_scores else "—")
    table.add_row("Stdev", f"{stdev(valid_scores):.4f}" if len(valid_scores) > 1 else "—")
    table.add_row("% improved (score>0)", f"{100*sum(s>0 for s in valid_scores)/max(1,len(valid_scores)):.1f}%")
    console.print(table)

    (eval_out / "eval_summary.json").write_text(json.dumps({
        "run_id": run_id, "n_repos": len(rows),
        "n_valid": len(valid_scores),
        "mean_score": mean(valid_scores) if valid_scores else None,
        "scores": scores,
    }, indent=2))


@app.command("inject")
def inject_cmd(
    run_id: str = typer.Option(..., "--run"),
    force: bool = typer.Option(False, "--force", help="Bypass allow_permanent_inject guard"),
) -> None:
    """Permanently patch template_opencode_os.sh with winning prompts."""
    from harness.prompt_optimisation.inject import inject
    try:
        inject(run_id, force=force)
        console.print("[bold green]Template patched successfully.[/bold green]")
    except PermissionError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
