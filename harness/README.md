# Web Experience Benchmark — Harness

End-to-end agentic benchmarking pipeline. For each repo in `SAMPLE/input.csv` and each configured agent, the harness:

1. Clones the repo fresh from GitHub and checks out the pinned commit
2. Runs the agent to produce a CWV-optimization patch
3. Measures **initial PSI** (baseline, before the agent) and **final PSI** (after the patch) via bore.pub tunnels
4. Measures CWV with `cwv_benchmark.py` (Playwright-based, mobile + desktop)
5. Validates visually with `visual_validate.py` (screenshot + AI eval)

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

# Optional — inject one audited suggestion per run (see “Suggestions file” below)
# SUGGESTIONS_FILE=/path/to/repo_cwv_suggestions_mobile.json
# SUGGESTION_INDICES=0,2    # comma-separated 0-based indices; omit for all suggestions
```

---

## Running the full pipeline

```bash
cd adobe/web-experience-benchmark/harness

# Serial, all rows
./evaluate.sh

# 2 parallel jobs, first 4 rows
./evaluate.sh --parallel 2 --limit 4

# 4 parallel jobs, all rows, save log
./evaluate.sh --parallel 4 2>&1 | tee out/run_$(date +%Y%m%d_%H%M%S).log

# Patch-only mode (skip PSI / CWV / visual — fastest for agent testing)
SKIP_CWV_MEASURE=1 ./evaluate.sh --parallel 4 --limit 10
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

`--limit N` still limits **CSV rows**, not suggestion indices. Whitespace-only `SUGGESTIONS_FILE` in `.env` is treated as unset (legacy behavior).

For each suggestion run, the harness writes one object to `eval_suggestion.json` in the job temp dir and sets **`EVAL_SUGGESTION_FILE`** and **`EVAL_SUGGESTION_INDEX`** for the agent. Result filenames use the prefix **`{ID}_s{N}_{AGENT}_`** instead of `{ID}_{AGENT}_`, and a copy of the input object is saved as **`{ID}_s{N}_{AGENT}_input_suggestion.json`**.

**Agent support:** `agents/template_opencodegpt51codex.sh` reads `EVAL_SUGGESTION_FILE` and appends that JSON (in a fenced block) to both the planning and execution prompts. Other agent templates do not append suggestions unless you add the same pattern.

### Generate suggestions, then evaluate (combined)

Use `scripts/01_generate_suggestions.sh` (single-entry mode, **not** `--all`) to run `cwv-optimizer suggest` on one dataset row. On success it prints **only** the absolute path to the new `*_cwv_suggestions_{mobile|desktop}.json` on **stdout** (progress and errors go to **stderr**), which is meant to be captured and passed to `evaluate.sh`.

**Prerequisites:** `cwv-optimizer` on your `PATH` (install the repo package from the project root, e.g. `pip install -e .`), plus the API keys and tunnel setup that `cwv-optimizer suggest` expects—same as running `01_generate_suggestions.sh` on its own. `evaluate.sh` still needs `harness/.env` for agents and PSI as usual.

**Align inputs:** `--hf-index` must be the CSV row you want analyzed, `--framework` must match that row’s `FRAMEWORK` column, and `--limit` on `evaluate.sh` should include that row (e.g. row `0` → `--limit 1`). Example for the first row of `SAMPLE/input.csv` (`Express`, id `101`):

```bash
cd adobe/web-experience-benchmark/harness

SUGG=$(./scripts/01_generate_suggestions.sh \
  --dataset SAMPLE/input.csv \
  --hf-index 0 \
  --framework Express \
  --device mobile) \
  && ./evaluate.sh --suggestions-file "$SUGG" --limit 1
```

One line (same assumptions):

```bash
cd adobe/web-experience-benchmark/harness && ./evaluate.sh --suggestions-file "$(./scripts/01_generate_suggestions.sh --dataset SAMPLE/input.csv --hf-index 0 --framework Express --device mobile)" --limit 1
```

Optional: add `./evaluate.sh` flags such as `--parallel 2`, `--suggestion-indices 0,1`, or `SKIP_CWV_MEASURE=1` after the suggestions path is known. Batch suggestion generation (`01_generate_suggestions.sh --all`) does not emit one path per repo on stdout; for those runs, point `evaluate.sh` at the JSON files under `out/suggestions/<timestamp>/` by path instead.

