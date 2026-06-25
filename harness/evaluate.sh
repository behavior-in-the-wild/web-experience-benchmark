#!/usr/bin/env bash
set -euo pipefail

# =========================
# Parse arguments
# =========================
# Skip flags (also overridable via env before invoking):
#   SKIP_CWV, SKIP_INIT_PSI, SKIP_FINAL_PSI, SKIP_VISUAL, SKIP_CWV_MEASURE, SKIP_AGENT
#   ALLOW_CWV_AFTER_VISUAL_REGRESSION
# CLI: --skip-cwv, --skip-init-psi, --skip-final-psi, --skip-visual, --skip-cwv-measure, --skip-all
#      --skip-agent, --patch-results-dir
LIMIT=""
PARALLEL=32
CONFIG_FILE=""
SOURCE_CONFIG_FILE=""
PRINT_CONFIG=0
SOURCE_MODE_OVERRIDE=""
INPUT_JSONL_OVERRIDE=""
MIRRORS_ROOT_OVERRIDE=""
SERVE_MODEL_OVERRIDE=""
MODEL_KIND_OVERRIDE=""
MODEL_NAME_OVERRIDE=""
AGENT_TEMPLATE_OVERRIDE=""
_OVERRIDE_SUGGESTIONS_FILE=""
_OVERRIDE_SUGGESTION_INDICES=""
_OVERRIDE_CSV=""
SKIP_CWV="${SKIP_CWV:-0}"
SKIP_INIT_PSI="${SKIP_INIT_PSI:-0}"
SKIP_FINAL_PSI="${SKIP_FINAL_PSI:-0}"
SKIP_VISUAL="${SKIP_VISUAL:-0}"
SKIP_CWV_MEASURE="${SKIP_CWV_MEASURE:-0}"
SKIP_AGENT="${SKIP_AGENT:-0}"
PATCH_RESULTS_DIR="${PATCH_RESULTS_DIR:-}"
ALLOW_CWV_AFTER_VISUAL_REGRESSION="${ALLOW_CWV_AFTER_VISUAL_REGRESSION:-0}"
VISUAL_USE_GPU="${VISUAL_USE_GPU:-1}"
VISUAL_GPU_COUNT="${VISUAL_GPU_COUNT:-}"

usage() {
  cat <<'EOF'
Usage: evaluate.sh [options]

Options:
  --config F            Source a model/run config before dispatching jobs
  --source-config F     Source a source-loading config, e.g. configs/sources/live.env
  --print-config        Print resolved run/model config and exit
  --csv PATH            Input CSV path (default: SAMPLE/input_100.csv, or config CSV)
  --source MODE         Source mode: git|live (default: git)
  --jsonl PATH          Live source JSONL path (for --source live)
  --mirrors-root DIR    Live mirror root directory (for --source live)
  --limit N              Process only the first N CSV rows
  --parallel N           Max concurrent jobs (default: 32)
  --agent-template F    Agent template path, e.g. agents/template_codex.sh
  --model-kind K        Model kind label: closed|open (overrides config)
  --model NAME          Model/result label (overrides config)
  --serve-model         For MODEL_KIND=open, start vLLM + usage proxy inside evaluate.sh
  --no-serve-model      For MODEL_KIND=open, use an already-running OpenAI-compatible endpoint
  --skip-cwv             Skip CWV benchmark runs (post-patch; PageSpeed still runs unless skipped)
  --skip-init-psi        Skip baseline PSI (before agent)
  --skip-final-psi       Skip final PSI (after patch)
  --skip-visual          Skip screenshot + AI visual validation
  --skip-cwv-measure     Skip all measurement after the agent (no host/bore/PSI/CWV/visual)
  --skip-all             Skip every measurement phase (init PSI, final PSI, CWV, visual, post-agent hosting)
  --skip-agent           Skip the agent run (step 5); use with --patch-results-dir to supply
                         pre-existing patches and run only the measurement steps (7-9).
  --patch-results-dir D  Directory containing pre-existing <JOB_LABEL>.patch files (e.g. a
                         previous run's results/ folder). Implies --skip-agent. Each job looks
                         for D/<JOB_LABEL>.patch; if absent the job runs with an empty patch.
  --suggestions-file F   Optional. JSON with top-level "suggestions" array (see harness/out/suggestions/…).
                         Omit this flag (and SUGGESTIONS_FILE) for the original behavior: one full run
                         per CSV row per agent, with no suggestion injection.
  --suggestion-indices L Optional; only used with --suggestions-file. Comma-separated 0-based indices
                         (default: all suggestions). Example: 0,2,4
  --help, -h             Show this message

Environment:
  CSV                    Input CSV path (default: SAMPLE/input_100.csv)
  AGENT_TEMPLATE         Single agent template selected by config/env
  MODEL_KIND             closed|open; open can optionally start vLLM
  MODEL_NAME             Human/result label for the selected model
  MODEL_ENDPOINT         Existing OpenAI-compatible endpoint for --no-serve-model
  SOURCE_MODE            git|live
  INPUT_JSONL            Live source JSONL path
  MIRRORS_ROOT           Live mirror root directory
  SUGGESTIONS_FILE       Optional; same as --suggestions-file (CLI wins if both are set)
  SUGGESTION_INDICES     Optional; same as --suggestion-indices (CLI wins if both are set)
  PATCH_RESULTS_DIR      Optional; same as --patch-results-dir (CLI wins if both are set)
  ALLOW_CWV_AFTER_VISUAL_REGRESSION
                         Set to 1 to measure CWV even when visual validation flags a regression
  SKIP_*                 Same behavior as the matching --skip-* flags (0|1)

When SUGGESTIONS_FILE is set and the file exists, each CSV row is evaluated once per selected
suggestion index. Each such run writes that suggestion to eval_suggestion.json, exports
EVAL_SUGGESTION_FILE for the agent, and prefixes result artifacts with  ID_s<N>_AGENT  instead of  ID_AGENT .
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --config PATH"; exit 1; }
      CONFIG_FILE="$1"
      shift
      ;;
    --source-config)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --source-config PATH"; exit 1; }
      SOURCE_CONFIG_FILE="$1"
      shift
      ;;
    --print-config)
      PRINT_CONFIG=1
      shift
      ;;
    --csv)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --csv PATH"; exit 1; }
      _OVERRIDE_CSV="$1"
      shift
      ;;
    --source)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --source git|live"; exit 1; }
      SOURCE_MODE_OVERRIDE="$1"
      shift
      ;;
    --jsonl)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --jsonl PATH"; exit 1; }
      INPUT_JSONL_OVERRIDE="$1"
      shift
      ;;
    --mirrors-root)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --mirrors-root DIR"; exit 1; }
      MIRRORS_ROOT_OVERRIDE="$1"
      shift
      ;;
    --limit)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --limit N"; exit 1; }
      LIMIT="$1"
      shift
      ;;
    --parallel)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N"; exit 1; }
      PARALLEL="$1"
      shift
      ;;
    --agent-template)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --agent-template PATH"; exit 1; }
      AGENT_TEMPLATE_OVERRIDE="$1"
      shift
      ;;
    --model-kind)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --model-kind closed|open"; exit 1; }
      MODEL_KIND_OVERRIDE="$1"
      shift
      ;;
    --model)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --model NAME"; exit 1; }
      MODEL_NAME_OVERRIDE="$1"
      shift
      ;;
    --serve-model)
      SERVE_MODEL_OVERRIDE=1
      shift
      ;;
    --no-serve-model)
      SERVE_MODEL_OVERRIDE=0
      shift
      ;;
    --skip-cwv)
      SKIP_CWV=1
      shift
      ;;
    --skip-init-psi)
      SKIP_INIT_PSI=1
      shift
      ;;
    --skip-final-psi)
      SKIP_FINAL_PSI=1
      shift
      ;;
    --skip-visual)
      SKIP_VISUAL=1
      shift
      ;;
    --skip-cwv-measure)
      SKIP_CWV_MEASURE=1
      shift
      ;;
    --skip-all)
      SKIP_CWV=1
      SKIP_INIT_PSI=1
      SKIP_FINAL_PSI=1
      SKIP_VISUAL=1
      SKIP_CWV_MEASURE=1
      shift
      ;;
    --skip-agent)
      SKIP_AGENT=1
      shift
      ;;
    --patch-results-dir)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --patch-results-dir PATH"; exit 1; }
      PATCH_RESULTS_DIR="$1"
      SKIP_AGENT=1
      shift
      ;;
    --suggestions-file)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --suggestions-file PATH"; exit 1; }
      _OVERRIDE_SUGGESTIONS_FILE="$1"
      shift
      ;;
    --suggestion-indices)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --suggestion-indices 0,1,…"; exit 1; }
      _OVERRIDE_SUGGESTION_INDICES="$1"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *) echo "Unknown option: $1 (try --help)"; exit 1 ;;
  esac
