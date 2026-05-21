# Web Experience Benchmark

Benchmarking for Evaluating Web Experience (Core Web Vitals, etc)

[![Dataset](https://img.shields.io/badge/Dataset-HuggingFace-blue.svg)](https://huggingface.co/datasets/behavior-in-the-wild/cwv-bench-v0)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Research Overview

Large language models (LLMs) have shown significant progress on software engineering tasks, leading to the development of coding agents. However, current benchmarks like SWE-Bench and Polyglot are limited by their focus on small bug fixes (average 12 lines of code) and lack representation of web development—which constitutes 50% of software jobs and generates 40% of industry revenue.

Web performance optimization presents unique challenges compared to traditional bug-fixing: there are no predefined "correct" answers, solutions must address site-specific bottlenecks, and success is measured by continuous improvement in metrics like Largest Contentful Paint (LCP), Cumulative Layout Shift (CLS), and Interaction to Next Paint (INP).

**CWV-Bench** bridges this gap by evaluating coding agents on their ability to improve real website performance. Unlike traditional benchmarks that test against engineered test cases, CWV-Bench assesses whether agents can diagnose complex rendering pipeline bottlenecks, implement optimizations without introducing regressions, and reason about performance trade-offs in real-world scenarios.

## Installation

Tested on Ubuntu 24.04 LTS and macOS (Apple Silicon) with Python 3.12.

```bash
git clone https://github.com/behavior-in-the-wild/web-experience-benchmark.git
cd web-experience-benchmark

# Use Python 3.12 — 3.14+ is too new for some dependencies
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install tldextract pelican   # extra hosting deps not in requirements.txt
playwright install chromium

# bore tunnel (for PSI measurements)
cargo install bore-cli
```

### Framework hosting runtimes

Each framework requires its own runtime. Install only what you need:

**Node.js** (Express, React, Next.js, Vue, Hexo)
```bash
# macOS
brew install node

# Ubuntu
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

**Hugo + Go** (`host_hugo.sh`)
```bash
# macOS
brew install hugo go

# Ubuntu — use official Hugo binary (apt version is outdated)
sudo apt install -y golang-go
wget https://github.com/gohugoio/hugo/releases/latest/download/hugo_extended_linux_amd64.tar.gz
tar -xzf hugo_extended_linux_amd64.tar.gz && sudo mv hugo /usr/local/bin/
```

**Ruby + Jekyll** (`host_jekyll.sh`)
```bash
# macOS (system Ruby is read-only; use Homebrew)
brew install ruby
export PATH="/opt/homebrew/opt/ruby/bin:/opt/homebrew/lib/ruby/gems/$(ruby -e 'puts RUBY_VERSION.match(/^\d+\.\d+/)[0]').0/bin:$PATH"
gem install jekyll bundler

# Ubuntu
sudo apt install -y ruby-full build-essential
gem install jekyll bundler
```

> See [`harness/host_files/README.md`](harness/host_files/README.md) for the full hosting setup reference including a verification checklist.

> **Note:** `vllm` in `requirements.txt` is Linux/GPU only and will be skipped on macOS.

### Agent CLIs (install only what you need)

```bash
curl -fsSL https://opencode.ai/install | bash   # OpenCode
npm install -g @openai/codex                     # Codex
pip install aider-chat                           # Aider
```

## Harness

The benchmark harness evaluates coding agents on CWV optimization tasks. See [`harness/README.md`](harness/README.md) for full documentation.

```bash
# Run all agents on first 10 repos, 4 parallel jobs
./harness/evaluate.sh --parallel 4 --limit 10

# Patch-only mode (skip CWV measurement — fastest for agent testing)
SKIP_CWV_MEASURE=1 ./harness/evaluate.sh --parallel 8 --limit 50
```

Create `harness/.env` with your credentials:

```bash
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_DEPLOYMENT_NAME=gpt-4.1
GOOGLE_PAGESPEED_INSIGHTS_API_KEY=...   # for PSI measurements
```

### Available agents

`template_opencode_os.sh` (OS models), `template_opencode.sh`, `template_claudecode.sh`, `template_codex.sh`, `template_aider.sh`, `template_gemini.sh`, `template_null.sh`

### Output structure

Each job writes into its own subdirectory:

```
harness/out/<YYYYMMDD_HHMMSS>/results/
└── {ID}_{AGENT}/
    ├── {ID}_{AGENT}.patch          # code patch written by agent
    ├── agent.log                   # agent stdout/stderr
    ├── usage.json                  # token counts, cost, wall time
    ├── host.log                    # HTTP server logs
    ├── screenshot.png              # screenshot of patched site
    ├── visual.json                 # AI visual regression result
    ├── mobile.json                 # CWV metrics (mobile)
    ├── desktop.json                # CWV metrics (desktop)
    ├── init_psi_mobile.json        # PageSpeed before patch (if enabled)
    ├── init_psi_desktop.json
    ├── final_psi_mobile.json       # PageSpeed after patch (if enabled)
    └── final_psi_desktop.json
```

## Open-Source Models

Run the full benchmark suite against self-hosted open-source models via vLLM. Models are served one at a time with automatic GPU management.

```bash
# Run all 6 compatible models on 100 samples
bash harness/opensource_models/run_os_models.sh \
  --csv harness/SAMPLE/input_100.csv \
  2>&1 | tee harness/out/run_full.log

# Smoke test — 1 sample, all models
bash harness/opensource_models/run_os_models.sh \
  --csv harness/SAMPLE/input_1_test.csv \
  --parallel 1

# Single model
bash harness/opensource_models/run_os_models.sh gemma \
  --csv harness/SAMPLE/input_100.csv
```

Supported models (A100/SM80 compatible): `gemma-4-31b-it`, `glm-4.7-flash`, `qwen3-coder-next`, `gpt-oss-120b`, `devstral-2-123b`, `minimax-m2.7`

Token accounting in `usage/summary.json`:
- `prompt_tokens` — input tokens
- `completion_tokens` — non-reasoning output tokens
- `reasoning_tokens` — thinking tokens (separate)
- `total_tokens` — sum of all three

## Prompt Optimization

Automatic optimization of the agent's Phase 1 (planning) and Phase 2 (execution) prompts using Bayesian search over LLM-generated instruction candidates, scored by measured CWV improvements. See [`harness/prompt_optimisation/README.md`](harness/prompt_optimisation/README.md).

```bash
# From repo root
python -m harness.prompt_optimisation.cli select-training-set
python -m harness.prompt_optimisation.cli optimize --algo gepa
python -m harness.prompt_optimisation.cli show --run 20260521_140000
```

## Directory Structure

```
web-experience-benchmark/
├── harness/                          # Benchmark harness
│   ├── evaluate.sh                   # Main benchmark runner
│   ├── agents/                       # Agent templates
│   ├── host_files/                   # Framework hosting scripts
│   ├── scripts/                      # Utility scripts (rerun CSV, batch apply, etc.)
│   ├── opensource_models/            # vLLM serving + multi-model runner
│   ├── prompt_optimisation/          # MIPRO-style prompt optimization system
│   └── SAMPLE/                       # Input CSVs and repo snapshots
├── scripts/                          # Standalone analysis scripts
├── requirements.txt
└── README.md
```

## Dataset

`harness/SAMPLE/input.csv` — 503 web repositories pinned to specific commits.

| Column | Description |
|---|---|
| `ID` | Unique integer identifier |
| `REPO_ID` | `owner/repo` on GitHub |
| `FRAMEWORK` | e.g. `Jekyll`, `Express`, `Static HTML` |
| `COMMIT_ID` | Pinned commit SHA |
| `HOST_FILE_PATH` | Path to framework hosting script |
| `CWV_MOBILE` / `CWV_DESKTOP` | Baseline CWV JSON |

## Citation

```bibtex
@software{web_experience_benchmark_2025,
  title={{Towards Benchmarking and Optimizing Web Experiences}},
  author={{Behavior in the Wild}},
  year={2025},
  url={https://github.com/behavior-in-the-wild/web-experience-benchmark}
}
```
