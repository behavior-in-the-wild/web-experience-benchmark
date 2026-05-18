#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Edit this list when you want a default multi-model run with no CLI args.
MODELS=(
  qwen3-coder-next
  glm-4.7-flash
  gemma-4-31b-it
)

CSV_PATH="$HARNESS_DIR/SAMPLE/input_100.csv"
PARALLEL=50
VLLM_BASE_PORT="${VLLM_BASE_PORT:-8000}"
VLLM_BIND_HOST="${VLLM_BIND_HOST:-0.0.0.0}"
VLLM_CLIENT_HOST="${VLLM_CLIENT_HOST:-127.0.0.1}"
VLLM_READY_TIMEOUT="${VLLM_READY_TIMEOUT:-1800}"
USAGE_PROXY_BASE_PORT="${USAGE_PROXY_BASE_PORT:-9000}"
USAGE_PROXY_HOST="${USAGE_PROXY_HOST:-127.0.0.1}"

usage() {
  cat <<'EOF'
Usage: harness/opensource_models/run_os_models.sh [model ...] [options] [-- evaluate.sh args...]

Models:
  qwen3-coder-next
  glm-4.7-flash
  gemma-4-31b-it

Options:
  --models A,B,C       Comma-separated models. Overrides positional models.
  --csv PATH           CSV to evaluate (default: harness/SAMPLE/input_100.csv)
  --parallel N         Passed to evaluate.sh (default: 50)
  --vllm-base-port N   First vLLM port (default: 8000)
  --proxy-base-port N  First usage proxy port (default: 9000)
  --help, -h           Show this message

Environment:
  TENSOR_PARALLEL_SIZE, GPU_MEMORY_UTILIZATION, DTYPE, MAX_MODEL_LEN,
  MAX_NUM_BATCHED_TOKENS, VLLM_EXTRA_ARGS, TOOL_CALL_PARSER, CHAT_TEMPLATE
  are forwarded to the vLLM hosting scripts.

Output:
  harness/out/opensource_models/<timestamp>/<model>/
    vllm.log
    usage_proxy.log
    usage/api_calls.jsonl
    usage/errors.jsonl
    usage/summary.json
    usage/by_job.csv
    usage/by_phase.csv
    usage/by_job_phase.csv
EOF
}

cli_models=()
eval_args=()
models_from_flag=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --models)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --models A,B,C"; exit 1; }
      models_from_flag="$1"
      shift
      ;;
    --csv)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --csv PATH"; exit 1; }
      CSV_PATH="$1"
      shift
      ;;
    --parallel)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N"; exit 1; }
      PARALLEL="$1"
      shift
      ;;
    --vllm-base-port)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --vllm-base-port N"; exit 1; }
      VLLM_BASE_PORT="$1"
      shift
      ;;
    --proxy-base-port)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --proxy-base-port N"; exit 1; }
      USAGE_PROXY_BASE_PORT="$1"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      eval_args+=("$@")
      break
      ;;
    --*)
      eval_args+=("$@")
      break
      ;;
    *)
      cli_models+=("$1")
      shift
      ;;
  esac
done

if [[ -n "$models_from_flag" ]]; then
  IFS=',' read -r -a MODELS <<< "$models_from_flag"
