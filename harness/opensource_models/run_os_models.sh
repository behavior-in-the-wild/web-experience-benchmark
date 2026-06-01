#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/vllm_lifecycle_lib.sh"

# Edit this list when you want a default multi-model run with no CLI args.
MODELS=(
  gemma-4-31b-it
  glm-4.7-flash
  qwen3-coder-next
  gpt-oss-120b
  devstral-2-123b
  minimax-m2.7
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
  # glm-5.1          # INCOMPATIBLE: sparse MLA requires Hopper/Blackwell (SM90+)
  gemma-4-31b-it
  # deepseek-v4-flash # INCOMPATIBLE: HyperConnection kernel requires Hopper/Blackwell (SM90+)
  devstral-2-123b
  minimax-m2.7
  gpt-oss-120b

Options:
  --models A,B,C       Comma-separated models. Overrides positional models.
  --csv PATH           CSV to evaluate (default: harness/SAMPLE/input_100.csv)
  --parallel N         Passed to evaluate.sh (default: 50)
  --vllm-base-port N   First vLLM port (default: 8000)
  --proxy-base-port N  First usage proxy port (default: 9000)
  --resume-dir DIR     Resume into an existing output dir instead of creating out/<ts>/. New
                       results land in DIR/<model>/results/, overwriting failed artifacts in place.
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
RESUME_DIR=""

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
      _PARALLEL_OVERRIDE="$1"
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
    --resume-dir)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --resume-dir DIR"; exit 1; }
      RESUME_DIR="$1"
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

