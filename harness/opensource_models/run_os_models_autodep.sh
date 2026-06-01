#!/usr/bin/env bash
# =============================================================
#  Run the 5 paper-OSS models on the 100-site auto-deployed benchmark.
#
#  Differences vs run_os_models.sh:
#    * Defaults to the auto-deploy CSV at SAMPLE/autodep_100.csv
#    * Five-model default list (drops gpt-oss-120b, which is not in the
#      paper's OSS evaluation)
#    * Lower default --parallel (8) because each autodep host_*.sh runs
#      npm install + npm run build at startup, which is heavy
#    * Output lands in harness/out/autodep_models/<ts>/<model>/
#
#  Everything else (vLLM lifecycle, usage proxy, per-model serve scripts,
#  TP/parallel/max_tokens tables, resume support) is inherited verbatim.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/vllm_lifecycle_lib.sh"

# Ten-model paper OSS set: five standalone open-weight coders plus the
# five-member Qwen3.5 scaling sweep. Edit to add gpt-oss-120b if needed.
MODELS=(
  gemma-4-31b-it
  glm-4.7-flash
  qwen3-coder-next
  devstral-2-123b
  minimax-m2.7
  qwen3.5-9b
  qwen3.5-27b
  qwen3.5-35b-a3b
  qwen3.5-122b-a10b
  qwen3.5-397b-a17b
)

CSV_PATH="$HARNESS_DIR/SAMPLE/autodep_100.csv"
PARALLEL=8
VLLM_BASE_PORT="${VLLM_BASE_PORT:-8000}"
VLLM_BIND_HOST="${VLLM_BIND_HOST:-0.0.0.0}"
VLLM_CLIENT_HOST="${VLLM_CLIENT_HOST:-127.0.0.1}"
VLLM_READY_TIMEOUT="${VLLM_READY_TIMEOUT:-1800}"
USAGE_PROXY_BASE_PORT="${USAGE_PROXY_BASE_PORT:-9000}"
USAGE_PROXY_HOST="${USAGE_PROXY_HOST:-127.0.0.1}"

usage() {
  cat <<'EOF'
Usage: harness/opensource_models/run_os_models_autodep.sh [model ...] [options] [-- evaluate.sh args...]

Default models (paper OSS set, 10 total):
  gemma-4-31b-it       glm-4.7-flash       qwen3-coder-next
  devstral-2-123b      minimax-m2.7
  qwen3.5-9b           qwen3.5-27b         qwen3.5-35b-a3b
  qwen3.5-122b-a10b    qwen3.5-397b-a17b

Options:
  --models A,B,C       Comma-separated models. Overrides positional models.
  --csv PATH           CSV to evaluate (default: harness/SAMPLE/autodep_100.csv)
  --parallel N         Passed to evaluate.sh (default: 8; lower than the
                       static-HTML run because autodep hosts run npm install
                       + npm run build before serving)
  --vllm-base-port N   First vLLM port (default: 8000)
  --proxy-base-port N  First usage proxy port (default: 9000)
  --resume-dir DIR     Resume into an existing output dir.
  --rebuild-csv        Regenerate harness/SAMPLE/autodep_100.csv before the run
                       (calls scripts/build_autodep_csv.py).
  --help, -h           Show this message

Environment:
  AUTODEP_ROOT         Override the location of the per-repo autodep host scripts
                       (default: project_root/autodep_final_100_host_scripts/).
  TENSOR_PARALLEL_SIZE, GPU_MEMORY_UTILIZATION, DTYPE, MAX_MODEL_LEN,
  MAX_NUM_BATCHED_TOKENS, VLLM_EXTRA_ARGS, TOOL_CALL_PARSER, CHAT_TEMPLATE
  are forwarded to the vLLM hosting scripts.

Output:
  harness/out/autodep_models/<timestamp>/<model>/
    vllm.log
    usage_proxy.log
    usage/{api_calls.jsonl, errors.jsonl, summary.json, by_*.csv}
    results/                 ← agent patches, plans, CWV measurements
EOF
}

