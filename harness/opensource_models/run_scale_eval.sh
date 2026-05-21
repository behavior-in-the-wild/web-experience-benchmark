#!/usr/bin/env bash
# Sequential size-scaling evaluation across the Qwen3.5 model family.
# Each model is served, evaluated, then fully torn down before the next starts.
# All model configs are embedded here — no separate serve_*.sh scripts needed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Smallest → largest so early results are available quickly.
MODELS=(
  qwen3.5-9b
  qwen3.5-27b
  qwen3.5-35b-a3b
  qwen3.5-122b-a10b
  qwen3.5-397b-a17b
)

CSV_PATH="$HARNESS_DIR/SAMPLE/input_100.csv"
PARALLEL=50
PARALLEL_OVERRIDE=""   # set to 1 when --parallel is given on CLI
VLLM_BASE_PORT="${VLLM_BASE_PORT:-8000}"
VLLM_BIND_HOST="${VLLM_BIND_HOST:-0.0.0.0}"
VLLM_CLIENT_HOST="${VLLM_CLIENT_HOST:-127.0.0.1}"
VLLM_READY_TIMEOUT="${VLLM_READY_TIMEOUT:-1800}"
USAGE_PROXY_BASE_PORT="${USAGE_PROXY_BASE_PORT:-9000}"
USAGE_PROXY_HOST="${USAGE_PROXY_HOST:-127.0.0.1}"

usage() {
  cat <<'EOF'
Usage: harness/opensource_models/run_scale_eval.sh [options] [-- evaluate.sh args...]

Models (run sequentially, smallest to largest):
  qwen3.5-9b          Qwen/Qwen3.5-9B          (9B dense,         TP=4)
  qwen3.5-27b         Qwen/Qwen3.5-27B         (27B dense,        TP=8)
  qwen3.5-35b-a3b     Qwen/Qwen3.5-35B-A3B     (35B MoE, 3B act,  TP=8)
  qwen3.5-122b-a10b   Qwen/Qwen3.5-122B-A10B   (122B MoE, 10B act,TP=8)
  qwen3.5-397b-a17b   Qwen/Qwen3.5-397B-A17B-FP8 (397B MoE, 17B act, FP8, TP=8)

Options:
  --models A,B,C       Comma-separated subset of models to run.
  --csv PATH           CSV to evaluate (default: harness/SAMPLE/input_100.csv)
  --parallel N         Override per-model parallel agents (default: per-model — 9b:100, 27b:80, 35b:80, 122b:60, 397b:40)
  --vllm-base-port N   First vLLM port (default: 8000)
  --proxy-base-port N  First usage proxy port (default: 9000)
  --help, -h           Show this message

Environment:
  TENSOR_PARALLEL_SIZE, GPU_MEMORY_UTILIZATION, DTYPE, MAX_MODEL_LEN,
  MAX_NUM_BATCHED_TOKENS, QUANTIZATION, VLLM_EXTRA_ARGS
  are forwarded to serve_model.sh and override per-model defaults.

Output:
  harness/out/scale_eval/<timestamp>/<model>/
    vllm.log
    usage_proxy.log
    usage/api_calls.jsonl
    usage/summary.json
    usage/by_job.csv
    (agent results via evaluate.sh)
EOF
}

# ── Model config lookups ──────────────────────────────────────────────────────

hf_id_for_model() {
  case "$1" in
    qwen3.5-9b)          echo "Qwen/Qwen3.5-9B" ;;
    qwen3.5-27b)         echo "Qwen/Qwen3.5-27B" ;;
    qwen3.5-35b-a3b)     echo "Qwen/Qwen3.5-35B-A3B" ;;
    qwen3.5-122b-a10b)   echo "Qwen/Qwen3.5-122B-A10B" ;;
    qwen3.5-397b-a17b)   echo "Qwen/Qwen3.5-397B-A17B-FP8" ;;
    *) echo "Unknown model: $1" >&2; return 1 ;;
  esac
}

