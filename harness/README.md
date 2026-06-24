# Web Experience Benchmark — Harness

End-to-end agentic benchmarking pipeline. For each repo in `SAMPLE/input.csv` and each configured agent, the harness:

1. Clones the repo fresh from GitHub and checks out the pinned commit
2. Runs the agent to produce a CWV-optimization patch
3. Measures **initial PSI** (baseline, before the agent) and **final PSI** (after the patch) via bore.pub tunnels
4. Measures CWV with `cwv_benchmark.py` (Playwright-based, mobile + desktop)
5. Validates visually with `src/regression_tool/visual_validate.py` using structural DOM comparison, text similarity, screenshot judgment, and console-error diff

Jobs run in parallel (`--parallel N`), each on its own local port and bore tunnel.

---

## Prerequisites

### System packages
```bash
sudo apt-get update -qq && sudo apt-get install -y zip curl git
```

### bore (tunnel provider)
```bash
cargo install bore-cli
bore --version
```

### Python dependencies
```bash
cd /path/to/adobe/web-experience-benchmark
pip install -r requirements.txt   # or: pip install requests playwright
playwright install chromium
```

### Agent CLIs (install only what you need)
```bash
# OpenCode (recommended)
curl -fsSL https://opencode.ai/install | bash
opencode --version

# Codex
npm install -g @openai/codex
codex --version

# Aider
pip install aider-chat
```

---

## Environment variables

Create a `.env` file in this directory (it is sourced automatically):

```bash
# harness/.env

# Azure OpenAI — required for most agents and for cwv_benchmark.py AI analysis
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_DEPLOYMENT_NAME=gpt-4.1   # deployment name for the harness judge
AZURE_RESOURCE_NAME=your-resource           # required by opencode for Azure provider

# Google PSI — required for psi_report.py
GOOGLE_PAGESPEED_INSIGHTS_API_KEY=...

# Optional overrides (can also be passed inline on the command line)
PORT=4000          # base port; parallel jobs use PORT, PORT+1, PORT+2, …
NUM_RUNS=5         # CWV measurement runs per device
DEVICE=desktop     # not used directly by harness but forwarded to agents

# Temp directory for git clones and other mktemp calls.
# Must point to a filesystem with enough headroom for (parallel × ~500MB) of repo clones.
# Default: /dev/shm — a ~1TB tmpfs on the benchmark host.
# Change this if /dev/shm is unavailable or too small on your machine.
# WARNING: /tmp is backed by a ~75GB overlay FS; at parallel=32 it fills up within one
# model run (hundreds of simultaneous git clones), causing silent job failures.
HARNESS_TMPDIR=/dev/shm

# Optional — inject one audited suggestion per run (see “Suggestions file” below)
# SUGGESTIONS_FILE=/path/to/repo_cwv_suggestions_mobile.json
# SUGGESTION_INDICES=0,2    # comma-separated 0-based indices; omit for all suggestions
```

---

## Running the full pipeline

```bash
cd adobe/web-experience-benchmark/harness

# Serial, all rows with the default Codex config
./evaluate.sh --config configs/closed/codex.env

# 2 parallel jobs, first 4 rows
./evaluate.sh --config configs/closed/gpt-5.1-codex.env --parallel 2 --limit 4

# 4 parallel jobs, all rows, save log
./evaluate.sh --config configs/closed/gpt-5.1-codex.env --parallel 4 2>&1 | tee out/run_$(date +%Y%m%d_%H%M%S).log

# Patch-only mode (skip PSI / CWV / visual — fastest for agent testing)
SKIP_CWV_MEASURE=1 ./evaluate.sh --config configs/closed/codex.env --parallel 4 --limit 10
```

### Config-driven model routing

`evaluate.sh` is the single evaluation entrypoint for closed and open models. The selected config decides the agent template, model label, output label, and, for open models, vLLM serving parameters.

```bash
# Print the resolved config without running jobs
./evaluate.sh --config configs/open/gemma-4-31b-it.env --print-config

# Run an open model by starting vLLM inside evaluate.sh
./evaluate.sh --config configs/open/gemma-4-31b-it.env --serve-model --limit 1 --parallel 1

# Use an already-running OpenAI-compatible endpoint
MODEL_ENDPOINT=http://127.0.0.1:9000/v1 \
  ./evaluate.sh --config configs/open/gemma-4-31b-it.env --no-serve-model --limit 1

# Evaluate pre-existing patches in place
./evaluate.sh --config configs/closed/codex.env \
  --skip-agent --patch-results-dir closed_model_runs/codex/results \
  --limit 1 --skip-init-psi --skip-final-psi
```

Open-model suites use the single suite loop:

```bash
bash opensource_models/run_model_suite.sh \
  --config configs/suites/oss-models.env \
  --models gemma-4-31b-it \
  --limit 1 --parallel 1 --skip-all

bash opensource_models/run_model_suite.sh \
  --config configs/suites/scale-eval.env \
  --models qwen3.5-27b \
  --limit 1 --parallel 1 --skip-all
```

