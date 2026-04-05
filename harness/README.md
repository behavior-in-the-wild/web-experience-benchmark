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

Each run writes to `out/<YYYYMMDD_HHMMSS>/`:

```
out/20260405_141208/
├── run/                          # temporary per-job working dirs (deleted on success)
└── results/
    ├── {ID}_{AGENT}_agent.log          # agent stdout/stderr
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
