# Web Experience Benchmark

Benchmarking for Evaluating Web Experience (Core Web Vitals, etc)

[![Dataset](https://img.shields.io/badge/Dataset-HuggingFace-blue.svg)](https://huggingface.co/datasets/behavior-in-the-wild/cwv-bench-v0)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Dataset

This project uses a comprehensive benchmark dataset of 503 web repositories for evaluating Core Web Vitals optimization:

## Research Overview

Large language models (LLMs) have shown significant progress on software engineering tasks, leading to the development of coding agents. However, current benchmarks like SWE-Bench and Polyglot are limited by their focus on small bug fixes (average 12 lines of code) and lack representation of web development—which constitutes 50% of software jobs and generates 40% of industry revenue.

Web performance optimization presents unique challenges compared to traditional bug-fixing: there are no predefined "correct" answers, solutions must address site-specific bottlenecks, and success is measured by continuous improvement in metrics like Largest Contentful Paint (LCP), Cumulative Layout Shift (CLS), and accessibility scores.

**CWV-Bench** bridges this gap by evaluating coding agents on their ability to improve real website performance and user experience. Unlike traditional benchmarks that test against engineered test cases, CWV-Bench assesses whether agents can:

- Diagnose complex rendering pipeline bottlenecks
- Implement optimizations without introducing regressions
- Reason about performance trade-offs in real-world scenarios

This enables evaluation of genuine agent capabilities rather than retrieval of memorized solutions, addressing the critical gap between benchmark performance (~90%) and real-world effectiveness (25-30%).

## Installation

This guide details how to set up and run the Core Web Vitals (CWV) Agent demo end-to-end on a **Linux machine**.
Has been tested on Ubuntu 24.04 LTS.

Set up the directory, clone the repository, and install Python and Node.js dependencies.

```bash
# Create directory and clone the repository
mkdir demo && cd demo
git clone https://github.com/behavior-in-the-wild/web-experience-benchmark.git
cd web-experience-benchmark

# Set up Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Node.js + npm
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Install Python dependencies 
pip install -e .
pip install datasets

# Playwright
pip install playwright
sudo .venv/bin/python -m playwright install-deps
playwright install
```

```bash
# --- LiteLLM / Aider (REQUIRED) ---
AZURE_API_KEY=
AZURE_API_BASE=
AZURE_API_VERSION=
AZURE_DEPLOYMENT=

# --- Aider Models (Force) ---
AIDER_MODEL=
AIDER_EDITOR_MODEL=
AIDER_WEAK_MODEL=

# --- CWV Optimizer Defaults ---
LOG_LEVEL=INFO
DEFAULT_MODEL=azure/gpt-4.1
CWV_MODEL=azure/gpt-4.1
TEMPERATURE=0.0

# --- Testing Configuration ---
DEVICE=mobile
HEADLESS=true
NUM_RUNS=3

# --- Optional / Local Proxy (Safe to keep) ---
ANTHROPIC_BASE_URL=http://localhost:4000
ANTHROPIC_API_KEY=dummy
```

## Baseline Implementation (Optional)

For implementing the baseline suggestions provided in the dataset, you'll need additional setup.

### Google CrUX API Setup

**Getting Google CrUX API Credentials:**

- Go to [Google Cloud Console](https://console.cloud.google.com/)
- Enable "Chrome UX Report API" in APIs & Services → Library
- Create an API key in APIs & Services → Credentials
- Optionally enable "PageSpeed Insights API"

### Baseline Installation

```bash
npm install
```

### Baseline Configuration

Create a `.env` file in the `cwv-agent/` directory with (as in .env.example):

```bash
# --- Google API Keys (for CWV analysis) ---
GOOGLE_CRUX_API_KEY=your_crux_api_key_here
GOOGLE_PAGESPEED_INSIGHTS_API_KEY=your_psi_api_key_here

# --- Azure OpenAI (REQUIRED by llm-factory.js) ---
AZURE_OPENAI_API_INSTANCE_NAME=
AZURE_OPENAI_API_DEPLOYMENT_NAME=
AZURE_OPENAI_API_VERSION=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_ENDPOINT=
```

## Running the Optimizer

The CWV Optimizer provides multiple commands for different use cases. Here's how to run it with detailed parameter explanations:

### Framework Pipeline (Recommended)

The framework pipeline automatically detects and deploys web frameworks (Hexo, Jekyll, Static HTML):


```bash
# Basic usage with a single GitHub repository
cwv-optimizer framework \
  --github-url https://github.com/username/repo \
  --framework "Static HTML"

# To run the 1st hf datapoint
cwv-optimizer framework \
  --github-url https://github.com/00btweb/00btweb.github.io \
  --framework "Hugo"

# Use dataset entry by index
cwv-optimizer framework \
  --use-hf \
  --hf-index 0 \
  --framework "Static HTML"

# Process ALL entries from the dataset (batch mode)
cwv-optimizer framework \
  --use-hf \
  --all \
  --framework "Jekyll"
```

**Framework Pipeline Arguments:**

- `--github-url, -g`: GitHub repository URL to analyze and optimize
- `--framework, -f`: Web framework type (`"Hexo"`, `"Jekyll"`, or `"Static HTML"`)
- `--use-hf`: Use the HuggingFace dataset instead of providing a GitHub URL
- `--hf-index, -i`: Index of entry in HuggingFace dataset (default: 0)
- `--all, -a`: Process ALL entries in the dataset (batch processing)
- `--device`: Device type for testing (`"mobile"` or `"desktop"`, default: `"mobile"`)
- `--model, -m`: LLM model for code optimization (default: `"azure/gpt-5"`)
- `--cwv-model`: LLM model for CWV analysis (default: `"gpt-5"`)
- `--coding-agent-provider`: AI coding agent (`"aider"`, `"claude"`, `"codex"`, default: `"aider"`)
- `--num-runs, -n`: Number of performance test runs (default: 3)
- `--checkpoint, -c`: Enable workflow checkpointing for resumability
- `--stream, -s`: Stream output in real-time
- `--verbose, -v`: Enable verbose logging

### Full Pipeline

The full pipeline includes additional analysis and AI-driven framework detection:

```bash
# Full pipeline with GitHub URL
cwv-optimizer full \
  --github-url https://github.com/username/repo

# Full pipeline with dataset entry
cwv-optimizer full \
  --use-hf \
  --hf-index 5
```

**Full Pipeline Arguments:**

- Same as framework pipeline, but includes AI framework detection
- `--hf-index, -i`: Index of entry in HuggingFace dataset (default: 0)
- `--use-hf`: Use the HuggingFace dataset instead of providing a GitHub URL

### Environment Variables Setup

Before running, set up the required environment variables:

```bash
# Set runtime environment variables
export CWV_AGENT_NO_SANDBOX=1
export AIDER_IGNORE="fonts/**,*.woff,*.woff2,*.ttf,*.eot,*.otf"
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome || true

# Load environment variables from .env
set -a
. .env
set +a
```

## Harness Pipeline

The benchmark harness evaluates coding agents on CWV optimization tasks. The pipeline has two stages that can be run together or independently.

```text
┌──────────────────────┐       ┌──────────────────────┐
│  Stage 1: AGENTS     │       │  Stage 2: EVALUATE   │
│                      │       │                      │
│  Unzip repo snapshot │─────▶│  Apply patch         │
│  Run coding agent    │       │  Host site locally   │
│  Produce .patch file │       │  Measure CWV metrics │
│                      │       │  Visual validation   │
└──────────────────────┘       └──────────────────────┘
   out/<ts>/patches/              out/<ts>/results/
```

### Full Pipeline (Both Stages)

Run agents on the benchmark dataset and evaluate the results end-to-end:

```bash
# Run the cwv-optimizer agent on first 5 repos, then evaluate
./harness/pipeline.sh \
  --agents agents/template_cwvoptimizer.sh \
  --limit 5

# Run multiple agents and auto-download missing snapshots
./harness/pipeline.sh \
  --agents agents/template_aider.sh,agents/template_codex.sh \
  --auto-snapshot \
  --num-runs 5

# Use a custom CSV dataset
./harness/pipeline.sh \
  --csv harness/SAMPLE/input_300.csv \
  --agents agents/template_claudecode.sh
```

### Stage 1: Run Agents (Produce Patches)

Run coding agents on each repo to produce optimization patches, without hosting or measuring.

```bash
./harness/scripts/run_agents.sh \
  --agents agents/template_cwvoptimizer.sh \
  --limit 10 \
  --auto-snapshot
```

**Output:** `harness/out/<timestamp>/patches/{ID}_{AGENT}.patch` and `harness/out/<timestamp>/logs/{ID}_{AGENT}_agent.log`

| Flag | Description | Default |
|------|-------------|---------|
| `--agents AGENTS` | Comma-separated agent template paths | `agents/template_cwvoptimizer.sh` |
| `--csv PATH` | Input CSV file | `SAMPLE/input.csv` |
| `--auto-snapshot` | Clone+zip repos if snapshots are missing | off |
| `--limit N` | Only process the first N repos | all |
| `--run-ts TIMESTAMP` | Shared run timestamp | auto-generated |

Available agent templates: `template_null.sh`, `template_aider.sh`, `template_codex.sh`, `template_opencode.sh`, `template_claudecode.sh`, `template_cwvoptimizer.sh`, `template_gemini.sh`

### Stage 2: Evaluate Patches

Take patches from a previous Stage 1 run, apply them, host each site, measure CWV (mobile + desktop), and run visual validation.

```bash
./harness/scripts/run_evaluation.sh \
  --run-dir harness/out/20260403_120000 \
  --port 4000 \
  --num-runs 5
```

**Output:** `<run-dir>/results/{ID}_{AGENT}_mobile.json`, `{ID}_{AGENT}_desktop.json`, `{ID}_{AGENT}_screenshot.png`, `{ID}_{AGENT}_visual.json`

| Flag | Description | Default |
|------|-------------|---------|
| `--run-dir DIR` | Run directory from Stage 1 (contains `patches/`) | *required* |
| `--csv PATH` | Input CSV file | `SAMPLE/input.csv` |
| `--port PORT` | Localhost port for hosting | 4000 |
| `--num-runs N` | CWV measurement runs per device | 5 |
| `--agents AGENTS` | Comma-separated agent filter (evaluate only these) | all patches |
| `--limit N` | Only process the first N repos | all |
| `--skip-visual` | Skip visual validation (screenshot + AI eval) | off |

### Resuming / Re-evaluating

You can re-evaluate patches from a previous run without re-running agents:

```bash
# Evaluate an existing run
./harness/pipeline.sh --run-dir harness/out/20260403_120000

# Re-evaluate with more measurement runs
./harness/pipeline.sh --run-dir harness/out/20260403_120000 --num-runs 10

# Evaluate only specific agent's patches
./harness/pipeline.sh --run-dir harness/out/20260403_120000 --agents template_aider
```

### Pipeline Output Structure

```text
harness/out/<timestamp>/
├── patches/                              # Stage 1 output
│   ├── 0_template_cwvoptimizer.patch
│   ├── 1_template_cwvoptimizer.patch
│   └── ...
├── logs/                                 # Stage 1 agent logs
│   ├── 0_template_cwvoptimizer_agent.log
│   └── ...
└── results/                              # Stage 2 output
    ├── 0_template_cwvoptimizer_mobile.json
    ├── 0_template_cwvoptimizer_desktop.json
    ├── 0_template_cwvoptimizer_screenshot.png
    ├── 0_template_cwvoptimizer_visual.json
    ├── 0_template_cwvoptimizer_host.log
    └── ...
```

### Monolithic Mode (evaluate.sh)

The original `evaluate.sh` runs both stages in a single loop (no intermediate patch directory). It supports the same agents and dataset but is not modular:

```bash
./harness/evaluate.sh --auto-snapshot --limit 10
```

Environment overrides: `DEVICE=desktop`, `PORT=8080`, `NUM_RUNS=10`, `SKIP_CWV_MEASURE=1` (patch-only mode). Edit the `AGENTS` array inside `evaluate.sh` to select which agents to benchmark.

## Directory Structure

```text
web-experience-benchmark/
├── src/cwv_optimizer/       # CWV Optimizer (LangGraph pipeline + CLI)
├── harness/                 # Benchmark harness
│   ├── pipeline.sh          # Unified pipeline (agents → evaluate)
│   ├── evaluate.sh          # Monolithic benchmark runner
│   ├── scripts/             # Modular pipeline stages
│   │   ├── run_agents.sh    #   Stage 1: run agents, produce patches
│   │   └── run_evaluation.sh#   Stage 2: host + CWV + visual validate
│   ├── agents/              # Agent templates (aider, codex, claude, etc.)
│   ├── host_files/          # Framework-specific hosting scripts
│   ├── SAMPLE/              # Input datasets and repo snapshots
│   ├── visual_validate.py   # Screenshot + AI visual validation
│   └── out/                 # Pipeline output (patches, logs, results)
├── cwv-agent/               # CWV analysis agent (Node.js submodule)
├── scripts/                 # Standalone utility scripts
│   ├── helper_scripts/      # PSI, CWV benchmark, dataset enrichment
│   ├── framework_scripts/   # Framework detection and package analysis
│   └── sampling_scripts/    # Dataset sampling and EDA
└── dumps/                   # Pipeline workspaces (gitignored)
```

## Citation

If you use this work in your research, please cite:

```bibtex
@software{web_experience_benchmark_2025,
  title={{Towards Benchmarking and Optimizing Web Experiences}},
  author={{Behavior in the Wild}},
  year={2025},
  url={https://github.com/behavior-in-the-wild/web-experience-benchmark},
  version={0.1.0}
}
```