### Live Mirrors

Live-page benchmark rows use the same `evaluate.sh` pipeline with `SOURCE_MODE=live`. Instead of cloning GitHub, the live source loader copies a pre-fetched mirror from `MIRRORS_ROOT`.

```bash
# Fetch/update mirrors from the sample live JSONL
bash live/fetch_mirrors.sh --jsonl SAMPLE/live_filtered_top3.jsonl --limit 5

# Evaluate one mirrored live page with the live OpenCode template
HOST_SANDBOX=0 ./evaluate.sh \
  --config configs/closed/gpt-5.1-codex.env \
  --agent-template agents/template_live_opencode.sh \
  --source-config configs/sources/live.env \
  --limit 1 --parallel 1 \
  --skip-init-psi --skip-final-psi

# Print the resolved live config
./evaluate.sh \
  --config configs/closed/gpt-5.1-codex.env \
  --agent-template agents/template_live_opencode.sh \
  --source-config configs/sources/live.env \
  --print-config

# Same open-model suite, live source loader
bash opensource_models/run_model_suite.sh \
  --config configs/suites/oss-models.env \
  --source-config configs/sources/live.env \
  --models gemma-4-31b-it \
  --limit 1 --parallel 1 --skip-all
```

### Suggestions file (optional)

By default, `evaluate.sh` runs **once per CSV row per agent**, unchanged from earlier versions.

If you pass a JSON file whose top-level object includes a **`suggestions`** array (the same shape produced under `out/suggestions/…`, e.g. `*_cwv_suggestions_mobile.json`), the harness runs the **full pipeline once per selected suggestion index** for each row and agent: clone → baseline PSI → agent → patch → final PSI → CWV → visual.

```bash
# Every suggestion in the file × each row × each agent in AGENTS
./evaluate.sh --suggestions-file out/suggestions/20260320_163830/aamitn.github.io_cwv_suggestions_mobile.json

# Only suggestions at indices 0 and 3 (0-based)
./evaluate.sh --suggestions-file path/to/suggestions.json --suggestion-indices 0,3

# Same via environment (command-line flags override .env when both are set)
SUGGESTIONS_FILE=path/to/suggestions.json SUGGESTION_INDICES=0 ./evaluate.sh --limit 2
```

`--limit N` still limits **CSV rows**, not suggestion indices. Whitespace-only `SUGGESTIONS_FILE` in `.env` is treated as unset.

For each suggestion run, the harness writes one object to `eval_suggestion.json` in the job temp dir and sets **`EVAL_SUGGESTION_FILE`** and **`EVAL_SUGGESTION_INDEX`** for the agent. Result filenames use the prefix **`{ID}_s{N}_{AGENT}_`** instead of `{ID}_{AGENT}_`, and a copy of the input object is saved as **`{ID}_s{N}_{AGENT}_input_suggestion.json`**.

**Agent support:** `agents/template_opencodegpt51codex.sh` reads `EVAL_SUGGESTION_FILE` and appends that JSON (in a fenced block) to both the planning and execution prompts. Other agent templates do not append suggestions unless you add the same pattern.

### Selecting an agent

Use a config file for normal runs, or override it directly:

```bash
./evaluate.sh --config configs/closed/gpt-5.1-codex.env --parallel 2 --limit 4
./evaluate.sh --agent-template agents/template_codex.sh --model codex --parallel 2 --limit 4
```

### Custom base port

```bash
PORT=5000 ./evaluate.sh --parallel 4   # uses ports 5000–5003
```

---

## Output structure

Each run writes to `out/<YYYYMMDD_HHMMSS>/`.

**Filename prefix:** `{ID}_{AGENT}_` in the default harness. When `--suggestions-file` is used, artifacts for suggestion index `N` use **`{ID}_s{N}_{AGENT}_`** (agent basename without `.sh`, same as `{AGENT}` below).

```
out/20260405_141208/
├── run/                          # temporary per-job working dirs (deleted on success)
└── results/
    └── {ID}_{AGENT}/
        ├── agent.log              # agent stdout/stderr
        ├── phase1_prompt.txt      # OpenCode: full planning prompt (if agent writes it)
        ├── phase2_prompt.txt      # OpenCode: full execution prompt (if agent writes it)
        ├── input_suggestion.json  # only when suggestions-file mode is used
        ├── {ID}_{AGENT}.patch     # git diff produced by agent
        ├── usage.json             # tokens/cost/tool calls/wall time when available
        ├── init_psi_mobile.json   # PSI before patch, mobile
        ├── init_psi_desktop.json  # PSI before patch, desktop
        ├── final_psi_mobile.json  # PSI after patch, mobile
        ├── final_psi_desktop.json # PSI after patch, desktop
        ├── mobile.json            # CWV metrics, mobile
        ├── desktop.json           # CWV metrics, desktop
        ├── screenshot.png         # visual screenshot (patched)
        └── visual.json            # visual validation result
```