elif [[ ${#cli_models[@]} -gt 0 ]]; then
  MODELS=("${cli_models[@]}")
fi

if [[ ! "$CSV_PATH" = /* ]]; then
  CSV_PATH="$(cd "$(dirname "$CSV_PATH")" && pwd)/$(basename "$CSV_PATH")"
fi

[[ -f "$CSV_PATH" ]] || { echo "Missing CSV: $CSV_PATH"; exit 1; }
[[ -x "$HARNESS_DIR/evaluate.sh" || -f "$HARNESS_DIR/evaluate.sh" ]] || { echo "Missing evaluate.sh"; exit 1; }

RUN_TS="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$HARNESS_DIR/out/opensource_models/$RUN_TS"
mkdir -p "$LOG_DIR"

vllm_pid=""
proxy_pid=""
cleanup() {
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
  if [[ -n "$vllm_pid" ]]; then
    kill "$vllm_pid" 2>/dev/null || true
    wait "$vllm_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

script_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3)
      echo "$SCRIPT_DIR/serve_qwen3_coder_next.sh"
      ;;
    glm-4.7-flash|glm|glm47)
      echo "$SCRIPT_DIR/serve_glm_4_7_flash.sh"
      ;;
    gemma-4-31b-it|gemma|gemma4)
      echo "$SCRIPT_DIR/serve_gemma_4_31b_it.sh"
      ;;
    *)
      echo "Unknown model: $1" >&2
      return 1
      ;;
  esac
}

served_name_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3) echo "qwen3-coder-next" ;;
    glm-4.7-flash|glm|glm47) echo "glm-4.7-flash" ;;
    gemma-4-31b-it|gemma|gemma4) echo "gemma-4-31b-it" ;;
    *) return 1 ;;
  esac
}

wait_for_vllm() {
  local port="$1"
  local deadline="$2"
  local url="http://${VLLM_CLIENT_HOST}:${port}/v1/models"
  local i
  for i in $(seq 1 "$deadline"); do
    if curl -fs "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_proxy() {
  local port="$1"
  local deadline="${2:-30}"
  local url="http://${USAGE_PROXY_HOST}:${port}/healthz"
  local i
  for i in $(seq 1 "$deadline"); do
    if curl -fs "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

for idx in "${!MODELS[@]}"; do
  model="${MODELS[$idx]}"
  model="${model#"${model%%[![:space:]]*}"}"
  model="${model%"${model##*[![:space:]]}"}"
  [[ -n "$model" ]] || continue

  serve_script="$(script_for_model "$model")"
  served_name="$(served_name_for_model "$model")"
  port=$((VLLM_BASE_PORT + idx))
  proxy_port=$((USAGE_PROXY_BASE_PORT + idx))
  model_dir="$LOG_DIR/$served_name"
  usage_dir="$model_dir/usage"
  mkdir -p "$usage_dir"
  log_file="$model_dir/vllm.log"
  proxy_log_file="$model_dir/usage_proxy.log"

  echo "[os-models] Starting vLLM for $served_name on port $port"
  VLLM_HOST="$VLLM_BIND_HOST" \
  VLLM_PORT="$port" \
  SERVED_MODEL_NAME="$served_name" \
    "$serve_script" >"$log_file" 2>&1 &
  vllm_pid=$!

  if ! wait_for_vllm "$port" "$VLLM_READY_TIMEOUT"; then
    echo "[os-models] ERROR: vLLM did not become ready for $served_name. Log: $log_file"
    exit 1
  fi

  echo "[os-models] Starting usage proxy for $served_name on port $proxy_port"
  python3 "$SCRIPT_DIR/usage_proxy.py" \
    --listen-host "$USAGE_PROXY_HOST" \
    --listen-port "$proxy_port" \
    --upstream-base "http://${VLLM_CLIENT_HOST}:${port}" \
    --output-dir "$usage_dir" \
    --model-label "$served_name" \
    --quiet \
    >"$proxy_log_file" 2>&1 &
  proxy_pid=$!

  if ! wait_for_proxy "$proxy_port" 30; then
    echo "[os-models] ERROR: usage proxy did not become ready for $served_name. Log: $proxy_log_file"
    exit 1
  fi

  echo "[os-models] Running evaluate.sh for $served_name with parallel=$PARALLEL"
  set +e
  CSV="$CSV_PATH" \
  EVAL_AGENTS="agents/template_opencode_os.sh" \
  OPENCODE_OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_API_KEY="${VLLM_API_KEY:-EMPTY}" \
  OPENCODE_USAGE_PROXY=1 \
  VLLM_SERVED_MODEL_NAME="$served_name" \
  OPENCODE_MODEL="openai/$served_name" \
    bash "$HARNESS_DIR/evaluate.sh" --parallel "$PARALLEL" --skip-all "${eval_args[@]}"
  eval_status=$?
  set -e

  python3 "$SCRIPT_DIR/aggregate_usage.py" \
    --input "$usage_dir/api_calls.jsonl" \
    --output-dir "$usage_dir" \
    || echo "[os-models] WARN: usage aggregation failed for $served_name"

  echo "[os-models] Stopping usage proxy for $served_name"
  kill "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
  proxy_pid=""
  echo "[os-models] Stopping vLLM for $served_name"
  kill "$vllm_pid" 2>/dev/null || true
  wait "$vllm_pid" 2>/dev/null || true
  vllm_pid=""

  if [[ "$eval_status" -ne 0 ]]; then
    echo "[os-models] ERROR: evaluate.sh failed for $served_name with status $eval_status"
    exit "$eval_status"
  fi
done

echo "[os-models] All requested model evaluations complete. vLLM logs: $LOG_DIR"
