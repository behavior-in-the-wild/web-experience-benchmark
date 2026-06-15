#!/usr/bin/env bash
# =============================================================
#  Run 3 OSS models on the live-bench suggestions eval.
#  Adapted from harness/opensource_models/run_os_models_suggestions.sh.
#
#  Models:
#    gemma-4-31b-it   (via serve_gemma_4_31b_it.sh)
#    minimax-m2.7     (via serve_minimax_m2.sh)
#    qwen3.5-27b      (via serve_model.sh + Qwen3.5 env block)
#
#  For each model:
#    1. Start vLLM + usage proxy
#    2. Run run_cwv_evals_suggestions_row.sh (agent + patch, no measurement)
#    3. Aggregate usage
#    4. Stop vLLM + proxy → next model
#
#  Input: harness_live_bench/SAMPLE/live_filtered_top3.jsonl
#         (contains embedded suggestions + URLs; mirrors must exist in MIRRORS_ROOT)
#
#  Usage:
#    bash harness_live_bench/run_os_models_suggestions.sh
#    bash harness_live_bench/run_os_models_suggestions.sh --parallel 20
#    bash harness_live_bench/run_os_models_suggestions.sh --models gemma-4-31b-it,qwen3.5-27b
#    bash harness_live_bench/run_os_models_suggestions.sh --resume-dir harness_live_bench/out/suggestions_eval/20260601_120000
# =============================================================
set -euo pipefail
export TMPDIR="${TMPDIR:-/dev/shm/ayush/web-experience-benchmark/.tmp}"
mkdir -p "$TMPDIR"

HARNESS_LIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OSS_DIR="$(cd "$HARNESS_LIVE/../harness/opensource_models" && pwd)"
HARNESS="$(cd "$HARNESS_LIVE/../harness" && pwd)"

MODELS=(
  gemma-4-31b-it
  minimax-m2.7
  qwen3.5-27b
)

JSONL_PATH="$HARNESS_LIVE/SAMPLE/live_filtered_top3.jsonl"
MIRRORS_ROOT="${MIRRORS_ROOT:-$HARNESS_LIVE/../live_assets_eds}"
PARALLEL=20
SKIP_MEASURE=1
LIMIT=""
RESUME_DIR=""
VLLM_BASE_PORT="${VLLM_BASE_PORT:-8000}"
VLLM_BIND_HOST="${VLLM_BIND_HOST:-0.0.0.0}"
VLLM_CLIENT_HOST="${VLLM_CLIENT_HOST:-127.0.0.1}"
VLLM_READY_TIMEOUT="${VLLM_READY_TIMEOUT:-1800}"
USAGE_PROXY_BASE_PORT="${USAGE_PROXY_BASE_PORT:-9000}"
USAGE_PROXY_HOST="${USAGE_PROXY_HOST:-127.0.0.1}"

usage() {
  cat <<'EOF'
Usage: harness_live_bench/run_os_models_suggestions.sh [options]

Models (default, all 3):
  gemma-4-31b-it   minimax-m2.7   qwen3.5-27b

Options:
  --models A,B,C       Comma-separated subset of the 3 models
  --jsonl PATH         Input JSONL (default: SAMPLE/live_filtered_top3.jsonl)
  --mirrors-root DIR   Mirror root dir (default: ../live_assets_eds)
  --parallel N         Parallel jobs per model (default: 20)
  --limit N            Process only first N rows (for testing)
  --no-skip-measure    Also run visual + CWV measurement after patching
  --resume-dir DIR     Resume into existing output root dir
  --vllm-base-port N   First vLLM port (default: 8000)
  --proxy-base-port N  First usage proxy port (default: 9000)
  --help, -h           Show this message

Output:
  harness_live_bench/out/suggestions_eval/<timestamp>/<model>/
    vllm.log
    usage_proxy.log
    usage/{api_calls.jsonl, summary.json, …}
    results/{row_id}_s{0,1,2}_template_opencode_os_direct/
EOF
}