cli_models=()
eval_args=()
models_from_flag=""
RESUME_DIR=""
REBUILD_CSV=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --models)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --models A,B,C"; exit 1; }
      models_from_flag="$1"; shift ;;
    --csv)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --csv PATH"; exit 1; }
      CSV_PATH="$1"; shift ;;
    --parallel)
      shift; [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N"; exit 1; }
      PARALLEL="$1"; _PARALLEL_OVERRIDE="$1"; shift ;;
    --vllm-base-port)
      shift; [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --vllm-base-port N"; exit 1; }
      VLLM_BASE_PORT="$1"; shift ;;
    --proxy-base-port)
      shift; [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --proxy-base-port N"; exit 1; }
      USAGE_PROXY_BASE_PORT="$1"; shift ;;
    --resume-dir)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --resume-dir DIR"; exit 1; }
      RESUME_DIR="$1"; shift ;;
    --rebuild-csv) REBUILD_CSV=1; shift ;;
    --help|-h) usage; exit 0 ;;
    --) shift; eval_args+=("$@"); break ;;
    --*) eval_args+=("$@"); break ;;
    *) cli_models+=("$1"); shift ;;
  esac
done

if [[ -n "$models_from_flag" ]]; then
  IFS=',' read -r -a MODELS <<< "$models_from_flag"