Replace `{ID}_{AGENT}_` with `{ID}_s{N}_{AGENT}_` when that job used suggestion index `N`.

### Quick result inspection
```bash
RUN=$(ls -t out | head -1)

# Full release metrics for one model/run folder
python3 scripts/compute_metrics.py "out/$RUN" --baseline-csv SAMPLE/input_100.csv
python3 scripts/compute_metrics.py "out/$RUN" --format json --json-out /tmp/metrics.json

# PSI performance score comparison (baseline vs patched)
python3 -c "
import json, glob, os
d = 'out/$RUN/results'
for f in sorted(glob.glob(f'{d}/*/init_psi_mobile.json')):
    base = os.path.basename(os.path.dirname(f))
    fin  = f.replace('/init_psi_mobile.json','/final_psi_mobile.json')
    init_s = json.load(open(f)).get('lighthouseResult',{}).get('categories',{}).get('performance',{}).get('score')
    fin_s  = json.load(open(fin)).get('lighthouseResult',{}).get('categories',{}).get('performance',{}).get('score') if os.path.exists(fin) else None
    print(f'{base:50s}  init={init_s}  final={fin_s}')
"

# Patch sizes
wc -l out/$RUN/results/*/*.patch
```

---

## Running individual components

### 1. Clone and checkout only
```bash
git clone https://github.com/USER/REPO.git /tmp/myrepo
git -C /tmp/myrepo checkout <commit_id>
```

### 2. Host a repo locally
```bash
# Pick the right script from host_files/ for the repo's framework:
PORT=4000 bash host_files/host_jekyll.sh /tmp/myrepo /tmp/host.log
PORT=4000 bash host_files/host_express.sh /tmp/myrepo /tmp/host.log
PORT=4000 bash host_files/host_static_html.sh /tmp/myrepo /tmp/host.log
# (see host_files/ for: host_hugo.sh, host_next.sh, host_react.sh,
#  host_vue.sh, host_hexo.sh, host_flask.sh, host_pelican.sh, host_quarto.sh)
```

### 3. Open a bore tunnel
```bash
RUST_LOG=info bore local 4000 --to bore.pub
# Output contains: "listening at bore.pub:XXXXX"
# Public URL: http://bore.pub:XXXXX
```

### 4. PSI report (single call)
```bash
python3 psi_report.py \
  --url  "http://bore.pub:12345/" \
  --strategy mobile \
  --output /tmp/psi_mobile.json

python3 psi_report.py \
  --url  "http://bore.pub:12345/" \
  --strategy desktop \
  --output /tmp/psi_desktop.json

# Requires: GOOGLE_PAGESPEED_INSIGHTS_API_KEY in env
# Output: raw PSI JSON (or {"error":"..."} on failure)
```

### 5. CWV benchmark (Playwright)
```bash
python3 ../src/cwv_tool/cwv_benchmark.py \
  --url "http://bore.pub:12345/" \
  --device mobile \
  --num-runs 3
```

### 6. Visual validation
```bash
python3 ../src/regression_tool/visual_validate.py \
  --url "http://bore.pub:12345/" \
  --screenshot-path /tmp/screenshot.png \
  --repo-id "USER/REPO" \
  --output-json /tmp/visual.json
```

### 7. Run an agent manually
```bash
# Export context the harness normally provides
export REPO_ID="USER/REPO"
export FRAMEWORK="jekyll"
export CWV_BASELINE_MOBILE='{"score":0.72,...}'
export CWV_BASELINE_DESKTOP='{"score":0.85,...}'

# Optional — same as a suggestions-file run (single object JSON, pretty-printed is fine)
# export EVAL_SUGGESTION_FILE="/tmp/one_suggestion.json"
# export EVAL_SUGGESTION_INDEX="0"

bash agents/template_opencodegpt51codex.sh \
  /tmp/myrepo \
  tasks/optimize_cwv_debug.txt \
  /tmp/agent.log \
  /tmp/agent.patch
```

---

## Dataset

`SAMPLE/input.csv` columns:

| Column | Description |
|---|---|
| `ID` | Unique integer identifier |
| `REPO_ID` | `owner/repo` on GitHub |
| `FRAMEWORK` | e.g. `Jekyll`, `Express`, `Static HTML` |
| `COMMIT_ID` | Pinned commit SHA (empty = HEAD) |
| `ZIP_REPO_PATH` | Legacy — ignored by current harness |
| `HOST_FILE_PATH` | Relative path to the host script, e.g. `host_files/host_jekyll.sh` |
| `CWV_MOBILE` | Baseline CWV JSON (mobile) |
| `CWV_DESKTOP` | Baseline CWV JSON (desktop) |
| `LCP_ENTRIES_*` | LCP element details per device |
| `CLS_SHIFTS_*` | CLS shift details per device |
| `INP_INTERACTIONS_*` | INP interaction details per device |