# Per-model tensor parallel size.
# 9B dense (~18 GB BF16) fits on 1 GPU but TP=4 improves throughput for 50 concurrent agents.
# 27B dense (~54 GB BF16) and all MoE variants use TP=8.
# 397B-A17B MoE (~794 GB BF16) exceeds 8×80 GB; set QUANTIZATION=fp8 or DTYPE=float8 to fit.
tp_for_model() {
  case "$1" in
    qwen3.5-9b)          echo "${TENSOR_PARALLEL_SIZE:-4}" ;;
    qwen3.5-27b)         echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    qwen3.5-35b-a3b)     echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    qwen3.5-122b-a10b)   echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    qwen3.5-397b-a17b)   echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    *) echo "${TENSOR_PARALLEL_SIZE:-1}" ;;
  esac
}

# Per-model parallel agent counts, tuned to step latency and KV cache headroom.
# 9B/35B-A3B: small active weight footprint → fast steps → push to 100.
# 27B/35B-A3B: moderate KV pressure → 80.
# 122B-A10B: large KV cache per token → 60.
# 397B-A17B: fp8 quantised, huge KV → conservative 40.
# CLI --parallel overrides all of these.
parallel_for_model() {
  [[ -n "$PARALLEL_OVERRIDE" ]] && echo "$PARALLEL" && return
  case "$1" in
    qwen3.5-9b)          echo 100 ;;
    qwen3.5-27b)         echo 80  ;;
    qwen3.5-35b-a3b)     echo 80  ;;
    qwen3.5-122b-a10b)   echo 60  ;;
    qwen3.5-397b-a17b)   echo 40  ;;
    *)                   echo "$PARALLEL" ;;
  esac
}

# All Qwen3.5 models share a 262K native context.
# Reserve ~40K for input/tool history; pass the rest as max output tokens.
max_tokens_for_model() { echo "220000"; }

# ── Inline vLLM launcher (replaces individual serve_*.sh files) ───────────────
# All Qwen3.5 models share the same parser flags; only MODEL_ID and TP differ.
start_vllm_for_model() {
  local model="$1" port="$2"
  MODEL_ID="$(hf_id_for_model "$model")" \
  SERVED_MODEL_NAME="$model" \
  VLLM_HOST="$VLLM_BIND_HOST" \
  VLLM_PORT="$port" \
  TENSOR_PARALLEL_SIZE="$(tp_for_model "$model")" \
  TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_coder}" \
  REASONING_PARSER="${REASONING_PARSER:-qwen3}" \
  MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}" \
  MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-65536}" \
    "$SCRIPT_DIR/serve_model.sh"
}

# ── CLI parsing ───────────────────────────────────────────────────────────────

cli_models=()
eval_args=()
models_from_flag=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --models)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --models A,B,C"; exit 1; }
      models_from_flag="$1"; shift ;;
    --csv)
      shift
      [[ $# -gt 0 ]] || { echo "Usage: --csv PATH"; exit 1; }
      CSV_PATH="$1"; shift ;;
    --parallel)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N"; exit 1; }
      PARALLEL="$1"; PARALLEL_OVERRIDE=1; shift ;;
    --vllm-base-port)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --vllm-base-port N"; exit 1; }
      VLLM_BASE_PORT="$1"; shift ;;
    --proxy-base-port)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --proxy-base-port N"; exit 1; }
      USAGE_PROXY_BASE_PORT="$1"; shift ;;
    --help|-h) usage; exit 0 ;;
    --)
      shift; eval_args+=("$@"); break ;;
    --*)
      eval_args+=("$@"); break ;;
    *)
      cli_models+=("$1"); shift ;;
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
[[ -f "$CSV_PATH" ]]               || { echo "Missing CSV: $CSV_PATH"; exit 1; }
[[ -f "$HARNESS_DIR/evaluate.sh" ]] || { echo "Missing evaluate.sh"; exit 1; }

# ── Output directory ──────────────────────────────────────────────────────────

RUN_TS="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$HARNESS_DIR/out/scale_eval/$RUN_TS"
mkdir -p "$LOG_DIR"

vllm_pid=""
proxy_pid=""

# ── Process-group kill + VRAM drain ──────────────────────────────────────────

_kill_vllm() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  local pgid
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" || pgid=""
  kill "$pid" 2>/dev/null || true
  [[ -n "$pgid" && "$pgid" != "0" && "$pgid" != "1" ]] && \
    kill -- "-$pgid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  sleep 8
  local used_mb
  used_mb="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
    | awk 'BEGIN{s=0}{s+=$1}END{print s}')" || used_mb="?"
  echo "[scale-eval] GPU memory after vLLM stop: ${used_mb} MiB used"
}

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