elif [[ ${#cli_models[@]} -gt 0 ]]; then
  MODELS=("${cli_models[@]}")
fi

if [[ "$REBUILD_CSV" -eq 1 ]]; then
  echo "[autodep] Rebuilding $CSV_PATH ..."
  python3 "$HARNESS_DIR/../scripts/build_autodep_csv.py" --out "$CSV_PATH"
fi

if [[ ! "$CSV_PATH" = /* ]]; then
  CSV_PATH="$(cd "$(dirname "$CSV_PATH")" && pwd)/$(basename "$CSV_PATH")"
fi
[[ -f "$CSV_PATH" ]] || { echo "Missing CSV: $CSV_PATH (try --rebuild-csv)"; exit 1; }
[[ -x "$HARNESS_DIR/evaluate.sh" || -f "$HARNESS_DIR/evaluate.sh" ]] || { echo "Missing evaluate.sh"; exit 1; }
[[ -f "$HARNESS_DIR/host_files/host_autodep.sh" ]] || { echo "Missing dispatcher: harness/host_files/host_autodep.sh"; exit 1; }

# Surface the autodep root early so failures are loud rather than per-row.
_AUTODEP_ROOT="${AUTODEP_ROOT:-$(cd "$HARNESS_DIR/.." && pwd)/autodep_final_100_host_scripts}"
[[ -d "$_AUTODEP_ROOT" ]] || { echo "Missing AUTODEP_ROOT: $_AUTODEP_ROOT"; exit 1; }
echo "[autodep] AUTODEP_ROOT=$_AUTODEP_ROOT  (host scripts: $(ls "$_AUTODEP_ROOT"/*__host.sh 2>/dev/null | wc -l))"
export AUTODEP_ROOT="$_AUTODEP_ROOT"

if [[ -n "$RESUME_DIR" ]]; then
  [[ "$RESUME_DIR" = /* ]] || RESUME_DIR="$(cd "$RESUME_DIR" && pwd)"
  [[ -d "$RESUME_DIR" ]] || { echo "Error: --resume-dir '$RESUME_DIR' does not exist"; exit 1; }
  LOG_DIR="$RESUME_DIR"
  echo "[autodep] Resuming into existing output dir: $LOG_DIR"
else
  RUN_TS="$(date +%Y%m%d_%H%M%S)"
  LOG_DIR="$HARNESS_DIR/out/autodep_models/$RUN_TS"
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

is_qwen35_family() {
  case "$1" in
    qwen3.5-9b|qwen3.5-27b|qwen3.5-35b-a3b|qwen3.5-122b-a10b|qwen3.5-397b-a17b) return 0 ;;
    *) return 1 ;;
  esac
}

# Maps Qwen3.5 served name -> HuggingFace model id. Mirrors run_scale_eval.sh.
hf_id_for_qwen35() {
  case "$1" in
    qwen3.5-9b)          echo "Qwen/Qwen3.5-9B" ;;
    qwen3.5-27b)         echo "Qwen/Qwen3.5-27B" ;;
    qwen3.5-35b-a3b)     echo "Qwen/Qwen3.5-35B-A3B" ;;
    qwen3.5-122b-a10b)   echo "Qwen/Qwen3.5-122B-A10B" ;;
    qwen3.5-397b-a17b)   echo "Qwen/Qwen3.5-397B-A17B-FP8" ;;
    *) return 1 ;;
  esac
}

# For the standalone OSS models we use the dedicated serve_*.sh scripts.
# The Qwen3.5 family uses the generic serve_model.sh + env vars (handled
# inline in the launch loop below), so script_for_model() returns "" for them.
script_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3) echo "$SCRIPT_DIR/serve_qwen3_coder_next.sh" ;;
    glm-4.7-flash|glm|glm47) echo "$SCRIPT_DIR/serve_glm_4_7_flash.sh" ;;
    gemma-4-31b-it|gemma|gemma4) echo "$SCRIPT_DIR/serve_gemma_4_31b_it.sh" ;;
    devstral-2-123b|devstral|devstral2) echo "$SCRIPT_DIR/serve_devstral_2_123b.sh" ;;
    minimax-m2.7|minimax|minimax-m2) echo "$SCRIPT_DIR/serve_minimax_m2.sh" ;;
    gpt-oss-120b|gptoss|gpt-oss) echo "$SCRIPT_DIR/serve_gpt_oss_120b.sh" ;;
    qwen3.5-9b|qwen3.5-27b|qwen3.5-35b-a3b|qwen3.5-122b-a10b|qwen3.5-397b-a17b)
      echo ""  # Qwen3.5 family is served inline via serve_model.sh + env vars
      ;;
    *) echo "Unknown model: $1" >&2; return 1 ;;
  esac
}

served_name_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3) echo "qwen3-coder-next" ;;
    glm-4.7-flash|glm|glm47) echo "glm-4.7-flash" ;;
    gemma-4-31b-it|gemma|gemma4) echo "gemma-4-31b-it" ;;
    devstral-2-123b|devstral|devstral2) echo "devstral-2-123b" ;;
    minimax-m2.7|minimax|minimax-m2) echo "minimax-m2.7" ;;
    gpt-oss-120b|gptoss|gpt-oss) echo "gpt-oss-120b" ;;
    qwen3.5-9b|qwen3.5-27b|qwen3.5-35b-a3b|qwen3.5-122b-a10b|qwen3.5-397b-a17b) echo "$1" ;;
    *) return 1 ;;
  esac
}

# All Qwen3.5 models share the 262K context; reserve ~40K for input/history.
max_tokens_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3) echo "220000" ;;
    glm-4.7-flash|glm|glm47)     echo "90000"  ;;
    gemma-4-31b-it|gemma|gemma4) echo "220000" ;;
    devstral-2-123b|devstral|devstral2) echo "90000" ;;
    minimax-m2.7|minimax|minimax-m2) echo "90000"  ;;
    gpt-oss-120b|gptoss|gpt-oss) echo "90000" ;;
    qwen3.5-9b|qwen3.5-27b|qwen3.5-35b-a3b|qwen3.5-122b-a10b|qwen3.5-397b-a17b) echo "220000" ;;
    *) echo "16000" ;;
  esac
}

# Per-model parallel defaults. The Qwen3.5 numbers match run_scale_eval.sh,
# tuned to step latency and KV-cache headroom. CLI --parallel still wins.
parallel_for_model() {
  [[ -n "${_PARALLEL_OVERRIDE:-}" ]] && { echo "$_PARALLEL_OVERRIDE"; return; }
  case "$1" in
    gemma-4-31b-it|gemma|gemma4)        echo "12"  ;;
    glm-4.7-flash|glm|glm47)            echo "12"  ;;
    qwen3-coder-next|qwen|qwen3)        echo "12"  ;;
    gpt-oss-120b|gptoss|gpt-oss)        echo "8"   ;;
    devstral-2-123b|devstral|devstral2) echo "8"   ;;
    minimax-m2.7|minimax|minimax-m2)    echo "8"   ;;
    qwen3.5-9b)                         echo "100" ;;
    qwen3.5-27b)                        echo "80"  ;;
    qwen3.5-35b-a3b)                    echo "80"  ;;
    qwen3.5-122b-a10b)                  echo "60"  ;;
    qwen3.5-397b-a17b)                  echo "40"  ;;
    *) echo "${PARALLEL:-8}" ;;
  esac
}

tp_for_model() {
  case "$1" in
    qwen3-coder-next|qwen|qwen3) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    glm-4.7-flash|glm|glm47)     echo "${TENSOR_PARALLEL_SIZE:-4}" ;;
    gemma-4-31b-it|gemma|gemma4) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    devstral-2-123b|devstral|devstral2) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    minimax-m2.7|minimax|minimax-m2) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    gpt-oss-120b|gptoss|gpt-oss) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
    qwen3.5-9b)                  echo "${TENSOR_PARALLEL_SIZE:-4}" ;;
    qwen3.5-27b|qwen3.5-35b-a3b|qwen3.5-122b-a10b|qwen3.5-397b-a17b)
                                 echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
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

  echo "[autodep] Starting vLLM for $served_name on port $port (TP=$(tp_for_model "$model"))"
  if is_qwen35_family "$model"; then
    # Qwen3.5 family: launch the generic serve_model.sh with full model-specific
    # env block (matches run_scale_eval.sh's start_vllm_for_model exactly).
    ( MODEL_ID="$(hf_id_for_qwen35 "$model")" \
      SERVED_MODEL_NAME="$served_name" \
      VLLM_HOST="$VLLM_BIND_HOST" \
      VLLM_PORT="$port" \
      TENSOR_PARALLEL_SIZE="$(tp_for_model "$model")" \
      TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_coder}" \
      REASONING_PARSER="${REASONING_PARSER:-qwen3}" \
      MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}" \
      MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-65536}" \
      setsid "$SCRIPT_DIR/serve_model.sh" >"$log_file" 2>&1 ) &
  else
    ( VLLM_HOST="$VLLM_BIND_HOST" \
      VLLM_PORT="$port" \
      SERVED_MODEL_NAME="$served_name" \
      TENSOR_PARALLEL_SIZE="$(tp_for_model "$model")" \
      setsid "$serve_script" >"$log_file" 2>&1 ) &
  fi
  vllm_pid=$!

  if ! wait_for_vllm "$port" "$VLLM_READY_TIMEOUT"; then
    echo "[autodep] ERROR: vLLM did not become ready for $served_name. Log: $log_file"
    exit 1
  fi

  echo "[autodep] Starting usage proxy for $served_name on port $proxy_port"
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
    echo "[autodep] ERROR: usage proxy did not become ready for $served_name. Log: $proxy_log_file"
    exit 1
  fi

  model_parallel="$(parallel_for_model "$model")"
  echo "[autodep] Running evaluate.sh for $served_name with parallel=$model_parallel (CSV=$CSV_PATH)"
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
  AUTODEP_ROOT="$AUTODEP_ROOT" \
    bash "$HARNESS_DIR/evaluate.sh" --parallel "$model_parallel" --skip-all "${eval_args[@]}"
  eval_status=$?
  set -e

  python3 "$SCRIPT_DIR/aggregate_usage.py" \
    --input "$usage_dir/api_calls.jsonl" \
    --output-dir "$usage_dir" \
    || echo "[autodep] WARN: usage aggregation failed for $served_name"

  echo "[autodep] Stopping usage proxy for $served_name"
  kill "$proxy_pid" 2>/dev/null || true
  wait "$proxy_pid" 2>/dev/null || true
  proxy_pid=""
  echo "[autodep] Stopping vLLM for $served_name"
  _kill_vllm "$vllm_pid"
  vllm_pid=""

  if [[ "$eval_status" -ne 0 ]]; then
    echo "[autodep] ERROR: evaluate.sh failed for $served_name with status $eval_status"
    exit "$eval_status"
  fi
done

echo "[autodep] All requested model evaluations complete. Output: $LOG_DIR"
