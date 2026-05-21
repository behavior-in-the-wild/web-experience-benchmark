# Prompt Optimization System

Automatic optimization of the Phase 1 (planning) and Phase 2 (execution) prompt instructions
inside `harness/agents/template_opencode_os.sh` using Bayesian search over LLM-generated
instruction candidates, scored by measured Core Web Vitals improvements.

---

## 1. What this system does

The bash agent template contains two hardcoded instruction blocks:

- **Phase 1** — the planning prompt (read-only repo access, writes `plan.md`)
- **Phase 2** — the execution prompt (implements changes, writes patch)

Both are static text today. This system treats them as **tunable parameters** and uses an
optimization loop to find instruction variants that maximize measured CWV improvements
(LCP ↓, INP ↓, CLS ↓) across a diverse set of web repositories.

The method is a re-implementation of the core algorithm from
**MIPRO** (Khattab et al., 2024 — *Optimizing Instructions and Demonstrations for
Multi-Stage Language Model Programs*), wired to the actual bash harness instead of a
Python LM. The search algorithm is **Optuna TPE** (Tree-structured Parzen Estimator),
which is the same Bayesian optimizer used internally by DSPy's MIPROv2.

---

## 2. Literature alignment

| Decision | Literature basis |
|---|---|
| Bootstrap from unoptimized program | MIPRO (2024): rejection-sample traces above metric threshold θ; no ground-truth labels needed |
| Joint optimization of both phases | MIPRO (2024): joint assignment of instructions + demos across all stages; sequential/frozen-phase approach not recommended |
| TPE Bayesian search over discrete candidates | Hyperband-based BO for black-box prompt selection (2024): Bayesian optimization is consensus for expensive noisy oracles |
| Fixed minibatch cycling (not random resampling) | Avoids Goodhart: consistent (prompt, repo) signal per trial; Optuna model requires stable mapping from params → metric |
| Demo pool = successful bootstrap traces | MIPRO (2024): bootstrapped demos selected by end-to-end metric, not intermediate labels. Demos show *what success looks like*, not *how the prompt achieves it* — valid across prompt variants |
| Proposer quality matters | OPRO limitations study (2024): small proposers generate generic instructions regardless of scorer quality; mitigated here with rich meta-prompt + CWV-specific context |
| ~150 TPE trials for 1800-combination space | Literature: TPE competitive with random search in small discrete spaces; TPE kept for future-proofing and principled exploration |
| Distribution shift risk | Any-Shift Prompting (CVPR 2024): learned prompts overfit training distribution; mitigated by diverse training set + leave-one-framework-out validation |
| TextGrad deferred to Phase 2 | TEP paper (ICLR 2026): textual backpropagation in multi-step pipelines causes feedback growth; per-repo refinement requires harness measurement per iteration — expensive, not in initial scope |

---

## 3. Required template modification

One block must be added to `harness/agents/template_opencode_os.sh` near the top,
**before** the Phase 1 heredoc. This is the only change to the existing harness.

```bash
# ── Prompt override hook (used by prompt_optimisation/bridge/runner.py) ──
# When unset, falls through to the hardcoded defaults below — fully backward-compatible.
_PHASE1_INSTRUCTION="${PHASE1_INSTRUCTION:-}"
_PHASE2_INSTRUCTION="${PHASE2_INSTRUCTION:-}"
```

Then in each heredoc, the static instruction text is wrapped:

```bash
cat <<EOF > "$PLAN_PROMPT"
${_PHASE1_INSTRUCTION:-You are a Core Web Vitals optimization expert analyzing a $FRAMEWORK web application.

### Prompt: LCP, CLS, and INP for mobile and desktop
... (existing text) ...
}
EOF
```

The env vars `PHASE1_INSTRUCTION` and `PHASE2_INSTRUCTION` are set by `bridge/runner.py`
during optimization trials. When not set (all existing runs), the `${:-default}` expansion
produces the current hardcoded text unchanged.