### Selecting an agent

Edit the `AGENTS` array near the top of `evaluate.sh`:

```bash
AGENTS=(
  # "agents/template_null.sh"
  # "agents/template_codex.sh"          # requires: npm install -g @openai/codex
  # "agents/template_aider.sh"
  # "agents/template_opencode.sh"       # OpenCode with OPENCODE_MODEL
  "agents/template_opencodegpt51codex.sh"   # OpenCode hard-wired to gpt-5.1-codex
  # "agents/template_gemini.sh"
  # "agents/template_claudecode.sh"
  # "agents/template_cwvoptimizer.sh"
)
```

### Setting the model (OpenCode agents)

```bash
# template_opencode.sh reads OPENCODE_MODEL
OPENCODE_MODEL=azure/gpt-4.1 ./evaluate.sh --parallel 2 --limit 4

# Other valid values:
#   openai/gpt-4o
#   azure/gpt-5
#   openrouter/moonshotai/kimi-k2
#   302ai/glm-4.7
```

`template_opencodegpt51codex.sh` has `gpt-5.1-codex` hardcoded — no env var needed.

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
    ├── {ID}_{AGENT}_agent.log          # agent stdout/stderr
    ├── {ID}_{AGENT}_phase1_prompt.txt  # OpenCode: full planning prompt (if agent writes it)
    ├── {ID}_{AGENT}_phase2_prompt.txt  # OpenCode: full execution prompt (if agent writes it)
    ├── {ID}_{AGENT}_input_suggestion.json   # only when suggestions file mode: JSON passed to the agent
    ├── {ID}_{AGENT}.patch              # git diff produced by agent
    ├── {ID}_{AGENT}_init_host.log      # baseline HTTP server log
    ├── {ID}_{AGENT}_init_bore.log      # baseline bore tunnel log
    ├── {ID}_{AGENT}_init_psi_mobile.json    # PSI before patch, mobile
    ├── {ID}_{AGENT}_init_psi_desktop.json   # PSI before patch, desktop
    ├── {ID}_{AGENT}_host.log           # patched HTTP server log
    ├── {ID}_{AGENT}_bore.log           # patched bore tunnel log
    ├── {ID}_{AGENT}_final_psi_mobile.json   # PSI after patch, mobile
    ├── {ID}_{AGENT}_final_psi_desktop.json  # PSI after patch, desktop
    ├── {ID}_{AGENT}_mobile.json        # CWV metrics, mobile
    ├── {ID}_{AGENT}_desktop.json       # CWV metrics, desktop
    ├── {ID}_{AGENT}_cwv_stderr.txt     # cwv_benchmark.py stderr
    ├── {ID}_{AGENT}_screenshot.png     # visual screenshot (patched)
    └── {ID}_{AGENT}_visual.json        # visual validation result
```

Replace `{ID}_{AGENT}_` with `{ID}_s{N}_{AGENT}_` when that job used suggestion index `N`.

### Quick result inspection
```bash
RUN=$(ls -t out | head -1)

# PSI performance score comparison (baseline vs patched)
python3 -c "
import json, glob, os
d = 'out/$RUN/results'
for f in sorted(glob.glob(f'{d}/*_init_psi_mobile.json')):
    base = os.path.basename(f).replace('_init_psi_mobile.json','')
    fin  = f.replace('_init_psi_mobile','_final_psi_mobile')
    init_s = json.load(open(f)).get('lighthouseResult',{}).get('categories',{}).get('performance',{}).get('score')
    fin_s  = json.load(open(fin)).get('lighthouseResult',{}).get('categories',{}).get('performance',{}).get('score') if os.path.exists(fin) else None
    print(f'{base:50s}  init={init_s}  final={fin_s}')
"

# Patch sizes
wc -l out/$RUN/results/*.patch
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
python3 ../scripts/helper_scripts/cwv_benchmark.py \
  --url "http://bore.pub:12345/" \
  --device mobile \
  --num-runs 3
```

### 6. Visual validation
```bash
python3 visual_validate.py \
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