if [[ -n "$RESUME_DIR" ]]; then
  # Resolve to absolute path before any directory changes
  [[ "$RESUME_DIR" = /* ]] || RESUME_DIR="$(cd "$RESUME_DIR" && pwd)"
  [[ -d "$RESUME_DIR" ]] || { echo "Error: --resume-dir '$RESUME_DIR' does not exist"; exit 1; }
  LOG_DIR="$RESUME_DIR"
  echo "[os-models] Resuming into existing output dir: $LOG_DIR"
else
  RUN_TS="$(date +%Y%m%d_%H%M%S)"
  # All artifacts (vLLM logs, usage, agent results) live under one root:
  #   out/<ts>/<model>/vllm.log
  #   out/<ts>/<model>/usage_proxy.log
  #   out/<ts>/<model>/usage/
  #   out/<ts>/<model>/results/   ← agent patches, plans, logs
  LOG_DIR="$HARNESS_DIR/out/$RUN_TS"
  mkdir -p "$LOG_DIR"
fi

vllm_pid=""
proxy_pid=""

cleanup() {
  if [[ -n "$proxy_pid" ]]; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
  if [[ -n "$vllm_pid" ]]; then
    _kill_vllm "$vllm_pid"
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
    glm-5.1|glm5|glm51)
      echo "$SCRIPT_DIR/serve_glm_5_1.sh"
      ;;
    deepseek-v4-flash|deepseek|dsv4)
      echo "$SCRIPT_DIR/serve_deepseek_v4_flash.sh"
      ;;
    devstral-2-123b|devstral|devstral2)
      echo "$SCRIPT_DIR/serve_devstral_2_123b.sh"
      ;;
    minimax-m2.7|minimax|minimax-m2)
      echo "$SCRIPT_DIR/serve_minimax_m2.sh"
      ;;
    gpt-oss-120b|gptoss|gpt-oss)
      echo "$SCRIPT_DIR/serve_gpt_oss_120b.sh"
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
    glm-5.1|glm5|glm51) echo "glm-5.1" ;;
    deepseek-v4-flash|deepseek|dsv4) echo "deepseek-v4-flash" ;;
    devstral-2-123b|devstral|devstral2) echo "devstral-2-123b" ;;
    minimax-m2.7|minimax|minimax-m2) echo "minimax-m2.7" ;;
    gpt-oss-120b|gptoss|gpt-oss) echo "gpt-oss-120b" ;;
    *) return 1 ;;
  esac
}

# Max output tokens per model: native context minus ~40K headroom for input/history
max_tokens_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3) echo "220000" ;;  # 256K context
    glm-4.7-flash|glm|glm47)     echo "90000"  ;;  # 128K context
    gemma-4-31b-it|gemma|gemma4) echo "220000" ;;  # 256K context
    glm-5.1|glm5|glm51)              echo "24000"  ;;  # 64K context (AWQ 4-bit, ~384GB weights)
    deepseek-v4-flash|deepseek|dsv4) echo "90000"  ;;  # 128K context
    devstral-2-123b|devstral|devstral2) echo "90000" ;;  # 131K context (FP8, 123B dense)
    minimax-m2.7|minimax|minimax-m2) echo "90000"  ;;  # 128K context (BF16 229B MoE, fp8 KV cache)
    gpt-oss-120b|gptoss|gpt-oss)     echo "90000"  ;;  # 131K context (MXFP4, ~58GB weights)
    *) echo "16000" ;;
  esac
}

# Per-model evaluate.sh parallelism (number of concurrent tasks).
# Caller-supplied --parallel overrides these defaults.
parallel_for_model() {
  [[ -n "${_PARALLEL_OVERRIDE:-}" ]] && { echo "$_PARALLEL_OVERRIDE"; return; }
  case "$1" in
    gemma-4-31b-it|gemma|gemma4)       echo "32" ;;
    glm-4.7-flash|glm|glm47)           echo "32" ;;
    qwen3-coder-next|qwen|qwen3)       echo "32" ;;
    gpt-oss-120b|gptoss|gpt-oss)       echo "16" ;;
    devstral-2-123b|devstral|devstral2) echo "16" ;;
    minimax-m2.7|minimax|minimax-m2)   echo "16" ;;
    *) echo "${PARALLEL:-32}" ;;
  esac
}

# Per-model tensor parallel size.
# GLM-4.7-Flash has 20 attention heads — must be divisible; 4 works, 8 does not.
# Caller-supplied TENSOR_PARALLEL_SIZE overrides these defaults.
tp_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    glm-4.7-flash|glm|glm47)     echo "${TENSOR_PARALLEL_SIZE:-4}" ;;
    gemma-4-31b-it|gemma|gemma4) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    # GLM-5.1: 64 attention heads (full MHA, not GQA) → TP=8 works (64/8=8).
    glm-5.1|glm5|glm51) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    # DeepSeek-V4-Flash: 64 attention heads → TP=8 works (64/8=8).
    # MLA means num_key_value_heads=1 but vLLM handles MLA independently of TP.
    deepseek-v4-flash|deepseek|dsv4) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    # Devstral-2-123B: 96 attention heads, 8 KV heads (GQA) → TP=8 works (96/8=12, 8/8=1).
    devstral-2-123b|devstral|devstral2) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    # MiniMax-M2.7: 48 attention heads, 8 KV heads (GQA) → TP=8 works (48/8=6, 8/8=1).
    minimax-m2.7|minimax|minimax-m2) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    # GPT-OSS-120B: 64 attention heads, 8 KV heads (GQA) → TP=8 works (64/8=8, 8/8=1).
    gpt-oss-120b|gptoss|gpt-oss) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    *) echo "${TENSOR_PARALLEL_SIZE:-1}" ;;
  esac
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

  echo "[os-models] Starting vLLM for $served_name on port $port (TP=$(tp_for_model "$model"))"
  # Launch vLLM in its own process group via setsid so that _kill_vllm can
  # safely kill the whole group (vLLM + TP workers) without also killing this
  # script.  In non-interactive shells bash does not create new process groups
  # for background jobs, so without setsid kill -- -$pgid would SIGTERM us too.
  ( VLLM_HOST="$VLLM_BIND_HOST" \
    VLLM_PORT="$port" \
    SERVED_MODEL_NAME="$served_name" \
    TENSOR_PARALLEL_SIZE="$(tp_for_model "$model")" \
    setsid "$serve_script" >"$log_file" 2>&1 ) &
  vllm_pid=$!

  if ! wait_for_vllm "$port" "$VLLM_READY_TIMEOUT"; then
    echo "[os-models] ERROR: vLLM did not become ready for $served_name. Log: $log_file"
    exit 1
  fi

  echo "[os-models] Starting usage proxy for $served_name on port $proxy_port"
  # Clear any stale process occupying the proxy port before binding.
  fuser -k "${proxy_port}/tcp" 2>/dev/null || true
  sleep 1
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

  model_parallel="$(parallel_for_model "$model")"
  echo "[os-models] Running evaluate.sh for $served_name with parallel=$model_parallel"
  set +e
  CSV="$CSV_PATH" \
  EVAL_OUT_DIR="$model_dir" \
  EVAL_AGENTS="agents/template_opencode_os.sh" \
  OPENCODE_OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_API_KEY="${VLLM_API_KEY:-EMPTY}" \
  OPENCODE_USAGE_PROXY=1 \
  VLLM_SERVED_MODEL_NAME="$served_name" \
  OPENCODE_MODEL="vllm/$served_name" \
  OPENCODE_MAX_TOKENS="$(max_tokens_for_model "$model")" \
    bash "$HARNESS_DIR/evaluate.sh" --parallel "$model_parallel" --skip-all "${eval_args[@]}"
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
  _kill_vllm "$vllm_pid"
  vllm_pid=""

  if [[ "$eval_status" -ne 0 ]]; then
    echo "[os-models] ERROR: evaluate.sh failed for $served_name with status $eval_status"
    exit "$eval_status"
  fi
done

echo "[os-models] All requested model evaluations complete. vLLM logs: $LOG_DIR"