`allow_permanent_inject: false` in `config.yaml` gates **`inject.py` only** — the script
that permanently bakes winning instructions into the template. It has no effect on trial-time
injection, which always uses the env-var mechanism above.

---

## 4. Folder structure

```
prompt_optimisation/
│
├── README.md                    ← this document
├── requirements.txt             ← new deps (optuna, pydantic, typer, rich, filelock, textgrad)
├── config.yaml                  ← all tunable hyperparameters
│
├── data/
│   ├── select_training_set.py   ← stratified 60-repo sample from harness/SAMPLE/input.csv
│   └── build_demo_pool.py       ← mine past harness run dirs for successful (plan,patch,delta) tuples
│
├── prompts/
│   ├── schema.py                ← Pydantic: PromptConfig, DemoExample, InstructionCandidate
│   ├── templates.py             ← baseline phase1 + phase2 text extracted verbatim from bash
│   └── proposer.py              ← Qwen generates K instruction variants; Jaccard deduplication
│
├── optimizer/
│   ├── metric.py                ← composite CWV score (optimization mode + validation mode)
│   ├── bootstrap.py             ← run baseline harness on training set, collect demo candidates
│   ├── search.py                ← Optuna TPE loop with fixed minibatch cycling
│   └── refine.py                ← [Phase 2] per-repo TextGrad-style refinement
│
├── bridge/
│   ├── runner.py                ← subprocess wrapper: writes prompt files, calls evaluate.sh
│   ├── parser.py                ← reads *_mobile.json, *_desktop.json, *_visual.json → dicts
│   └── cache.py                 ← thread-safe SQLite: (prompt_hash, repo_id) → CWVResult
│
├── inject.py                    ← permanently patches template (gated by allow_permanent_inject)
├── cli.py                       ← Typer CLI
│
└── runs/                        ← auto-created; one subdir per optimization run
    └── YYYYMMDD_HHMMSS/
        ├── study.db             ← Optuna study (resumable)
        ├── candidates.jsonl     ← all generated instruction variants (phase1 + phase2)
        ├── demo_sets.jsonl      ← 8 pre-built demo set combinations
        ├── best_prompt.json     ← winning PromptConfig
        ├── trial_log.csv        ← per-trial: indices, metric, repos evaluated, duration
        └── validation.json      ← held-out validation results
```

---

## 5. Data model (`prompts/schema.py`)

```
DemoExample
  repo_id           str        e.g. "owner/repo"
  framework         str        e.g. "jekyll"
  baseline_summary  str        condensed CWV snapshot (fits in ~200 tokens)
  plan_excerpt      str        first 60 lines of plan.md
  patch_excerpt     str        first 80 lines of git diff
  lcp_delta_pct     float      (baseline_lcp - result_lcp) / baseline_lcp × 100
  inp_delta_pct     float
  cls_delta_pct     float

InstructionCandidate
  phase             Literal["phase1", "phase2"]
  text              str        full instruction text with {FRAMEWORK}, {CWV_MOBILE} slots
  candidate_idx     int
  source            str        "baseline" | "proposed"

PromptConfig
  phase1_instruction  str      instruction text, demos already embedded (serialized as markdown)
  phase2_instruction  str
  demos               list[DemoExample]   the 3-5 demos embedded in phase1 text
  config_hash         str      sha256(phase1_instruction + phase2_instruction)
```

Demos are embedded in Phase 1 instruction text as a markdown `## Examples` section appended
after the main instruction. Phase 2 instruction does not include demos (execution is plan-driven).

---

## 6. Component specifications

### `data/select_training_set.py`

Reads `harness/SAMPLE/input.csv`. Stratifies by:

- Framework type: jekyll, hugo, express, static HTML, other
- Baseline LCP tier: poor (> 4 s), needs improvement (2.5–4 s), good (< 2.5 s)