models_from_flag=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --models)          shift; models_from_flag="$1"; shift ;;
    --jsonl)           shift; JSONL_PATH="$1"; shift ;;
    --mirrors-root)    shift; MIRRORS_ROOT="$1"; shift ;;
    --parallel)        shift; PARALLEL="$1"; shift ;;
    --limit)           shift; LIMIT="$1"; shift ;;
    --no-skip-measure) SKIP_MEASURE=0; shift ;;
    --vllm-base-port)  shift; VLLM_BASE_PORT="$1"; shift ;;
    --proxy-base-port) shift; USAGE_PROXY_BASE_PORT="$1"; shift ;;
    --resume-dir)      shift; RESUME_DIR="$1"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

if [[ -n "$models_from_flag" ]]; then
  IFS=',' read -r -a MODELS <<< "$models_from_flag"
fi

[[ -f "$JSONL_PATH" ]] || { echo "Missing JSONL: $JSONL_PATH"; exit 1; }
[[ -f "$HARNESS_LIVE/run_cwv_evals_suggestions_row.sh" ]] \
  || { echo "Missing: harness_live_bench/run_cwv_evals_suggestions_row.sh"; exit 1; }
[[ "$MIRRORS_ROOT" = /* ]] || MIRRORS_ROOT="$(cd "$MIRRORS_ROOT" && pwd)"
[[ -d "$MIRRORS_ROOT" ]] || { echo "Missing MIRRORS_ROOT: $MIRRORS_ROOT"; exit 1; }

# ── Output root ───────────────────────────────────────────────────────────────
if [[ -n "$RESUME_DIR" ]]; then
  [[ "$RESUME_DIR" = /* ]] || RESUME_DIR="$(cd "$RESUME_DIR" && pwd)"
  [[ -d "$RESUME_DIR" ]] || { echo "Error: --resume-dir '$RESUME_DIR' not found"; exit 1; }
  LOG_DIR="$RESUME_DIR"
  echo "[live-oss] Resuming into: $LOG_DIR"
else
  RUN_TS="$(date +%Y%m%d_%H%M%S)"
  LOG_DIR="$HARNESS_LIVE/out/suggestions_eval/$RUN_TS"
  mkdir -p "$LOG_DIR"
fi

# ── Model helpers (reuse from OSS_DIR) ───────────────────────────────────────
served_name_for() {
  case "$1" in
    gemma-4-31b-it|gemma|gemma4)         echo "gemma-4-31b-it" ;;
    minimax-m2.7|minimax|minimax-m2)     echo "minimax-m2.7" ;;
    qwen3.5-27b)                         echo "qwen3.5-27b" ;;
    *) echo "$1" ;;
  esac
}

serve_script_for() {
  case "$1" in
    gemma-4-31b-it|gemma|gemma4)     echo "$OSS_DIR/serve_gemma_4_31b_it.sh" ;;
    minimax-m2.7|minimax|minimax-m2) echo "$OSS_DIR/serve_minimax_m2.sh" ;;
    qwen3.5-27b)                     echo "__qwen35__" ;;
    *) echo ""; return 1 ;;
  esac
}

tp_for() {
  case "$1" in
    *) echo "${TENSOR_PARALLEL_SIZE:-8}" ;;
  esac
}

max_tokens_for() {
  case "$1" in
    gemma-4-31b-it|gemma|gemma4)     echo "220000" ;;
    minimax-m2.7|minimax|minimax-m2) echo "90000" ;;
    qwen3.5-27b)                     echo "220000" ;;
    *) echo "16000" ;;
  esac
}

# ── Process lifecycle ─────────────────────────────────────────────────────────
vllm_pid=""
proxy_pid=""

_kill_vllm() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  local pgid
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" || pgid=""
  kill "$pid" 2>/dev/null || true
  [[ -n "$pgid" && "$pgid" != "0" && "$pgid" != "1" ]] && \
    kill -- "-$pgid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  _drain_gpus
}

_drain_gpus() {
  sleep 5
  local gpu_pids
  gpu_pids="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null \
    | tr -d ' ' | grep -v '^$' | sort -u)" || gpu_pids=""
  if [[ -n "$gpu_pids" ]]; then
    echo "[live-oss] Killing orphaned GPU processes: $(echo "$gpu_pids" | tr '\n' ' ')"
    echo "$gpu_pids" | xargs kill -9 2>/dev/null || true
    sleep 10
  fi
  local i used_mb
  for i in $(seq 1 30); do
    used_mb="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
      | awk 'BEGIN{s=0}{s+=$1}END{print s}')" || used_mb="999999"
    if [[ "$used_mb" -lt 2000 ]]; then
      echo "[live-oss] GPUs clear: ${used_mb} MiB total used"
      return 0
    fi
    sleep 2
  done
  echo "[live-oss] WARN: GPUs not fully clear after drain (${used_mb} MiB) — proceeding anyway"
}

cleanup() {
  [[ -n "$proxy_pid" ]] && { kill "$proxy_pid" 2>/dev/null || true; wait "$proxy_pid" 2>/dev/null || true; }
  [[ -n "$vllm_pid"  ]] && _kill_vllm "$vllm_pid"
}
trap cleanup EXIT INT TERM

wait_for_vllm() {
  local port="$1" deadline="$2"
  local url="http://${VLLM_CLIENT_HOST}:${port}/v1/models"
  for i in $(seq 1 "$deadline"); do
    curl -fs -H "Authorization: Bearer ${VLLM_API_KEY:-EMPTY}" "$url" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

wait_for_proxy() {
  local port="$1" deadline="${2:-30}"
  for i in $(seq 1 "$deadline"); do
    curl -fs "http://${USAGE_PROXY_HOST}:${port}/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# ── Main loop ─────────────────────────────────────────────────────────────────
echo "[live-oss] Models:       ${MODELS[*]}"
echo "[live-oss] JSONL:        $JSONL_PATH"
echo "[live-oss] MIRRORS_ROOT: $MIRRORS_ROOT"
echo "[live-oss] Output:       $LOG_DIR"
echo "[live-oss] Parallel:     $PARALLEL"
[[ -n "$LIMIT" ]]          && echo "[live-oss] LIMIT=$LIMIT"
[[ "$SKIP_MEASURE" == "1" ]] && echo "[live-oss] --skip-measure: patch-only (no server/visual/CWV)"

for idx in "${!MODELS[@]}"; do
  model="${MODELS[$idx]}"
  model="${model#"${model%%[![:space:]]*}"}"
  model="${model%"${model##*[![:space:]]}"}"
  [[ -n "$model" ]] || continue

  served="$(served_name_for "$model")"
  serve_script="$(serve_script_for "$model")"
  port=$(( VLLM_BASE_PORT + idx ))
  proxy_port=$(( USAGE_PROXY_BASE_PORT + idx ))

  model_dir="$LOG_DIR/$served"
  usage_dir="$model_dir/usage"
  mkdir -p "$usage_dir"

  echo ""
  echo "========================================================"
  echo "[live-oss] Model: $served  vLLM port: $port  TP: $(tp_for "$model")"
  echo "========================================================"

  # Start vLLM
  if [[ "$serve_script" == "__qwen35__" ]]; then
    ( MODEL_ID="Qwen/Qwen3.5-27B" \
      SERVED_MODEL_NAME="$served" \
      VLLM_HOST="$VLLM_BIND_HOST" \
      VLLM_PORT="$port" \
      TENSOR_PARALLEL_SIZE="$(tp_for "$model")" \
      TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_coder}" \
      REASONING_PARSER="${REASONING_PARSER:-qwen3}" \
      MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}" \
      MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-131072}" \
      MAX_NUM_SEQS="${MAX_NUM_SEQS:-128}" \
      setsid "$OSS_DIR/serve_model.sh" \
      >"$model_dir/vllm.log" 2>&1 ) &
  else
    ( VLLM_HOST="$VLLM_BIND_HOST" \
      VLLM_PORT="$port" \
      SERVED_MODEL_NAME="$served" \
      TENSOR_PARALLEL_SIZE="$(tp_for "$model")" \
      setsid "$serve_script" \
      >"$model_dir/vllm.log" 2>&1 ) &
  fi
  vllm_pid=$!

  echo "[live-oss] Waiting for vLLM ($served) …"
  if ! wait_for_vllm "$port" "$VLLM_READY_TIMEOUT"; then
    echo "[live-oss] ERROR: vLLM never ready for $served. Log: $model_dir/vllm.log"
    exit 1
  fi
  echo "[live-oss] vLLM ready."

  # Start usage proxy
  fuser -k "${proxy_port}/tcp" 2>/dev/null || true
  sleep 1
  python3 "$OSS_DIR/usage_proxy.py" \
    --listen-host "$USAGE_PROXY_HOST" \
    --listen-port "$proxy_port" \
    --upstream-base "http://${VLLM_CLIENT_HOST}:${port}" \
    --output-dir "$usage_dir" \
    --model-label "$served" \
    --quiet \
    >"$model_dir/usage_proxy.log" 2>&1 &
  proxy_pid=$!

  if ! wait_for_proxy "$proxy_port" 30; then
    echo "[live-oss] ERROR: usage proxy never ready for $served"
    exit 1
  fi
  echo "[live-oss] Usage proxy ready on port $proxy_port."

  # Build run_cwv_evals_suggestions_row.sh args
  run_args=("--parallel" "$PARALLEL")
  [[ "$SKIP_MEASURE" == "1" ]] && run_args+=("--skip-measure")
  [[ -n "$LIMIT" ]]            && run_args+=("--limit" "$LIMIT")
  [[ -n "$RESUME_DIR" ]]       && run_args+=("--resume")

  run_status=0
  JSONL="$JSONL_PATH" \
  MIRRORS_ROOT="$MIRRORS_ROOT" \
  EVAL_OUT_DIR="$model_dir" \
  OPENCODE_OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_BASE_URL="http://${USAGE_PROXY_HOST}:${proxy_port}/v1" \
  OPENAI_API_KEY="${VLLM_API_KEY:-EMPTY}" \
  OPENCODE_USAGE_PROXY=1 \
  VLLM_SERVED_MODEL_NAME="$served" \
  OPENCODE_MODEL="vllm/$served" \
  OPENCODE_MAX_TOKENS="$(max_tokens_for "$model")" \
    bash "$HARNESS_LIVE/run_cwv_evals_suggestions_row.sh" "${run_args[@]}" || run_status=$?

  # Aggregate usage stats
  python3 "$OSS_DIR/aggregate_usage.py" \
    --input "$usage_dir/api_calls.jsonl" \
    --output-dir "$usage_dir" \
    || echo "[live-oss] WARN: usage aggregation failed for $served"

  # Tear down
  echo "[live-oss] Stopping usage proxy for $served"
  kill "$proxy_pid" 2>/dev/null || true; wait "$proxy_pid" 2>/dev/null || true
  proxy_pid=""
  echo "[live-oss] Stopping vLLM for $served"
  _kill_vllm "$vllm_pid"
  vllm_pid=""

  if [[ "$run_status" -ne 0 ]]; then
    echo "[live-oss] ERROR: run script failed for $served (status $run_status)"
    exit "$run_status"
  fi

  echo "[live-oss] ✓ Done: $served"
done

echo ""
echo "[live-oss] All models complete. Output: $LOG_DIR"
