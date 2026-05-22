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

SKIP_ROWWISE="${SKIP_ROWWISE:-0}"
ROWWISE_PARALLEL="${ROWWISE_PARALLEL:-4}"
ROWWISE_BASE_PORT="${ROWWISE_BASE_PORT:-14000}"
ROWWISE_NUM_RUNS="${ROWWISE_NUM_RUNS:-5}"

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
  --skip-rowwise       Skip the row-wise CWV + visual measurement phase
  --rowwise-parallel N Parallel jobs for row-wise measurement (default: 4)
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
    --skip-rowwise) SKIP_ROWWISE=1; shift ;;
    --rowwise-parallel)
      shift
      [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --rowwise-parallel N"; exit 1; }
      ROWWISE_PARALLEL="$1"; shift ;;
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
  fuser -k "${port}/tcp" 2>/dev/null || true
  sleep 2
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

# ── Row-wise CWV + visual measurement ────────────────────────────────────────
# Clone each repo once, then apply every model's patch and measure sequentially.
# Reduces GitHub clones from (N_models × N_jobs) → N_jobs.

if [[ "$SKIP_ROWWISE" != "1" ]]; then
  echo ""
  echo "[scale-eval] ── Row-wise CWV + visual measurement (parallel=$ROWWISE_PARALLEL) ──"

  _VISUAL_SCRIPT="$HARNESS_DIR/visual_validate.py"
  _CWV_SCRIPT="$HARNESS_DIR/../scripts/helper_scripts/cwv_benchmark.py"
  _ROWWISE_TMP="${HARNESS_TMPDIR:-/tmp}/rowwise_$$"
  mkdir -p "$_ROWWISE_TMP"

  _rw_wait_for_server() {
    local port="$1" timeout="${2:-90}" i
    for i in $(seq 1 "$timeout"); do
      curl -fs "http://localhost:${port}/" >/dev/null 2>&1 && return 0
      sleep 1
    done
    return 1
  }

  _rw_measure_job() {
    local ID="$1" REPO_ID="$2" FRAMEWORK="$3" COMMIT_ID_RAW="$4"
    local HOST_FILE_PATH="$5" SLOT="$6"
    local PORT=$(( ROWWISE_BASE_PORT + SLOT ))
    local JOB_TMP="$_ROWWISE_TMP/$ID"
    local BASELINE_DIR="$JOB_TMP/baseline"
    mkdir -p "$JOB_TMP"

    # 1) Clone baseline once
    local CLONE_TMP
    CLONE_TMP="$(mktemp -d -p "${HARNESS_TMPDIR:-/tmp}")"
    echo "[rowwise] Cloning $REPO_ID (ID=$ID) ..."
    if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -c credential.helper='' -c http.extraHeader='' \
         clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
      echo "[rowwise] Retry clone (ID=$ID) ..."
      sleep 10
      rm -rf "$CLONE_TMP"; CLONE_TMP="$(mktemp -d -p "${HARNESS_TMPDIR:-/tmp}")"
      if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
           git -c credential.helper='' -c http.extraHeader='' \
           clone "https://github.com/${REPO_ID}.git" "$CLONE_TMP" >/dev/null 2>&1; then
        echo "[rowwise] ERROR: clone failed (ID=$ID)"
        rm -rf "$JOB_TMP" "$CLONE_TMP"
        return 1
      fi
    fi

    # 2) Checkout pinned commit
    local COMMIT_CLEAN="$COMMIT_ID_RAW"
    [[ "$COMMIT_CLEAN" == " " || "$COMMIT_CLEAN" == "null" ]] && COMMIT_CLEAN=""
    if [[ -n "$COMMIT_CLEAN" ]]; then
      git -C "$CLONE_TMP" checkout "$COMMIT_CLEAN" >/dev/null 2>&1 || {
        echo "[rowwise] ERROR: checkout $COMMIT_CLEAN failed (ID=$ID)"
        rm -rf "$JOB_TMP" "$CLONE_TMP"
        return 1
      }
    fi
    git -C "$CLONE_TMP" add -A >/dev/null 2>&1 || true
    git -C "$CLONE_TMP" commit -qm "baseline" >/dev/null 2>&1 || true
    mv "$CLONE_TMP" "$BASELINE_DIR"

    local FW
    FW="$(echo "${FRAMEWORK:-static html}" | tr '[:upper:]' '[:lower:]')"

    # 3) For each model: apply patch, serve, measure
    for model in "${MODELS[@]}"; do
      local AGENT_NAME="template_opencode_os"
      local JOB_LABEL="${ID}_${AGENT_NAME}"
      local OUT_DIR="$LOG_DIR/$model/results/$JOB_LABEL"
      local PATCH_FILE="$OUT_DIR/${JOB_LABEL}.patch"

      if [[ ! -d "$OUT_DIR" ]]; then
        echo "[rowwise] SKIP: no results for $model/$ID"
        continue
      fi

      local WORK_DIR="$JOB_TMP/$model"
      cp -r "$BASELINE_DIR" "$WORK_DIR"

      if [[ -f "$PATCH_FILE" && -s "$PATCH_FILE" ]]; then
        git -C "$WORK_DIR" apply "$PATCH_FILE" >/dev/null 2>&1 \
          || echo "[rowwise] WARN: patch failed ($model/$ID)"
      else
        echo "[rowwise] WARN: empty/missing patch for $model/$ID — measuring baseline"
        touch "$OUT_DIR/empty.patch"
        PATCH_FILE="$OUT_DIR/empty.patch"
      fi

      fuser -k "${PORT}/tcp" 2>/dev/null || true
      sleep 1
      PORT="$PORT" bash "$HARNESS_DIR/$HOST_FILE_PATH" "$WORK_DIR" "$OUT_DIR/host.log" &
      local HOST_PID=$!

      if ! _rw_wait_for_server "$PORT" 90; then
        echo "[rowwise] ERROR: server not ready ($model/$ID)"
        kill "$HOST_PID" 2>/dev/null || true
        rm -rf "$WORK_DIR"
        continue
      fi

      python3 "$_VISUAL_SCRIPT" \
        --url             "http://localhost:$PORT" \
        --screenshot-path "$OUT_DIR/screenshot.png" \
        --repo-id         "$REPO_ID" \
        --commit-id       "${COMMIT_CLEAN:-}" \
        --framework       "${FW}" \
        --patch-file      "$PATCH_FILE" \
        --output-json     "$OUT_DIR/visual.json" \
        2>"$OUT_DIR/visual.stderr" \
        || echo "[rowwise] WARN: visual failed ($model/$ID)"

      local REGRESSED=0
      if [[ -f "$OUT_DIR/visual.json" ]]; then
        REGRESSED=$(python3 -c "
import json
d = json.load(open('$OUT_DIR/visual.json'))
print('1' if d.get('overall_regression') is True else '0')
" 2>/dev/null || echo "0")
      fi

      if [[ "$REGRESSED" == "1" ]]; then
        echo "[rowwise] Regression — skipping CWV ($model/$ID)"
      else
        python3 "$_CWV_SCRIPT" --device mobile  --num-runs "$ROWWISE_NUM_RUNS" \
          --url "http://localhost:$PORT" \
          > "$OUT_DIR/mobile.json"  2>>"$OUT_DIR/cwv_stderr.txt" || true
        python3 "$_CWV_SCRIPT" --device desktop --num-runs "$ROWWISE_NUM_RUNS" \
          --url "http://localhost:$PORT" \
          > "$OUT_DIR/desktop.json" 2>>"$OUT_DIR/cwv_stderr.txt" || true
      fi

      kill "$HOST_PID" 2>/dev/null || true
      wait "$HOST_PID" 2>/dev/null || true
      rm -rf "$WORK_DIR"
      echo "[rowwise] ✓ $model/$ID"
    done

    rm -rf "$JOB_TMP"
    echo "[rowwise] Done: ID=$ID (${#MODELS[@]} models)"
  }

  # Parallel slot pool
  declare -A _RW_SLOT=()
  _RW_SLOT_N=0

  _rw_acquire_slot() {
    while true; do
      local pid s used p count
      count=${#_RW_SLOT[@]}
      for pid in "${!_RW_SLOT[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
          _RW_SLOT_N="${_RW_SLOT[$pid]}"
          unset "_RW_SLOT[$pid]"
          return 0
        fi
      done
      if [[ $count -lt $ROWWISE_PARALLEL ]]; then
        for s in $(seq 0 $((ROWWISE_PARALLEL - 1))); do
          used=0
          for p in "${!_RW_SLOT[@]}"; do
            [[ "${_RW_SLOT[$p]}" == "$s" ]] && used=1 && break
          done
          [[ $used -eq 0 ]] && { _RW_SLOT_N="$s"; return 0; }
        done
      fi
      sleep 0.5
    done
  }

  while IFS=$'\t' read -r ID REPO_ID FRAMEWORK COMMIT_ID HOST_FILE_PATH; do
    _rw_acquire_slot
    slot=$_RW_SLOT_N
    ( _rw_measure_job "$ID" "$REPO_ID" "$FRAMEWORK" "$COMMIT_ID" "$HOST_FILE_PATH" "$slot" ) &
    _RW_SLOT[$!]=$slot
  done < <(python3 - "$CSV_PATH" <<'PY'
import csv, sys
csv.field_size_limit(10**7)
want = ["ID", "REPO_ID", "FRAMEWORK", "COMMIT_ID", "HOST_FILE_PATH"]
with open(sys.argv[1], newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        vals = [(row.get(c) or " ").replace("\t", " ").replace("\n", " ") for c in want]
        print("\t".join(vals))
PY
  )

  wait
  rm -rf "$_ROWWISE_TMP"
  echo ""
  echo "[scale-eval] Row-wise measurement complete."
fi

echo "[scale-eval] All scale-eval runs complete. Results: $LOG_DIR"