Outputs `data/training_set.csv` with:
- 50 training repos (used in optimization trials)
- 10 validation repos (held out; never seen during Optuna search)

The 10 validation repos match the framework distribution of the full dataset.
A leave-one-framework-out check is run at the end to surface overfitting by framework.

---

### `data/build_demo_pool.py`

Accepts a past harness results directory (`out/<timestamp>/results/`).

For each repo in that run:
1. Reads `{ID}_opencode_os_mobile.json` and `{ID}_opencode_os_desktop.json`
2. Computes CWV delta against baseline from input CSV
3. If `lcp_delta_pct > 10` AND `visual.regression == false` AND patch length < 300 lines:
   keeps as a candidate demo
4. Reads `{ID}_opencode_os_plan.md` and `{ID}_opencode_os.patch`
5. Truncates to fit context window (plan: 60 lines; patch: 80 lines)
6. Appends to `data/demo_pool.jsonl`

Threshold (10% LCP improvement) is configurable in `config.yaml`.

---

### `prompts/templates.py`

Contains the current Phase 1 and Phase 2 instruction text as Python string constants,
extracted verbatim from `template_opencode_os.sh`. These are:

- `BASELINE_PHASE1` — the planning instruction (currently ~20 lines)
- `BASELINE_PHASE2` — the execution instruction (currently ~10 lines)

These serve as (a) the starting point for the proposer and (b) the fallback when no
override is set.

---

### `prompts/proposer.py`

Calls Qwen Coder on the vLLM endpoint (via `openai` SDK, using `OPENCODE_OPENAI_BASE_URL`
and `OPENAI_API_KEY` from `.env`).

