<<<<<<< HEAD
# web-experience-benchmark
Benchmarking for Evaluating Web Experience (Core Web Vitals, etc)
=======
# CWV Agent Demo

This guide details how to set up and run the Core Web Vitals (CWV) Agent demo end-to-end on a **Linux machine**.

## 1. Installation

Set up the directory, clone the repository, and install Python and Node.js dependencies.

```bash
# Create directory and clone the repository
mkdir demo && cd demo
git clone https://github.com/Boltnav/cwv-agent.git
cd cwv-agent

# Initialize submodules
git submodule update --init --recursive

# Set up Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies and Playwright browsers
pip install -r requirements.txt
playwright install

# Install Node.js dependencies
npm install
```

## 2. Configuration

Create a .env file in the demo/cwv-agent/ directory. Copy the content below and fill in your API keys.

```
# --- Google API Keys (for CWV) ---
GOOGLE_CRUX_API_KEY=
GOOGLE_PAGESPEED_INSIGHTS_API_KEY=

# --- Azure OpenAI (REQUIRED by llm-factory.js) ---
AZURE_OPENAI_API_INSTANCE_NAME=
AZURE_OPENAI_API_DEPLOYMENT_NAME=
AZURE_OPENAI_API_VERSION=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_ENDPOINT=

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

## 3. Running the demo

Set the necessary environment variables, load your .env configuration, and run the optimizer.

```
# 1. Set runtime environment variables
export CWV_AGENT_NO_SANDBOX=1
export AIDER_IGNORE="fonts/**,*.woff,*.woff2,*.ttf,*.eot,*.otf"
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome || true

# 2. Load environment variables from .env
set -a
. .env
set +a

# 3. Run the CWV Optimizer
cwv-optimizer framework \
  -g [https://github.com/adchs/adchs.github.io](https://github.com/adchs/adchs.github.io) \
  -f "Static HTML"
```
>>>>>>> 8bc0d12 (Initial commit)