# ── Health-check helpers ──────────────────────────────────────────────────────

wait_for_vllm() {
  local port="$1" deadline="$2"
  local url="http://${VLLM_CLIENT_HOST}:${port}/v1/models"
  local i
  for i in $(seq 1 "$deadline"); do
    if curl -fs -H "Authorization: Bearer ${VLLM_API_KEY:-EMPTY}" "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_proxy() {
  local port="$1" deadline="${2:-30}"
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

# ── Main evaluation loop ──────────────────────────────────────────────────────

for idx in "${!MODELS[@]}"; do
  model="${MODELS[$idx]}"
  model="${model#"${model%%[![:space:]]*}"}"
  model="${model%"${model##*[![:space:]]}"}"
  [[ -n "$model" ]] || continue

  port=$((VLLM_BASE_PORT + idx))
  proxy_port=$((USAGE_PROXY_BASE_PORT + idx))
  model_dir="$LOG_DIR/$model"
  usage_dir="$model_dir/usage"
  mkdir -p "$usage_dir"
  log_file="$model_dir/vllm.log"
  proxy_log_file="$model_dir/usage_proxy.log"

  echo "[scale-eval] ── $model (HF: $(hf_id_for_model "$model"), TP=$(tp_for_model "$model")) ──"
  echo "[scale-eval] Starting vLLM on port $port"
  start_vllm_for_model "$model" "$port" >"$log_file" 2>&1 &
  vllm_pid=$!

  if ! wait_for_vllm "$port" "$VLLM_READY_TIMEOUT"; then
    echo "[scale-eval] ERROR: vLLM did not become ready for $model. Log: $log_file"
    exit 1
  fi
  echo "[scale-eval] vLLM ready for $model"

  echo "[scale-eval] Starting usage proxy on port $proxy_port"
  fuser -k "${proxy_port}/tcp" 2>/dev/null || true
  sleep 1
  python3 "$SCRIPT_DIR/usage_proxy.py" \
    --listen-host "$USAGE_PROXY_HOST" \
    --listen-port "$proxy_port" \
    --upstream-base "http://${VLLM_CLIENT_HOST}:${port}" \
    --output-dir "$usage_dir" \
    --model-label "$model" \
    --quiet \
    >"$proxy_log_file" 2>&1 &
  proxy_pid=$!

  if ! wait_for_proxy "$proxy_port" 30; then
    echo "[scale-eval] ERROR: usage proxy did not become ready for $model. Log: $proxy_log_file"
    exit 1
  fi

  model_parallel="$(parallel_for_model "$model")"
  echo "[scale-eval] Running evaluate.sh for $model with parallel=$model_parallel"
  set +e
  CSV="$CSV_PATH" \
  EVAL_OUT_DIR="$model_dir" \
  EVAL_AGENTS="agents/template_opencode_os.sh" \
  OPENCODE_OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_API_KEY="${VLLM_API_KEY:-EMPTY}" \
  OPENCODE_USAGE_PROXY=1 \
  VLLM_SERVED_MODEL_NAME="$model" \
  OPENCODE_MODEL="vllm/$model" \
  OPENCODE_MAX_TOKENS="$(max_tokens_for_model "$model")" \
    bash "$HARNESS_DIR/evaluate.sh" --parallel "$model_parallel" --skip-all "${eval_args[@]}"
  eval_status=$?
  set -e

  python3 "$SCRIPT_DIR/aggregate_usage.py" \
    --input "$usage_dir/api_calls.jsonl" \
    --output-dir "$usage_dir" \
    || echo "[scale-eval] WARN: usage aggregation failed for $model"

  echo "[scale-eval] Stopping usage proxy for $model"
  kill "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
  proxy_pid=""

  echo "[scale-eval] Stopping vLLM for $model"
  _kill_vllm "$vllm_pid"
  vllm_pid=""

  if [[ "$eval_status" -ne 0 ]]; then
    echo "[scale-eval] ERROR: evaluate.sh failed for $model with status $eval_status"
    exit "$eval_status"
  fi

  echo "[scale-eval] Completed $model ($((idx + 1))/${#MODELS[@]})"
done

echo "[scale-eval] All scale-eval runs complete. Results: $LOG_DIR"