**Inputs to the meta-prompt:**
- Task description (CWV optimization for web repos)
- Schema of available input variables (`{FRAMEWORK}`, `{CWV_MOBILE}`, etc.)
- 3 high-scoring demo examples from the pool (to show what success looks like)
- Current instruction text (as the baseline to vary from)
- Tip bank: ~10 CWV-specific rewriting hints (e.g., "prioritize sorting lcp_entries by
  renderTime before suggesting fixes", "name specific files rather than general patterns")

**Output:**
- K=15 instruction variant strings per phase, in one call (single prompt → JSON list)

**Deduplication:**
After generation, any pair of candidates with Jaccard token similarity > 0.85 has one
member discarded. This prevents near-duplicates from collapsing the effective search space.

**Quality note (from literature):**
Small proposer models generate generic instructions. Mitigation: the meta-prompt forces
Qwen to output CWV-specific variants by including real lcp_entries examples and requiring
each variant to mention at least one specific CWV mechanism (preload, lazy-load, image
format, explicit dimensions, etc.). Variants that fail this check are discarded.

---

### `optimizer/metric.py`

Single metric used for all stages — optimization trials and final validation alike.

```
lcp_delta  = (baseline_lcp  - result_lcp)  / max(baseline_lcp,  100)   # floor 100ms
inp_delta  = (baseline_inp  - result_inp)  / max(baseline_inp,  50)    # floor 50ms
cls_delta  = (baseline_cls  - result_cls)  / max(baseline_cls,  0.01)  # floor 0.01

score = clip(0.50*lcp_delta + 0.35*inp_delta + 0.15*cls_delta, -1.0, 1.0)

if visual.regression:
    score -= 1.0          # hard penalty for breaking the page
```

Mobile and desktop scores are averaged. Repos where CWV measurement failed entirely
(missing JSON, timeout) return `score = -1.0`.

**Rationale for floors:**
- Prevents division-by-zero for repos with near-zero baseline metrics
- Prevents artificially large deltas for repos already at good baseline values
- Consistent with common practice in relative improvement metrics

---

### `optimizer/bootstrap.py`

Runs the unmodified baseline prompt on all 50 training repos via `bridge/runner.py`.
This is a single harness call (not 50 individual calls) — `evaluate.sh` handles
parallelization internally.

Collects all runs where `score > config.bootstrap_threshold` (default: 0.10) as demo
candidates. These are passed to `data/build_demo_pool.py` logic to produce `demo_sets`.

**Why bootstrap demos transfer to modified prompts:**
Demos illustrate (framework, baseline metrics) → (effective plan, effective patch) mappings.
They are positive examples of *what a good output looks like*, not examples of *how the
current prompt achieves it*. Modified instructions change the model's reasoning approach
but the expected output structure (plan.md format, patch content) remains the same.
This is confirmed by MIPRO (2024): bootstrap traces are valid as long as end-to-end
metric passes, regardless of intermediate prompt phrasing.

**8 demo sets** are pre-built from the collected candidates:
- 2 sets of 3 demos (coverage-diverse: one per major framework)
- 3 sets of 4 demos (mixed quality: 2 high-delta + 2 medium-delta)
- 3 sets of 5 demos (high-volume: all from same framework cluster)

These are fixed before the Optuna loop starts and stored in `runs/{ts}/demo_sets.jsonl`.

---

### `optimizer/search.py`

This is the core optimization loop.

**Search space:**
```
phase1_idx  ∈ {0 … K-1}     K ≤ 15 phase1 instruction variants
phase2_idx  ∈ {0 … K-1}     K ≤ 15 phase2 instruction variants
demo_idx    ∈ {0 … 7}        8 pre-built demo sets
```

Total combinations: up to 15 × 15 × 8 = 1800. Optuna TPE explores ~150–200 of them.

**Fixed minibatch cycling:**

Before the study starts, the 50 training repos are partitioned into 5 fixed batches of
10 repos each (stratified by framework to ensure each batch has coverage). This partition
is stored in `runs/{ts}/minibatches.json` and never changes.

Trial N evaluates on `batch[N % 5]`. Every 5 consecutive trials together cover all 50
training repos exactly once.

**Why fixed batches (not random resampling):**
Random resampling means the same (prompt, repo) pair may appear in multiple trials with
different trial metrics, breaking Optuna's assumption that `f(x)` is consistent. Fixed
batches make each (prompt, repo) evaluation unique and cacheable. The per-trial metric
is the mean over 10 repos in that batch — Optuna's TPE fits a surrogate to these
per-trial values, which are stable.

**Trial execution:**

```
def objective(trial):
    p1_idx   = trial.suggest_int("phase1", 0, n_phase1_candidates - 1)
    p2_idx   = trial.suggest_int("phase2", 0, n_phase2_candidates - 1)
    d_idx    = trial.suggest_int("demos",  0, n_demo_sets - 1)

    config = build_prompt_config(
        phase1_text = candidates_phase1[p1_idx],
        phase2_text = candidates_phase2[p2_idx],
        demos       = demo_sets[d_idx],
    )
    # build_prompt_config embeds the demos as a markdown section appended to phase1_text

    batch_repos = minibatches[trial.number % 5]
    scores = []
    for repo_id in batch_repos:
        cached = cache.get(config.config_hash, repo_id)
        if cached is not None:
            scores.append(cached)
            continue
        result = runner.run_single(config, repo_id)
        score  = metric.compute(result)
        cache.put(config.config_hash, repo_id, score, result)
        scores.append(score)

    return float(mean(scores))
```

Runner calls within a trial run in a subprocess pool (default: 4 parallel slots within
the trial, not to overwhelm the GPU serving Qwen).

**Optuna configuration:**
```python
sampler = optuna.samplers.TPESampler(
    n_startup_trials=20,    # first 20 trials are random (warms up the surrogate)
    seed=42,
)
study = optuna.create_study(
    direction="maximize",
    sampler=sampler,
    storage=f"sqlite:///runs/{ts}/study.db",    # resumable
)
study.optimize(objective, n_trials=180, timeout=None)
```

`n_startup_trials=20` ensures the surrogate is not fit on too few points. The first 20
trials are random search; TPE takes over from trial 21 onward.

**Resumability:** The study is persisted in `study.db`. Interrupted runs can be resumed
with `po optimize --resume runs/{ts}/`.

**Post-search validation:**

After 180 trials, the best `PromptConfig` is evaluated on the held-out 10 validation repos
using the same metric and harness flags as the trials (`--skip-init-psi --skip-final-psi`).

Generalization gap = `validation_score - best_training_score`. If gap > 0.15, a warning
is printed suggesting the training set needs more diversity.

Leave-one-framework-out: validation is also re-run with each framework's repos excluded
from the training set to check framework-specific overfitting.

---

### `bridge/runner.py`

Wraps a single call to `harness/evaluate.sh`.

**Inputs:**
- `prompt_config: PromptConfig`
- `repo_ids: list[str]`
- `skip_visual: bool = True`     (visual validation disabled during trials for speed)
- `num_runs: int = 3`            (CWV measurement runs per repo; median returned)

**Steps:**
1. Write `phase1_instruction` text to a temp file `{tmpdir}/phase1.txt`
2. Write `phase2_instruction` text to a temp file `{tmpdir}/phase2.txt`
3. Build a filtered CSV containing only the target `repo_ids` (subset of training set)
4. Construct the command:
   ```
   PHASE1_INSTRUCTION={contents}  \
   PHASE2_INSTRUCTION={contents}  \
   bash harness/evaluate.sh       \
     --input-csv {filtered_csv}   \
     --agents opencode_os         \
     --parallel 4                 \
     --skip-init-psi              \
     --skip-final-psi             \
     [--skip-visual]              \   # when skip_visual=True
     --num-runs {num_runs}
   ```
5. Run subprocess, capture exit code
6. Return path to results directory

**Note on env vars vs. files:**
The instruction text can be long (500-1000 tokens). Passing directly as env var risks
`E2BIG` (argument list too long). Runner writes to temp file and sets env var to the
**file path**. The template reads with `${:-cat file}` pattern. Temp files are cleaned
up after the subprocess exits.

---

### `bridge/parser.py`

Given a results directory and a list of repo IDs, reads:

- `{ID}_opencode_os_mobile.json`   → `LCP_mean`, `INP_mean`, `CLS_mean`
- `{ID}_opencode_os_desktop.json`  → same
- `{ID}_opencode_os_visual.json`   → `regression: bool`

Returns a `CWVResult` dataclass per repo. Missing files return `None` fields; the metric
function treats these as `score = -1.0`.

---

### `bridge/cache.py`

SQLite database at `runs/{ts}/eval_cache.db`.

Schema:
```sql
CREATE TABLE results (
    prompt_hash  TEXT NOT NULL,
    repo_id      TEXT NOT NULL,
    score        REAL NOT NULL,
    result_json  TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY (prompt_hash, repo_id)
);
```

Wrapped with `filelock` for thread safety (multiple subprocess trials may write
concurrently). Cache is scoped to the run directory — a new optimization run starts with
an empty cache.

---

### `inject.py`

Reads `runs/{run_id}/best_prompt.json`. Modifies
`harness/agents/template_opencode_os.sh`:

1. Creates backup: `template_opencode_os.sh.bak`
2. Replaces the Phase 1 heredoc instruction text with the optimized version
3. Replaces the Phase 2 instruction text
4. Writes `runs/{run_id}/inject_record.json` with timestamp and diff

Only runs when `config.allow_permanent_inject: true` OR `--force` flag is passed.
Default is `allow_permanent_inject: false` — **the template is never permanently modified
until explicitly requested**.

---

### `optimizer/refine.py` (Phase 2 — not in initial scope)

Per-repo refinement for repos where the optimized prompt yields < 5% improvement.

Each iteration:
1. Build PromptConfig from best result of search
2. Run harness on the specific repo → measure CWV delta
3. Call Qwen critic with: current instruction + plan.md + patch + measured delta
4. Critic outputs: "What specific change to the Phase 2 instruction would have
   improved this result? Be concrete about the CWV mechanism."
5. Mutate the Phase 2 instruction with the feedback
6. Repeat up to 3 iterations; keep the iteration with best measured CWV delta

**Why iterations require harness runs:**
Unlike text classification tasks where TextGrad can compare outputs directly, CWV
improvement cannot be estimated from the patch text alone — it requires actual browser
measurement. Each refinement iteration therefore runs the harness once.
Cost: ~$2-3 per iteration × 3 iterations × N repos.

---

## 7. CLI commands (`cli.py`)

```
po select-training-set
    Stratified sample from harness/SAMPLE/input.csv.
    Outputs: data/training_set.csv

po build-demo-pool RESULTS_DIR
    Mine a past harness run dir for successful demos.
    Appends to: data/demo_pool.jsonl

po optimize [--resume STUDY_DB] [--config CONFIG_YAML]
    Run the full optimization loop.
    Steps: bootstrap → propose → search → validate → save best_prompt.json

po show --run YYYYMMDD_HHMMSS
    Print the winning phase1 and phase2 instruction text.

po evaluate --run YYYYMMDD_HHMMSS [--repos-csv CSV]
    Evaluate best_prompt on a repo set (default: full input.csv minus training set).
    Reports aggregate LCP/INP/CLS improvement + per-framework breakdown.

po inject --run YYYYMMDD_HHMMSS [--force]
    Permanently patch template_opencode_os.sh with winning prompts.
    Requires allow_permanent_inject: true in config OR --force.
```

---

## 8. `config.yaml` schema

```yaml
# ── Harness ──────────────────────────────────────────────────────────────────
harness:
  evaluate_script: harness/evaluate.sh
  target_template: harness/agents/template_opencode_os.sh
  agent_name: opencode_os
  parallel: 4                        # parallel jobs within a trial
  num_runs: 3                        # CWV measurement runs per repo (median used)
  allow_permanent_inject: false      # gates inject.py only; does NOT affect trial injection

# ── Training data ─────────────────────────────────────────────────────────────
data:
  input_csv: harness/SAMPLE/input.csv
  training_set_csv: prompt_optimisation/data/training_set.csv
  demo_pool_jsonl: prompt_optimisation/data/demo_pool.jsonl
  train_size: 50
  validation_size: 10
  frameworks: [jekyll, hugo, express, static, other]

# ── Proposer ─────────────────────────────────────────────────────────────────
proposer:
  base_url: "${OPENCODE_OPENAI_BASE_URL}"    # from .env
  api_key: "${OPENAI_API_KEY}"               # from .env
  model: "${VLLM_SERVED_MODEL_NAME}"         # e.g. qwen3-coder-next
  n_phase1_candidates: 15
  n_phase2_candidates: 15
  temperature: 0.9                           # high for diversity
  dedup_threshold: 0.85                      # Jaccard similarity cutoff

# ── Demo sets ────────────────────────────────────────────────────────────────
demos:
  n_demo_sets: 8
  demos_per_set: [3, 3, 4, 4, 4, 5, 5, 5]  # sizes for each of the 8 sets
  bootstrap_threshold: 0.10                  # min score for a trace to qualify as demo

# ── Optimization ─────────────────────────────────────────────────────────────
optimization:
  n_trials: 180
  n_startup_trials: 20            # random before TPE kicks in
  minibatch_size: 10              # repos per trial (5 fixed batches of 10)
  seed: 42

# ── Metric ───────────────────────────────────────────────────────────────────
metric:
  # Floors prevent division-by-zero and cap deltas for "already-good" baselines
  lcp_floor_ms: 100
  inp_floor_ms: 50
  cls_floor: 0.01
  regression_penalty: -1.0

  weights:
    lcp: 0.50
    inp: 0.35
    cls: 0.15

# ── Refinement (Phase 2) ─────────────────────────────────────────────────────
refine:
  enabled: false
  min_improvement_threshold: 0.05
  max_iterations: 3
  max_repos: 30
```

---

## 9. End-to-end data flow

```
harness/SAMPLE/input.csv
        │
        ▼
data/select_training_set.py
        │
        ├──► data/training_set.csv  (50 train + 10 validation repos)
        │
        ▼
optimizer/bootstrap.py
        │  runs harness/evaluate.sh on 50 training repos
        │  with baseline (unmodified) prompt
        │
        ├──► data/demo_pool.jsonl   (successful traces: score > 0.10)
        │
        ▼
prompts/proposer.py
        │  calls Qwen on vLLM endpoint
        │  generates 15 phase1 + 15 phase2 instruction variants
        │  Jaccard-deduplicates near-duplicates
        │
        ├──► runs/{ts}/candidates.jsonl
        ├──► runs/{ts}/demo_sets.jsonl  (8 pre-built combinations from pool)
        │
        ▼
optimizer/search.py  (Optuna TPE, 180 trials)
        │
        │  each trial:
        │    sample (phase1_idx, phase2_idx, demo_idx) via TPE
        │    build PromptConfig (embed demos into phase1 text)
        │    bridge/runner.py → harness/evaluate.sh
        │      └─ PHASE1_INSTRUCTION={file}, PHASE2_INSTRUCTION={file}
        │         --skip-init-psi --skip-final-psi --skip-visual
        │    bridge/parser.py → CWVResult per repo
        │    bridge/cache.py  → store (prompt_hash, repo_id) → score
        │    optimizer/metric.py → optimization score
        │    report score to Optuna
        │
        ├──► runs/{ts}/study.db      (resumable Optuna study)
        ├──► runs/{ts}/trial_log.csv
        │
        ▼
  best PromptConfig from study.best_trial
        │
        ▼
  final validation (10 held-out repos, same flags as trials)
        │
        ├──► runs/{ts}/best_prompt.json
        ├──► runs/{ts}/validation.json
        │
        ▼  (only when allow_permanent_inject: true OR --force)
  inject.py
        │
        └──► harness/agents/template_opencode_os.sh  (patched)
             harness/agents/template_opencode_os.sh.bak
```

---

## 10. Cost and time estimate

| Phase | Harness runs | Wall time | Approx cost |
|---|---|---|---|
| Bootstrap (50 repos, baseline) | 50 | ~2 hrs | ~$25 |
| Candidate generation (Qwen proposer) | 0 | 5 min | ~$0.10 |
| Optimization (180 trials × 10 repos/trial, cached) | ≤ 1800 | 5–8 hrs | ≤ $90 |
| Final validation (10 repos) | 10 | ~25 min | ~$5 |
| **Total** | **≤ 1860** | **~8–11 hrs** | **~$120** |

Cache eliminates re-runs for repeated (prompt, repo) pairs. In practice, TPE converges
toward a region of the search space, so the effective number of unique (prompt, repo)
evals is ~800–1000 rather than 1800.

---

## 11. Known risks and mitigations

| Risk | Mitigation |
|---|---|
| Proposer (Qwen) generates generic instructions | Rich meta-prompt with CWV-specific context; require each variant to mention at least one specific CWV mechanism; discard variants that don't |
| Overfitting training set (50 repos) | 10-repo validation + leave-one-framework-out check; warn if generalization gap > 0.15 |
| CWV measurement noise (±10-15%) | 3 runs per repo, median used; noise floor in delta calculation |
| Bootstrap demos don't transfer to new prompts | Confirmed valid by MIPRO (2024): demos show *what success looks like*, not *how prompt achieves it* |
| Template modification breaks existing runs | Override hook uses `${:-default}` — no env var set = original behavior, fully backward-compatible |
| TextGrad feedback growth in multi-step pipeline | Deferred to Phase 2; per-iteration harness measurement prevents unchecked feedback accumulation |