done

# =========================
# Resolve paths
# =========================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$SCRIPT_DIR"
source "$SCRIPT_DIR/lib/host_tool_lib.sh"

# Activate project venv so python3 picks up playwright, datasets, etc.
if [[ -f "$SCRIPT_DIR/../.venv/bin/activate" ]]; then
  source "$SCRIPT_DIR/../.venv/bin/activate"
fi

RUN_TS="$(date +%Y%m%d_%H%M%S)"
TASK_SPEC="$SCRIPT_DIR/tasks/optimize_cwv_debug.txt"

CWV_SCRIPT="$SCRIPT_DIR/../src/cwv_tool/cwv_benchmark.py"
VISUAL_SCRIPT="$SCRIPT_DIR/../src/regression_tool/visual_validate.py"
PSI_SCRIPT="$SCRIPT_DIR/psi_report.py"

# Resolve PATCH_RESULTS_DIR to absolute path before .env changes cwd semantics
[[ -n "$PATCH_RESULTS_DIR" && ! "$PATCH_RESULTS_DIR" = /* ]] && PATCH_RESULTS_DIR="$(cd "$PATCH_RESULTS_DIR" && pwd)"
export PATCH_RESULTS_DIR
export SKIP_AGENT

# Save command-line overrides before .env (so DEVICE=desktop ./evaluate.sh wins over .env)
_OVERRIDE_DEVICE="${DEVICE:-}"
_OVERRIDE_PORT="${PORT:-}"
_OVERRIDE_NUM_RUNS="${NUM_RUNS:-}"
_OVERRIDE_AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-}"
# CLI --suggestions-file / --suggestion-indices win; else env before .env wins over .env
[[ -z "$_OVERRIDE_SUGGESTIONS_FILE" ]] && _OVERRIDE_SUGGESTIONS_FILE="${SUGGESTIONS_FILE:-}"
[[ -z "$_OVERRIDE_SUGGESTION_INDICES" ]] && _OVERRIDE_SUGGESTION_INDICES="${SUGGESTION_INDICES:-}"

# =========================
# Load environment
# =========================
for _env_file in "$SCRIPT_DIR/../.env" "$SCRIPT_DIR/.env"; do
  if [[ -f "$_env_file" ]]; then
    set -a; source "$_env_file"; set +a
  fi
done
unset _env_file

if [[ -n "$CONFIG_FILE" ]]; then
  if [[ "$CONFIG_FILE" != /* ]]; then
    if [[ -f "$CONFIG_FILE" ]]; then
      CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
    else
      CONFIG_FILE="$SCRIPT_DIR/../$CONFIG_FILE"
    fi
  fi
  [[ -f "$CONFIG_FILE" ]] || { echo "Missing config: $CONFIG_FILE"; exit 1; }
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  set +a
fi

if [[ -n "$SOURCE_CONFIG_FILE" ]]; then
  if [[ "$SOURCE_CONFIG_FILE" != /* ]]; then
    if [[ -f "$SOURCE_CONFIG_FILE" ]]; then
      SOURCE_CONFIG_FILE="$(cd "$(dirname "$SOURCE_CONFIG_FILE")" && pwd)/$(basename "$SOURCE_CONFIG_FILE")"
    else
      SOURCE_CONFIG_FILE="$SCRIPT_DIR/../$SOURCE_CONFIG_FILE"
    fi
  fi
  [[ -f "$SOURCE_CONFIG_FILE" ]] || { echo "Missing source config: $SOURCE_CONFIG_FILE"; exit 1; }
  set -a
  # shellcheck disable=SC1090
  source "$SOURCE_CONFIG_FILE"
  set +a
fi

# Restore command-line overrides; then apply defaults for any still unset
[[ -n "$_OVERRIDE_CSV" ]] && CSV="$_OVERRIDE_CSV"
[[ -n "$SOURCE_MODE_OVERRIDE" ]] && SOURCE_MODE="$SOURCE_MODE_OVERRIDE"
[[ -n "$INPUT_JSONL_OVERRIDE" ]] && INPUT_JSONL="$INPUT_JSONL_OVERRIDE"
[[ -n "$MIRRORS_ROOT_OVERRIDE" ]] && MIRRORS_ROOT="$MIRRORS_ROOT_OVERRIDE"
[[ -n "$_OVERRIDE_DEVICE" ]] && DEVICE="$_OVERRIDE_DEVICE"
[[ -n "$_OVERRIDE_PORT" ]]   && PORT="$_OVERRIDE_PORT"
[[ -n "$_OVERRIDE_NUM_RUNS" ]] && NUM_RUNS="$_OVERRIDE_NUM_RUNS"
[[ -n "$_OVERRIDE_AZURE_DEPLOYMENT" ]] && AZURE_DEPLOYMENT="$_OVERRIDE_AZURE_DEPLOYMENT"
[[ -n "$AGENT_TEMPLATE_OVERRIDE" ]] && AGENT_TEMPLATE="$AGENT_TEMPLATE_OVERRIDE"
[[ -n "$MODEL_KIND_OVERRIDE" ]] && MODEL_KIND="$MODEL_KIND_OVERRIDE"
[[ -n "$MODEL_NAME_OVERRIDE" ]] && MODEL_NAME="$MODEL_NAME_OVERRIDE"
[[ -n "$SERVE_MODEL_OVERRIDE" ]] && SERVE_MODEL="$SERVE_MODEL_OVERRIDE"
SUGGESTIONS_FILE="${_OVERRIDE_SUGGESTIONS_FILE:-${SUGGESTIONS_FILE:-}}"
SUGGESTION_INDICES="${_OVERRIDE_SUGGESTION_INDICES:-${SUGGESTION_INDICES:-}}"
# Whitespace-only counts as unset.
_s="${SUGGESTIONS_FILE:-}"
_s="${_s#"${_s%%[![:space:]]*}"}"
_s="${_s%"${_s##*[![:space:]]}"}"
SUGGESTIONS_FILE="$_s"
_s="${SUGGESTION_INDICES:-}"
_s="${_s#"${_s%%[![:space:]]*}"}"
_s="${_s%"${_s##*[![:space:]]}"}"
SUGGESTION_INDICES="$_s"
BASE_PORT="${PORT:-4000}"
export AZURE_DEPLOYMENT="${AZURE_DEPLOYMENT:-gpt-4.1}"
DEVICE="${DEVICE:-desktop}"     # mobile|desktop
NUM_RUNS="${NUM_RUNS:-5}"
CSV="${CSV:-$SCRIPT_DIR/SAMPLE/input_100.csv}"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_MODE="${SOURCE_MODE:-git}"
INPUT_JSONL="${INPUT_JSONL:-$SCRIPT_DIR/SAMPLE/live_filtered_top3.jsonl}"
MIRRORS_ROOT="${MIRRORS_ROOT:-$REPO_ROOT/live_assets_eds}"
MODEL_KIND="${MODEL_KIND:-closed}"
MODEL_NAME="${MODEL_NAME:-${RESULTS_LABEL:-}}"
RESULTS_LABEL="${RESULTS_LABEL:-${MODEL_NAME:-}}"
SERVE_MODEL="${SERVE_MODEL:-0}"
VLLM_BASE_PORT="${VLLM_BASE_PORT:-8000}"
VLLM_BIND_HOST="${VLLM_BIND_HOST:-0.0.0.0}"
VLLM_CLIENT_HOST="${VLLM_CLIENT_HOST:-127.0.0.1}"
VLLM_READY_TIMEOUT="${VLLM_READY_TIMEOUT:-1800}"
USAGE_PROXY_BASE_PORT="${USAGE_PROXY_BASE_PORT:-9000}"
USAGE_PROXY_HOST="${USAGE_PROXY_HOST:-127.0.0.1}"

# Route all mktemp calls and repo cache to repo-local defaults unless overridden.
export TMPDIR="${HARNESS_TMPDIR:-${TMPDIR:-$REPO_ROOT/.tmp}}"
export HARNESS_TMPDIR="${HARNESS_TMPDIR:-$TMPDIR}"
export WEB_BENCH_REPO_CACHE="${WEB_BENCH_REPO_CACHE:-$REPO_ROOT/.cache/web_benchmark_repos}"
mkdir -p "$TMPDIR" "$WEB_BENCH_REPO_CACHE"
if [[ "$INPUT_JSONL" != /* ]]; then
  if [[ -f "$INPUT_JSONL" ]]; then
    INPUT_JSONL="$(cd "$(dirname "$INPUT_JSONL")" && pwd)/$(basename "$INPUT_JSONL")"
  else
    INPUT_JSONL="$REPO_ROOT/$INPUT_JSONL"
  fi
fi
if [[ "$MIRRORS_ROOT" != /* ]]; then
  if [[ -d "$MIRRORS_ROOT" ]]; then
    MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"
  else
    MIRRORS_ROOT="$REPO_ROOT/$MIRRORS_ROOT"
  fi
fi

# When EVAL_OUT_DIR is set, all artifacts land there. Otherwise configs can select
# a result label under harness/out/<timestamp>/<label>/.
if [[ -n "${EVAL_OUT_DIR:-}" ]]; then
  TMP_ROOT="$EVAL_OUT_DIR/run"
  RESULTS_DIR="$EVAL_OUT_DIR/results"
else
  EVAL_OUT_ROOT="${EVAL_OUT_ROOT:-$SCRIPT_DIR/out}"
  if [[ -n "$RESULTS_LABEL" ]]; then
    EVAL_RUN_ROOT="$EVAL_OUT_ROOT/${RUN_TS}/${RESULTS_LABEL}"
  else
    EVAL_RUN_ROOT="$EVAL_OUT_ROOT/${RUN_TS}"
  fi
  TMP_ROOT="$EVAL_RUN_ROOT/run"
  RESULTS_DIR="$EVAL_RUN_ROOT/results"
fi

# =========================
# Agents to benchmark
# =========================
AGENTS=(
  # "agents/template_null.sh"
  "agents/template_codex.sh"   # requires: npm install -g @openai/codex
  # "agents/template_aider.sh"
  # "agents/template_opencode.sh"
  # "agents/template_opencodegpt51codex.sh"
  # "agents/template_gemini.sh"
  # "agents/template_cwvoptimizer.sh"
  # "agents/template_opencodegpt41.sh"
  # "agents/template_claudecode.sh"
)

# Optional override for wrapper scripts/configs, e.g.
#   AGENT_TEMPLATE="agents/template_opencode_os.sh" ./evaluate.sh
#   EVAL_AGENTS="agents/template_opencode_os.sh" ./evaluate.sh
# Multiple agents may be comma-separated.
_AGENTS_OVERRIDE="${AGENT_TEMPLATE:-${EVAL_AGENTS:-${AGENTS_OVERRIDE:-}}}"
if [[ -n "$_AGENTS_OVERRIDE" ]]; then
  AGENTS=()
  IFS=',' read -r -a _AGENT_PARTS <<< "$_AGENTS_OVERRIDE"
  for _agent in "${_AGENT_PARTS[@]}"; do
    _agent="${_agent#"${_agent%%[![:space:]]*}"}"
    _agent="${_agent%"${_agent##*[![:space:]]}"}"
    [[ -n "$_agent" ]] && AGENTS+=("$_agent")
  done
fi

if [[ "$PRINT_CONFIG" == "1" ]]; then
  cat <<EOF
[config] CONFIG_FILE=${CONFIG_FILE:-}
[config] SOURCE_CONFIG_FILE=${SOURCE_CONFIG_FILE:-}
[config] MODEL_KIND=$MODEL_KIND
[config] MODEL_NAME=${MODEL_NAME:-}
[config] RESULTS_LABEL=${RESULTS_LABEL:-}
[config] SERVE_MODEL=$SERVE_MODEL
[config] MODEL_ENDPOINT=${MODEL_ENDPOINT:-}
[config] MODEL_ID=${MODEL_ID:-}
[config] SERVED_MODEL_NAME=${SERVED_MODEL_NAME:-}
[config] AZURE_DEPLOYMENT=${AZURE_DEPLOYMENT:-}
[config] AGENTS=${AGENTS[*]}
[config] CSV=$CSV
[config] SOURCE_MODE=$SOURCE_MODE
[config] INPUT_JSONL=$INPUT_JSONL
[config] MIRRORS_ROOT=$MIRRORS_ROOT
[config] OUTPUT=$RESULTS_DIR
[config] TMPDIR=$TMPDIR
[config] WEB_BENCH_REPO_CACHE=$WEB_BENCH_REPO_CACHE
EOF
  exit 0
fi

vllm_pid=""
proxy_pid=""
OPEN_MODEL_USAGE_DIR=""

cleanup_model_services() {
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
    proxy_pid=""
  fi
  if [[ -n "$vllm_pid" ]]; then
    _kill_vllm "$vllm_pid"
    vllm_pid=""
  fi
}
trap cleanup_model_services EXIT INT TERM

start_open_model_services() {
  local model_label port proxy_port model_dir log_file proxy_log_file
  model_label="${SERVED_MODEL_NAME:-${MODEL_NAME:-${MODEL_ID:-open-model}}}"
  port="${VLLM_PORT:-$VLLM_BASE_PORT}"
  proxy_port="${USAGE_PROXY_PORT:-$USAGE_PROXY_BASE_PORT}"
  model_dir="$(dirname "$RESULTS_DIR")"
  OPEN_MODEL_USAGE_DIR="$model_dir/usage"
  mkdir -p "$OPEN_MODEL_USAGE_DIR" "$model_dir/logs"
  log_file="$model_dir/logs/vllm.log"
  proxy_log_file="$model_dir/logs/usage_proxy.log"

  [[ -n "${MODEL_ID:-}" ]] || { echo "MODEL_ID is required when MODEL_KIND=open and SERVE_MODEL=1"; exit 1; }
  [[ -n "${SERVED_MODEL_NAME:-}" ]] || SERVED_MODEL_NAME="$model_label"
  export SERVED_MODEL_NAME

  source "$SCRIPT_DIR/opensource_models/vllm_lifecycle_lib.sh"

  echo "[model] Starting vLLM for $SERVED_MODEL_NAME on port $port"
  fuser -k "${port}/tcp" 2>/dev/null || true
  (
    VLLM_HOST="$VLLM_BIND_HOST" \
    VLLM_PORT="$port" \
    MODEL_ID="$MODEL_ID" \
    SERVED_MODEL_NAME="$SERVED_MODEL_NAME" \
    setsid "$SCRIPT_DIR/opensource_models/serve_model.sh" >"$log_file" 2>&1
  ) &
  vllm_pid=$!

  if ! wait_for_vllm "$port" "$VLLM_READY_TIMEOUT"; then
    echo "[model] ERROR: vLLM did not become ready for $SERVED_MODEL_NAME. Log: $log_file" >&2
    exit 1
  fi

  echo "[model] Starting usage proxy for $SERVED_MODEL_NAME on port $proxy_port"
  fuser -k "${proxy_port}/tcp" 2>/dev/null || true
  sleep 1
  python3 "$SCRIPT_DIR/opensource_models/usage_proxy.py" \
    --listen-host "$USAGE_PROXY_HOST" \
    --listen-port "$proxy_port" \
    --upstream-base "http://${VLLM_CLIENT_HOST}:${port}" \
    --output-dir "$OPEN_MODEL_USAGE_DIR" \
    --model-label "$SERVED_MODEL_NAME" \
    --quiet \
    >"$proxy_log_file" 2>&1 &
  proxy_pid=$!

  if ! wait_for_proxy "$proxy_port" 30; then
    echo "[model] ERROR: usage proxy did not become ready for $SERVED_MODEL_NAME. Log: $proxy_log_file" >&2
    exit 1
  fi

  export MODEL_ENDPOINT="http://${USAGE_PROXY_HOST}:${proxy_port}/v1"
  export OPENCODE_OPENAI_BASE_URL="$MODEL_ENDPOINT"
  export OPENAI_BASE_URL="$MODEL_ENDPOINT"
  export OPENAI_API_KEY="${VLLM_API_KEY:-EMPTY}"
  export OPENCODE_USAGE_PROXY=1
  export VLLM_SERVED_MODEL_NAME="$SERVED_MODEL_NAME"
  export OPENCODE_MODEL="${OPENCODE_MODEL:-vllm/$SERVED_MODEL_NAME}"
  export OPENCODE_MAX_TOKENS="${OPENCODE_MAX_TOKENS:-${MAX_OUTPUT_TOKENS:-8000}}"
}

if [[ "$MODEL_KIND" == "open" ]]; then
  if [[ "$SERVE_MODEL" == "1" ]]; then
    start_open_model_services
  else
    MODEL_ENDPOINT="${MODEL_ENDPOINT:-${OPENCODE_OPENAI_BASE_URL:-${OPENAI_BASE_URL:-}}}"
    if [[ -z "$MODEL_ENDPOINT" && "${SKIP_AGENT:-0}" != "1" ]]; then
      echo "MODEL_ENDPOINT/OPENAI_BASE_URL is required for open model --no-serve-model" >&2
      exit 1
    fi
    SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-${MODEL_NAME:-local-model}}"
    export MODEL_ENDPOINT
    export OPENCODE_OPENAI_BASE_URL="$MODEL_ENDPOINT"
    export OPENAI_BASE_URL="$MODEL_ENDPOINT"
    export OPENAI_API_KEY="${OPENAI_API_KEY:-${VLLM_API_KEY:-EMPTY}}"
    export VLLM_SERVED_MODEL_NAME="$SERVED_MODEL_NAME"
    export OPENCODE_MODEL="${OPENCODE_MODEL:-vllm/$SERVED_MODEL_NAME}"
    export OPENCODE_MAX_TOKENS="${OPENCODE_MAX_TOKENS:-${MAX_OUTPUT_TOKENS:-8000}}"
  fi
fi

export BENCH_MODEL_KIND="$MODEL_KIND"
export BENCH_MODEL_NAME="${MODEL_NAME:-${SERVED_MODEL_NAME:-}}"
export BENCH_MODEL_ENDPOINT="${MODEL_ENDPOINT:-}"
export BENCH_USAGE_PROXY_URL="${MODEL_ENDPOINT:-}"

# =========================
# Sanity checks
# =========================
[[ -f "$CSV" ]]           || { echo "Missing CSV: $CSV"; exit 1; }
if [[ "$SOURCE_MODE" == "live" ]]; then
  [[ -f "$INPUT_JSONL" ]] || { echo "Missing INPUT_JSONL: $INPUT_JSONL"; exit 1; }
  [[ -d "$MIRRORS_ROOT" ]] || { echo "Missing MIRRORS_ROOT: $MIRRORS_ROOT"; exit 1; }
elif [[ "$SOURCE_MODE" != "git" ]]; then
  echo "Invalid SOURCE_MODE=$SOURCE_MODE (must be git|live)" >&2
  exit 1
fi
[[ -f "$TASK_SPEC" ]]     || { echo "Missing task spec: $TASK_SPEC"; exit 1; }
[[ -f "$CWV_SCRIPT" ]]    || { echo "Missing cwv_benchmark.py: $CWV_SCRIPT"; exit 1; }
[[ -f "$VISUAL_SCRIPT" ]] || { echo "Missing visual_validate.py: $VISUAL_SCRIPT"; exit 1; }
[[ -f "$PSI_SCRIPT" ]]    || { echo "Missing psi_report.py: $PSI_SCRIPT"; exit 1; }

mkdir -p "$TMP_ROOT" "$RESULTS_DIR"
echo "[run] Input:    $CSV"
[[ "$SOURCE_MODE" == "live" ]] && echo "[run] Live JSONL: $INPUT_JSONL  Mirrors=$MIRRORS_ROOT"
echo "[run] Output:   $RESULTS_DIR"
echo "[run] Parallel: $PARALLEL  BasePort=$BASE_PORT  NumRuns=$NUM_RUNS"
if [[ -z "$VISUAL_GPU_COUNT" && "$VISUAL_USE_GPU" == "1" ]] && command -v nvidia-smi >/dev/null 2>&1; then
  VISUAL_GPU_COUNT="$(nvidia-smi -L 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
fi
if [[ "$VISUAL_USE_GPU" == "1" && "${VISUAL_GPU_COUNT:-0}" =~ ^[0-9]+$ && "${VISUAL_GPU_COUNT:-0}" -gt 0 ]]; then
  echo "[run] Visual GPU assignment: enabled across $VISUAL_GPU_COUNT GPU(s)"
else
  echo "[run] Visual GPU assignment: disabled"
fi
[[ -n "$LIMIT" ]] && echo "[run] LIMIT=$LIMIT"
[[ -n "${SUGGESTIONS_FILE:-}" ]] && echo "[run] Suggestions file: $SUGGESTIONS_FILE  indices=${SUGGESTION_INDICES:-all}"
[[ "$SKIP_CWV" == "1" ]]              && echo "[run] --skip-cwv"
[[ "$SKIP_INIT_PSI" == "1" ]]         && echo "[run] --skip-init-psi"
[[ "$SKIP_FINAL_PSI" == "1" ]]        && echo "[run] --skip-final-psi"
[[ "$SKIP_VISUAL" == "1" ]]           && echo "[run] --skip-visual"
[[ "$SKIP_CWV_MEASURE" == "1" ]]      && echo "[run] --skip-cwv-measure"
[[ "$SKIP_AGENT" == "1" ]]            && echo "[run] --skip-agent"
[[ "$ALLOW_CWV_AFTER_VISUAL_REGRESSION" == "1" ]] && echo "[run] ALLOW_CWV_AFTER_VISUAL_REGRESSION=1"
[[ -n "${PATCH_RESULTS_DIR:-}" ]]     && echo "[run] Patch results dir: $PATCH_RESULTS_DIR"

# =========================
# Helpers
# =========================

# Wait up to TIMEOUT seconds for localhost:PORT to respond.
wait_for_server() {
  local port="$1"
  local timeout="${2:-90}"
  local i
  for i in $(seq 1 "$timeout"); do
    if curl -fs "http://localhost:${port}/" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# Wait up to TIMEOUT seconds for bore to emit its public URL.
# Prints "http://bore.pub:PORT" on success; returns 1 on timeout.
# Accepts two bore output forms (from patch_bore_parallel.py BoreTunnel):
#   Form 1: "listening at bore.pub:PORT"
#   Form 2: "remote_port = PORT" or "remote_port=PORT"
wait_for_bore_url() {
  local log_file="$1"
  local timeout="${2:-30}"
  local i bore_port
  for i in $(seq 1 "$timeout"); do
    bore_port=$(sed -n 's/.*bore\.pub:\([0-9][0-9]*\).*/\1/p' "$log_file" 2>/dev/null | head -1 || true)
    if [[ -z "$bore_port" ]]; then
      bore_port=$(sed -n 's/.*remote_port[[:space:]]*[=:][[:space:]]*\([0-9][0-9]*\).*/\1/p' "$log_file" 2>/dev/null | head -1 || true)
    fi
    if [[ -n "$bore_port" ]]; then
      echo "http://bore.pub:${bore_port}"
      return 0
    fi
    sleep 1
  done
  return 1
}

# =========================
# Per-job function
# =========================
# All globals set before the dispatch loop (SCRIPT_DIR, TMP_ROOT, RESULTS_DIR,
# TASK_SPEC, CWV_SCRIPT, VISUAL_SCRIPT, PSI_SCRIPT, NUM_RUNS, BASE_PORT,
# AZURE_DEPLOYMENT) are inherited by the subshell that runs this function.
# Optional args 16–17: suggestions JSON path + 0-based index (see --suggestions-file).
run_job() {
  local ID="$1"
  local REPO_ID="$2"
  local FRAMEWORK="$3"
  local COMMIT_ID="$4"
  local HOST_FILE_PATH="$5"
  local CWV_MOBILE="$6"
  local CWV_DESKTOP="$7"
  local LCP_ENTRIES_DESKTOP="$8"
  local LCP_ENTRIES_MOBILE="$9"
  local CLS_SHIFTS_MOBILE="${10}"
  local CLS_SHIFTS_DESKTOP="${11}"
  local INP_INTERACTIONS_MOBILE="${12}"
  local INP_INTERACTIONS_DESKTOP="${13}"
  local AGENT="${14}"
  local SLOT="${15}"
  local SUGG_FILE_RAW="${16:- }"
  local SUGG_IDX_RAW="${17:- }"
  local PAGE_URL_RAW="${18:- }"
  local MIRROR_DIR_RAW="${19:- }"
  local DOMAIN_RAW="${20:- }"

  local AGENT_NAME PORT RUN_DIR REPO_DIR JOB_LABEL JOB_DIR
  AGENT_NAME="$(basename "$AGENT" .sh)"
  PORT="$(bench_port_for_slot "$BASE_PORT" "$SLOT" 0 "$PARALLEL")"
  [[ "$SUGG_FILE_RAW" == " " ]] && SUGG_FILE_RAW=""
  [[ "$SUGG_IDX_RAW" == " " ]] && SUGG_IDX_RAW=""
  if [[ -n "$SUGG_FILE_RAW" && -n "$SUGG_IDX_RAW" ]]; then
    JOB_LABEL="${ID}_s${SUGG_IDX_RAW}_${AGENT_NAME}"
  else
    JOB_LABEL="${ID}_${AGENT_NAME}"
  fi
  RUN_DIR="$TMP_ROOT/${JOB_LABEL}"
  REPO_DIR="$RUN_DIR/repo"
  JOB_DIR="$RESULTS_DIR/$JOB_LABEL"
  if [[ "$RUN_DIR" != /* || "$REPO_DIR" != /* || "$JOB_DIR" != /* ]]; then
    local _RUN_CWD="$PWD"
    [[ "$RUN_DIR" != /* ]] && RUN_DIR="$_RUN_CWD/$RUN_DIR"
    [[ "$REPO_DIR" != /* ]] && REPO_DIR="$_RUN_CWD/$REPO_DIR"
    [[ "$JOB_DIR" != /* ]] && JOB_DIR="$_RUN_CWD/$JOB_DIR"
  fi

  echo "======================================"
  if [[ -n "$SUGG_IDX_RAW" ]]; then
    echo "ID=$ID Repo=$REPO_ID Agent=$AGENT_NAME SuggestionIndex=$SUGG_IDX_RAW Slot=$SLOT Port=$PORT"
  else
    echo "ID=$ID Repo=$REPO_ID Agent=$AGENT_NAME Slot=$SLOT Port=$PORT"
  fi
  echo "======================================"

  mkdir -p "$RUN_DIR" "$REPO_DIR" "$JOB_DIR"

  if [[ -n "$SUGG_FILE_RAW" && -n "$SUGG_IDX_RAW" ]]; then
    python3 - "$SUGG_FILE_RAW" "$SUGG_IDX_RAW" "$RUN_DIR/eval_suggestion.json" <<'PY'
import json, sys
src, idx_s, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(src, encoding="utf-8") as f:
    data = json.load(f)
sugs = data.get("suggestions", [])
if not (0 <= idx_s < len(sugs)):
    print("ERROR: suggestion index out of range for extract", file=sys.stderr)
    sys.exit(1)
obj = sugs[idx_s]
with open(out, "w", encoding="utf-8") as o:
    json.dump(obj, o, indent=2, ensure_ascii=False)
PY
    cp "$RUN_DIR/eval_suggestion.json" "$JOB_DIR/input_suggestion.json"
    export EVAL_SUGGESTION_FILE="$RUN_DIR/eval_suggestion.json"
    export EVAL_SUGGESTION_INDEX="$SUGG_IDX_RAW"
  else
    unset EVAL_SUGGESTION_FILE EVAL_SUGGESTION_INDEX
  fi

  rm -rf "$REPO_DIR"
  local COMMIT_ID_CLEAN=""
  local VISUAL_BASELINE_DIR=""
  if [[ "$SOURCE_MODE" == "live" ]]; then
    local MIRROR_ABS="$MIRRORS_ROOT/$MIRROR_DIR_RAW"
    if [[ ! -d "$MIRROR_ABS" ]]; then
      echo "[run] ERROR: mirror not found: $MIRROR_ABS"
      rm -rf "$RUN_DIR"
      return 1
    fi
    mkdir -p "$REPO_DIR"
    cp -r "$MIRROR_ABS/." "$REPO_DIR/"
    git -C "$REPO_DIR" init -q
    git -C "$REPO_DIR" add -A >/dev/null 2>&1 || true
    git -C "$REPO_DIR" commit -qm "baseline" >/dev/null 2>&1 || true
    COMMIT_ID_CLEAN=""
    VISUAL_BASELINE_DIR="$RUN_DIR/baseline_visual"
    cp -r --no-preserve=mode "$REPO_DIR" "$VISUAL_BASELINE_DIR"
    python3 - "$JOB_DIR/baseline_meta.json" "$ID" "$DOMAIN_RAW" "$PAGE_URL_RAW" "$MIRROR_DIR_RAW" <<'PY'
import json, sys
out, row_id, domain, url, mirror = sys.argv[1:6]
with open(out, "w", encoding="utf-8") as f:
    json.dump({"source_mode": "live", "id": row_id, "domain": domain, "page_url": url, "mirror_dir": mirror}, f, indent=2)
PY
  else
    if ! bench_git_clone_checkout "$REPO_ID" "$COMMIT_ID" "$REPO_DIR" "[run]" "$ID"; then
      rm -rf "$RUN_DIR"
      return 1
    fi
    COMMIT_ID_CLEAN="$BENCH_GIT_REQUESTED_COMMIT"
    bench_git_write_meta "$JOB_DIR/baseline_meta.json"
  fi

  # -------------------------
  # 3) Commit baseline so agent diff is unambiguous
  # -------------------------
  git -C "$REPO_DIR" add -A >/dev/null 2>&1 || true
  git -C "$REPO_DIR" commit -qm "baseline" >/dev/null 2>&1 || true

  # -------------------------
  # 4) Export context env vars (CSV baselines) for agent
  # Write large CWV blobs to a file to avoid ARG_MAX limits when exec'ing the agent bash script.
  # The agent reads CWV_ENV_FILE to source these values.
  # -------------------------
  export FRAMEWORK="$(echo "${FRAMEWORK:-unknown}" | tr '[:upper:]' '[:lower:]')"
  export REPO_ID
  if [[ "$SOURCE_MODE" == "live" ]]; then
    export PAGE_URL="$PAGE_URL_RAW"
    export DOMAIN="${DOMAIN_RAW:-$REPO_ID}"
    export CWV_FIELD_MOBILE="${CWV_MOBILE:-null}"
    export CWV_FIELD_DESKTOP="${CWV_DESKTOP:-null}"
    export CWV_SYNTHETIC_MOBILE="${CWV_SYNTHETIC_MOBILE:-null}"
    export CWV_SYNTHETIC_DESKTOP="${CWV_SYNTHETIC_DESKTOP:-null}"
  fi
  local CWV_ENV_FILE="$RESULTS_DIR/${JOB_LABEL}_cwv.env"
  {
    printf 'CWV_BASELINE_MOBILE=%s\n'        "$(printf '%s' "${CWV_MOBILE:-}"                  | base64 -w0)"
    printf 'LCP_ENTRIES_MOBILE=%s\n'         "$(printf '%s' "${LCP_ENTRIES_MOBILE:-}"           | base64 -w0)"
    printf 'CWV_BASELINE_DESKTOP=%s\n'       "$(printf '%s' "${CWV_DESKTOP:-}"                  | base64 -w0)"
    printf 'LCP_ENTRIES_DESKTOP=%s\n'        "$(printf '%s' "${LCP_ENTRIES_DESKTOP:-}"           | base64 -w0)"
    printf 'CLS_SHIFTS_MOBILE=%s\n'          "$(printf '%s' "${CLS_SHIFTS_MOBILE:-}"             | base64 -w0)"
    printf 'CLS_SHIFTS_DESKTOP=%s\n'         "$(printf '%s' "${CLS_SHIFTS_DESKTOP:-}"            | base64 -w0)"
    printf 'INP_INTERACTIONS_MOBILE=%s\n'    "$(printf '%s' "${INP_INTERACTIONS_MOBILE:-}"       | base64 -w0)"
    printf 'INP_INTERACTIONS_DESKTOP=%s\n'   "$(printf '%s' "${INP_INTERACTIONS_DESKTOP:-}"      | base64 -w0)"
  } > "$CWV_ENV_FILE"
  export CWV_ENV_FILE

  # -------------------------
  # 4b) Initial PSI measurement (baseline, before agent runs)
  # -------------------------
  if [[ "${SKIP_CWV_MEASURE:-0}" != "1" && "${SKIP_INIT_PSI:-0}" != "1" ]]; then
    local INIT_HOST_LOG INIT_BORE_LOG INIT_PSI_MOBILE INIT_PSI_DESKTOP INIT_HOST_PID INIT_BORE_PID
    INIT_HOST_LOG="$JOB_DIR/init_host.log"
    INIT_BORE_LOG="$JOB_DIR/init_bore.log"
    INIT_PSI_MOBILE="$JOB_DIR/init_psi_mobile.json"
    INIT_PSI_DESKTOP="$JOB_DIR/init_psi_desktop.json"

    echo "[run] Starting baseline HTTP server on port $PORT ..."
    if ! bench_start_host "$REPO_DIR" "$JOB_DIR" "$HOST_FILE_PATH" "$FRAMEWORK" "$PORT" "$INIT_HOST_LOG" "$SLOT"; then
      echo "[run] WARN: baseline host tool failed; skipping initial PSI"
      INIT_HOST_PID=""
    else
      INIT_HOST_PID="$BENCH_HOST_HANDLE"
    fi

    if [[ -n "$INIT_HOST_PID" ]] && wait_for_server "$PORT" 90; then
      echo "[run] Starting baseline bore tunnel ..."
      RUST_LOG=info bore local "$PORT" --to bore.pub > "$INIT_BORE_LOG" 2>&1 &
      INIT_BORE_PID=$!

      local BORE_URL_INIT=""
      BORE_URL_INIT=$(wait_for_bore_url "$INIT_BORE_LOG" 30) || BORE_URL_INIT=""

      if [[ -n "$BORE_URL_INIT" ]]; then
        echo "[run] Baseline bore URL: $BORE_URL_INIT"
        echo "[run] Running initial PSI (mobile) ..."
        python3 "$PSI_SCRIPT" --url "$BORE_URL_INIT" --strategy mobile  --output "$INIT_PSI_MOBILE"  || true
        echo "[run] Running initial PSI (desktop) ..."
        python3 "$PSI_SCRIPT" --url "$BORE_URL_INIT" --strategy desktop --output "$INIT_PSI_DESKTOP" || true
      else
        echo "[run] WARN: bore tunnel did not come up for baseline PSI (ID=$ID) — skipping"
      fi

      kill "$INIT_BORE_PID" 2>/dev/null || true
      wait "$INIT_BORE_PID" 2>/dev/null || true
    else
      echo "[run] WARN: Baseline server never became ready (ID=$ID) — skipping initial PSI"
    fi

    bench_stop_host "$INIT_HOST_PID"
  fi

  # -------------------------
  # 5) Run agent (or reuse pre-existing patch when --skip-agent / --patch-results-dir)
  # -------------------------
  local AGENT_LOG PATCH_FILE USAGE_JSON
  AGENT_LOG="$JOB_DIR/agent.log"
  PATCH_FILE="$JOB_DIR/${JOB_LABEL}.patch"
  USAGE_JSON="$JOB_DIR/usage.json"
  export EVAL_JOB_LABEL="$JOB_LABEL"
  export EVAL_JOB_ID="$ID"
  export EVAL_AGENT_NAME="$AGENT_NAME"
  rm -f "$PATCH_FILE"

  if [[ "${SKIP_AGENT:-0}" == "1" ]]; then
    echo "[run] --skip-agent: skipping agent for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
    # If a patch results dir was supplied, copy the pre-existing patch into place.
    if [[ -n "${PATCH_RESULTS_DIR:-}" ]]; then
      local _SRC_PATCH=""
      # Support both new grouped layout (PATCH_RESULTS_DIR/JOB_LABEL/JOB_LABEL.patch)
      # and old flat layout (PATCH_RESULTS_DIR/JOB_LABEL.patch)
      if [[ -f "$PATCH_RESULTS_DIR/$JOB_LABEL/${JOB_LABEL}.patch" ]]; then
        _SRC_PATCH="$PATCH_RESULTS_DIR/$JOB_LABEL/${JOB_LABEL}.patch"
      elif [[ -f "$PATCH_RESULTS_DIR/${JOB_LABEL}.patch" ]]; then
        _SRC_PATCH="$PATCH_RESULTS_DIR/${JOB_LABEL}.patch"
      fi
      if [[ -n "$_SRC_PATCH" ]]; then
        cp "$_SRC_PATCH" "$PATCH_FILE"
        echo "[run] Using pre-existing patch: $_SRC_PATCH"
      else
        echo "[run] WARN: No pre-existing patch for $JOB_LABEL — proceeding with empty patch"
        touch "$PATCH_FILE"
      fi
    fi
  else
    local _AGENT_T0=$SECONDS
    # Serialize all large CWV vars to a JSON file so they survive past exec() without
    # hitting ARG_MAX / E2BIG (CLS_SHIFTS_DESKTOP alone can be 180KB).
    local _CWV_DATA_FILE="$JOB_DIR/cwv_data.json"
    # Use bash builtins only (no subprocess) so this write cannot itself trigger E2BIG.
    # The values from the CSV are already valid JSON (objects/arrays) or empty/space.
    {
      local _cls_m _cls_d _inp_m _inp_d _lcp_m _lcp_d _cwv_m _cwv_d
      _cls_m="${CLS_SHIFTS_MOBILE:-}";        [[ -z "$_cls_m"  || "$_cls_m"  == " " ]] && _cls_m="null"
      _cls_d="${CLS_SHIFTS_DESKTOP:-}";       [[ -z "$_cls_d"  || "$_cls_d"  == " " ]] && _cls_d="null"
      _inp_m="${INP_INTERACTIONS_MOBILE:-}";  [[ -z "$_inp_m"  || "$_inp_m"  == " " ]] && _inp_m="null"
      _inp_d="${INP_INTERACTIONS_DESKTOP:-}"; [[ -z "$_inp_d"  || "$_inp_d"  == " " ]] && _inp_d="null"
      _lcp_m="${LCP_ENTRIES_MOBILE:-}";       [[ -z "$_lcp_m"  || "$_lcp_m"  == " " ]] && _lcp_m="null"
      _lcp_d="${LCP_ENTRIES_DESKTOP:-}";      [[ -z "$_lcp_d"  || "$_lcp_d"  == " " ]] && _lcp_d="null"
      _cwv_m="${CWV_BASELINE_MOBILE:-}";      [[ -z "$_cwv_m"  || "$_cwv_m"  == " " ]] && _cwv_m="null"
      _cwv_d="${CWV_BASELINE_DESKTOP:-}";     [[ -z "$_cwv_d"  || "$_cwv_d"  == " " ]] && _cwv_d="null"
      printf '{"CLS_SHIFTS_MOBILE":%s,"CLS_SHIFTS_DESKTOP":%s,"INP_INTERACTIONS_MOBILE":%s,"INP_INTERACTIONS_DESKTOP":%s,"LCP_ENTRIES_MOBILE":%s,"LCP_ENTRIES_DESKTOP":%s,"CWV_BASELINE_MOBILE":%s,"CWV_BASELINE_DESKTOP":%s}\n' \
        "$_cls_m" "$_cls_d" "$_inp_m" "$_inp_d" "$_lcp_m" "$_lcp_d" "$_cwv_m" "$_cwv_d"
    } > "$_CWV_DATA_FILE"
    export EVAL_CWV_DATA_FILE="$_CWV_DATA_FILE"
    unset CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP \
          LCP_ENTRIES_MOBILE LCP_ENTRIES_DESKTOP CWV_BASELINE_MOBILE CWV_BASELINE_DESKTOP
    local AGENT_PATH="$AGENT"
    [[ "$AGENT_PATH" = /* ]] || AGENT_PATH="$SCRIPT_DIR/$AGENT_PATH"
    bash "$AGENT_PATH" \
      "$REPO_DIR" \
      "$TASK_SPEC" \
      "$AGENT_LOG" \
      "$PATCH_FILE" \
      </dev/null \
      || echo "[agent] Agent failed (continuing)"
    local _AGENT_WALL=$(( SECONDS - _AGENT_T0 ))

    # Merge wall clock time into usage JSON (agent writes cost/tokens/tool_calls there)
    if [[ -f "$USAGE_JSON" ]]; then
      python3 -c "
import json
with open('$USAGE_JSON') as f:
    d = json.load(f)
d['wall_clock_seconds'] = $_AGENT_WALL
with open('$USAGE_JSON', 'w') as f:
    json.dump(d, f, indent=2)
" 2>/dev/null || true
    else
      echo "{\"wall_clock_seconds\": $_AGENT_WALL}" > "$USAGE_JSON"
    fi
    echo "[run] Agent wall time: ${_AGENT_WALL}s (ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW})"
  fi

  # -------------------------
  # 6) Normalize patch (reset to baseline + apply patch only)
  # -------------------------
  if [[ -d "$REPO_DIR/.git" ]]; then
    # If agent didn't write patch, capture diff
    if [[ ! -s "$PATCH_FILE" ]]; then
      git -C "$REPO_DIR" add -A >/dev/null 2>&1
      git -C "$REPO_DIR" diff --cached > "$PATCH_FILE" 2>/dev/null || true
    fi

    git -C "$REPO_DIR" reset --hard HEAD >/dev/null 2>&1 || true
    git -C "$REPO_DIR" clean -fd >/dev/null 2>&1 || true

    if ! bench_git_apply_patch "$REPO_DIR" "$PATCH_FILE" "$JOB_DIR" "[run]"; then
      echo "[run] ERROR: patch failed to apply; skipping measurement for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
      rm -rf "$RUN_DIR"
      return 1
    fi
  fi

  # Skip measurement phases if requested
  if [[ "${SKIP_CWV_MEASURE:-0}" == "1" ]]; then
    echo "[run] SKIP_CWV_MEASURE=1; skipping measurement for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
    rm -rf "$RUN_DIR"
    echo "✓ Done: ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
    return 0
  fi

  # -------------------------
  # 7) Launch final HTTP server (patched repo)
  # -------------------------
  local HOST_LOG HOST_PID HOST_READY PORT_ATTEMPT
  HOST_PID=""
  HOST_READY=0
  for PORT_ATTEMPT in $(seq 0 $((PORT_RETRY_ATTEMPTS - 1))); do
    PORT="$(bench_port_for_slot "$BASE_PORT" "$SLOT" "$PORT_ATTEMPT" "$PARALLEL")"
    HOST_LOG="$JOB_DIR/host.log"
    [[ "$PORT_ATTEMPT" -gt 0 ]] && HOST_LOG="$JOB_DIR/host_retry_${PORT_ATTEMPT}.log"
    echo "[run] Starting final HTTP server on port $PORT (attempt $((PORT_ATTEMPT + 1))/$PORT_RETRY_ATTEMPTS) ..."
    if ! bench_start_host "$REPO_DIR" "$JOB_DIR" "$HOST_FILE_PATH" "$FRAMEWORK" "$PORT" "$HOST_LOG" "$SLOT"; then
      echo "WARN: Host tool failed on port $PORT (ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW})"
      tail -n 50 "$HOST_LOG" 2>/dev/null || true
      continue
    fi
    HOST_PID="$BENCH_HOST_HANDLE"

    if wait_for_server "$PORT" 90; then
      HOST_READY=1
      break
    fi

    echo "WARN: Patched site never became ready on port $PORT (ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW})"
    tail -n 50 "$HOST_LOG" 2>/dev/null || true
    bench_stop_host "$HOST_PID"
    HOST_PID=""
  done

  if [[ "$HOST_READY" != "1" ]]; then
    echo "ERROR: Patched site never became ready after $PORT_RETRY_ATTEMPTS port attempts (ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW})"
    rm -rf "$RUN_DIR"
    return 1
  fi

  # -------------------------
  # 7b) Open bore tunnel (only needed for PSI — CWV runs against localhost)
  # -------------------------
  local BORE_LOG BORE_PID BORE_URL_FINAL
  BORE_URL_FINAL=""
  BORE_PID=""
  if [[ "${SKIP_FINAL_PSI:-0}" != "1" ]] && command -v bore &>/dev/null; then
    BORE_LOG="$JOB_DIR/bore.log"
    RUST_LOG=info bore local "$PORT" --to bore.pub > "$BORE_LOG" 2>&1 &
    BORE_PID=$!
    BORE_URL_FINAL=$(wait_for_bore_url "$BORE_LOG" 30) || BORE_URL_FINAL=""
    if [[ -z "$BORE_URL_FINAL" ]]; then
      echo "[run] WARN: bore tunnel did not come up — skipping PSI (ID=$ID)"
    else
      echo "[run] Final bore URL: $BORE_URL_FINAL"
    fi
  fi

  # -------------------------
  # 8) Final PSI measurement (post-patch) — requires bore tunnel
  # -------------------------
  if [[ "${SKIP_FINAL_PSI:-0}" == "1" ]]; then
    echo "[run] --skip-final-psi set; skipping final PSI for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
  elif [[ -z "$BORE_URL_FINAL" ]]; then
    echo "[run] No bore URL; skipping final PSI for ID=$ID"
  else
    local FINAL_PSI_MOBILE FINAL_PSI_DESKTOP
    FINAL_PSI_MOBILE="$JOB_DIR/final_psi_mobile.json"
    FINAL_PSI_DESKTOP="$JOB_DIR/final_psi_desktop.json"

    echo "[run] Running final PSI (mobile) ..."
    python3 "$PSI_SCRIPT" --url "$BORE_URL_FINAL" --strategy mobile  --output "$FINAL_PSI_MOBILE"  || true
    echo "[run] Running final PSI (desktop) ..."
    python3 "$PSI_SCRIPT" --url "$BORE_URL_FINAL" --strategy desktop --output "$FINAL_PSI_DESKTOP" || true
  fi

  [[ -n "$BORE_PID" ]] && { kill "$BORE_PID" 2>/dev/null || true; wait "$BORE_PID" 2>/dev/null || true; }

  # -------------------------
  # 9) Visual validation (screenshot + AI eval) — runs first to gate CWV
  # -------------------------
  local _VISUAL_REGRESSED=0
  if [[ "${SKIP_VISUAL:-0}" == "1" ]]; then
    echo "[run] --skip-visual set; skipping visual validation for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
  else
    local SCREENSHOT_PATH VISUAL_JSON
    SCREENSHOT_PATH="$JOB_DIR/screenshot.png"
    VISUAL_JSON="$JOB_DIR/visual.json"
    local VISUAL_GPU_ID=""
    local VISUAL_ENV=()
    if [[ "$VISUAL_USE_GPU" == "1" && "${VISUAL_GPU_COUNT:-0}" =~ ^[0-9]+$ && "${VISUAL_GPU_COUNT:-0}" -gt 0 ]]; then
      VISUAL_GPU_ID=$((SLOT % VISUAL_GPU_COUNT))
      VISUAL_ENV=("CUDA_VISIBLE_DEVICES=$VISUAL_GPU_ID" "WEBBENCH_OCR_GPU=1")
      echo "[run] Visual GPU: slot=$SLOT gpu=$VISUAL_GPU_ID"
    else
      VISUAL_ENV=("WEBBENCH_OCR_GPU=0")
    fi
    local VISUAL_SLOT_JSON=""
    VISUAL_SLOT_JSON="$(bench_slot_json "$SLOT" docker 2>>"$JOB_DIR/visual.stderr")"
    local VISUAL_SLOT_ARGS=()
    [[ -n "$VISUAL_SLOT_JSON" ]] && VISUAL_SLOT_ARGS=(--slot-json "$VISUAL_SLOT_JSON")
    local VISUAL_BASELINE_ARGS=()
    [[ -n "$VISUAL_BASELINE_DIR" ]] && VISUAL_BASELINE_ARGS=(--baseline-dir "$VISUAL_BASELINE_DIR")
    if [[ "${REGRESSION_MEASURE_SANDBOX:-docker}" != "local" && "$HOST_PID" == docker:* ]]; then
      env "PYTHONPATH=$SCRIPT_DIR/../src${PYTHONPATH:+:$PYTHONPATH}" "${VISUAL_ENV[@]}" python3 -m docker_tool visual \
        --url "http://localhost:$PORT" \
        --screenshot-path "$SCREENSHOT_PATH" \
        --repo-id "$REPO_ID" \
        --commit-id "${COMMIT_ID_CLEAN:-}" \
        --framework "${FRAMEWORK:-Static HTML}" \
        --host-file-path "${HOST_FILE_PATH:-}" \
        --patch-file "$PATCH_FILE" \
        --output-json "$VISUAL_JSON" \
        --host-container-id "${HOST_PID#docker:}" \
        "${VISUAL_SLOT_ARGS[@]}"
    else
      env "${VISUAL_ENV[@]}" python3 "$VISUAL_SCRIPT" \
        --url "http://localhost:$PORT" \
        --screenshot-path "$SCREENSHOT_PATH" \
        --repo-id "$REPO_ID" \
        --commit-id "${COMMIT_ID_CLEAN:-}" \
        --framework "${FRAMEWORK:-Static HTML}" \
        --host-file-path "${HOST_FILE_PATH:-}" \
        --patch-file "$PATCH_FILE" \
        --output-json "$VISUAL_JSON" \
        "${VISUAL_SLOT_ARGS[@]}" \
        "${VISUAL_BASELINE_ARGS[@]}"
    fi
    # Check overall_regression from output JSON
    if [[ -f "$VISUAL_JSON" ]]; then
      _VISUAL_REGRESSED=$(python3 -c "
import json, sys
d = json.load(open('$VISUAL_JSON'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
    fi
    if [[ "$_VISUAL_REGRESSED" == "1" && "${ALLOW_CWV_AFTER_VISUAL_REGRESSION:-0}" != "1" ]]; then
      echo "[run] Visual regression detected — skipping CWV for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
    elif [[ "$_VISUAL_REGRESSED" == "1" ]]; then
      echo "[run] Visual regression detected; continuing to CWV because ALLOW_CWV_AFTER_VISUAL_REGRESSION=1 for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
    fi
  fi

  # -------------------------
  # 9b) Measure CWV (post-patch)
  # -------------------------
  if [[ "${SKIP_CWV:-0}" == "1" ]]; then
    echo "[run] --skip-cwv set; skipping CWV measurement for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
  elif [[ "$_VISUAL_REGRESSED" == "1" && "${ALLOW_CWV_AFTER_VISUAL_REGRESSION:-0}" != "1" ]]; then
    echo "[run] Skipping CWV (visual regression) for ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
  else
    local RESULT_MOBILE RESULT_DESKTOP CWV_STDERR
    RESULT_MOBILE="$JOB_DIR/mobile.json"
    RESULT_DESKTOP="$JOB_DIR/desktop.json"
    CWV_STDERR="$JOB_DIR/cwv_stderr.txt"

    bench_measure_cwv "http://localhost:$PORT" mobile "$NUM_RUNS" "$RESULT_MOBILE" "$CWV_STDERR" "$HOST_PID" "$SLOT" || true
    bench_measure_cwv "http://localhost:$PORT" desktop "$NUM_RUNS" "$RESULT_DESKTOP" "$CWV_STDERR" "$HOST_PID" "$SLOT" || true

    echo "RESULT_MOBILE=$RESULT_MOBILE"
    echo "RESULT_DESKTOP=$RESULT_DESKTOP"
  fi

  # -------------------------
  # 10) Teardown
  # -------------------------
  bench_stop_host "$HOST_PID"
  rm -rf "$RUN_DIR"

  echo "✓ Done: ID=$ID Agent=$AGENT_NAME${SUGG_IDX_RAW:+, sug=$SUGG_IDX_RAW}"
}

# =========================
# Job pool (slot tracking)
# =========================
declare -A JOB_SLOT=()   # pid -> slot index (0..PARALLEL-1)
_SLOT=0               # output variable for acquire_slot (avoids subshell)

# Block until a slot is available; sets _SLOT to the acquired index.
# Must be called directly (not via $()) so it can modify JOB_SLOT in-place.
# Polls every 0.5 s; reaps finished jobs to reclaim their slots.
acquire_slot() {
  while true; do
    local pid count s used p
    count=${#JOB_SLOT[@]}
    # Reap any finished jobs
    if [[ $count -gt 0 ]]; then
      for pid in "${!JOB_SLOT[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
          _SLOT="${JOB_SLOT[$pid]}"
          unset "JOB_SLOT[$pid]"
          return 0
        fi
      done
    fi
    # If under capacity, find the first unused slot
    if [[ $count -lt $PARALLEL ]]; then
      for s in $(seq 0 $((PARALLEL - 1))); do
        used=0
        if [[ $count -gt 0 ]]; then
          for p in "${!JOB_SLOT[@]}"; do
            [[ "${JOB_SLOT[$p]}" == "$s" ]] && used=1 && break
          done
        fi
        if [[ $used -eq 0 ]]; then
          _SLOT="$s"
          return 0
        fi
      done
    fi
    sleep 0.5
  done
}

# =========================
# Dispatch loop
# =========================
# When SUGGESTIONS_FILE is empty, Python emits one line per CSV row with placeholder suggestion
# columns → same job count and artifact names (ID_AGENT) as before the suggestions feature.
while IFS=$'\t' read -r \
  ID REPO_ID FRAMEWORK COMMIT_ID ZIP_REPO_PATH HOST_FILE_PATH \
  CWV_MOBILE CWV_DESKTOP LCP_ENTRIES_DESKTOP LCP_ENTRIES_MOBILE \
  CLS_SHIFTS_MOBILE CLS_SHIFTS_DESKTOP INP_INTERACTIONS_MOBILE INP_INTERACTIONS_DESKTOP \
  SUGG_PATH SUGG_IDX PAGE_URL MIRROR_DIR DOMAIN
do
  for AGENT in "${AGENTS[@]}"; do
    acquire_slot          # sets _SLOT; modifies JOB_SLOT in the parent shell
    slot=$_SLOT
    (
      run_job \
        "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" \
        "$CWV_MOBILE" "$CWV_DESKTOP" "$LCP_ENTRIES_DESKTOP" "$LCP_ENTRIES_MOBILE" \
        "$CLS_SHIFTS_MOBILE" "$CLS_SHIFTS_DESKTOP" "$INP_INTERACTIONS_MOBILE" "$INP_INTERACTIONS_DESKTOP" \
        "$AGENT" "$slot" \
        "$SUGG_PATH" "$SUGG_IDX" "$PAGE_URL" "$MIRROR_DIR" "$DOMAIN"
    ) &
    JOB_SLOT[$!]=$slot
  done
done < <(
if [[ "$SOURCE_MODE" == "live" ]]; then
  python3 "$SCRIPT_DIR/lib/live_source.py" \
    --jsonl "$INPUT_JSONL" \
    --mirrors-root "$MIRRORS_ROOT" \
    --limit "$LIMIT"
else
python3 - <<'PY' "$CSV" "$LIMIT" "${SUGGESTIONS_FILE:-}" "${SUGGESTION_INDICES:-}"
import csv, json, os, sys

csv.field_size_limit(sys.maxsize)
csv_path = sys.argv[1]
limit_s = sys.argv[2] if len(sys.argv) > 2 else ""
sug_path = (sys.argv[3] if len(sys.argv) > 3 else "").strip()
indices_raw = (sys.argv[4] if len(sys.argv) > 4 else "").strip()
limit = int(limit_s) if limit_s else None

cols = [
  "ID","REPO_ID","FRAMEWORK","COMMIT_ID","ZIP_REPO_PATH","HOST_FILE_PATH",
  "CWV_MOBILE","CWV_DESKTOP","LCP_ENTRIES_DESKTOP","LCP_ENTRIES_MOBILE",
  "CLS_SHIFTS_MOBILE","CLS_SHIFTS_DESKTOP","INP_INTERACTIONS_MOBILE","INP_INTERACTIONS_DESKTOP",
]

def row_tuple(row):
  out = []
  for c in cols:
    v = row.get(c, "")
    if v is None:
      v = ""
    v = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    if v == "":
      v = " "
    out.append(v)
  return out

sug_abs = ""
sug_indices = []
if sug_path:
  if not os.path.isfile(sug_path):
    print(f"ERROR: SUGGESTIONS_FILE not found: {sug_path}", file=sys.stderr)
    sys.exit(1)
  sug_abs = os.path.abspath(sug_path)
  with open(sug_abs, encoding="utf-8") as f:
    data = json.load(f)
  sugs = data.get("suggestions", [])
  if not sugs:
    print("ERROR: JSON has no entries in 'suggestions'", file=sys.stderr)
    sys.exit(1)
  if indices_raw:
    for part in indices_raw.split(","):
      part = part.strip()
      if not part:
        continue
      sug_indices.append(int(part))
  else:
    sug_indices = list(range(len(sugs)))
  for i in sug_indices:
    if i < 0 or i >= len(sugs):
      print(f"ERROR: suggestion index {i} out of range (0..{len(sugs)-1})", file=sys.stderr)
      sys.exit(1)

n = 0
with open(csv_path, newline="", encoding="utf-8") as f:
  r = csv.DictReader(f)
  for row in r:
    base = row_tuple(row)
    if sug_abs:
      for idx in sug_indices:
        print("\t".join(base + [sug_abs, str(idx), " ", " ", " "]))
    else:
      print("\t".join(base + [" ", " ", " ", " ", " "]))
    n += 1
    if limit is not None and n >= limit:
      break
PY
fi
)

# Wait for remaining benchmark job subshells only. Do not use a bare wait here:
# --serve-model leaves vLLM / usage-proxy background processes alive until the
# EXIT trap, and waiting on all background jobs would block forever.
for pid in "${!JOB_SLOT[@]}"; do
  if kill -0 "$pid" 2>/dev/null; then
    wait "$pid" || true
  fi
done
if [[ -n "$OPEN_MODEL_USAGE_DIR" && -f "$OPEN_MODEL_USAGE_DIR/api_calls.jsonl" ]]; then
  python3 "$SCRIPT_DIR/opensource_models/aggregate_usage.py" \
    --input "$OPEN_MODEL_USAGE_DIR/api_calls.jsonl" \
    --output-dir "$OPEN_MODEL_USAGE_DIR" \
    || echo "[model] WARN: usage aggregation failed"
fi
echo "[run] All jobs complete."
