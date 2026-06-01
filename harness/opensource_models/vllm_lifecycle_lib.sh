#!/usr/bin/env bash
# vllm_lifecycle_lib.sh — shared vLLM/proxy lifecycle helpers for OS model wrapper scripts.
#
# Source this file after SCRIPT_DIR/HARNESS_DIR are set and after the per-script
# variable declarations (VLLM_CLIENT_HOST, USAGE_PROXY_HOST, etc.):
#
#   source "$(dirname "${BASH_SOURCE[0]}")/vllm_lifecycle_lib.sh"
#
# Callers must have these globals set before calling the functions:
#   VLLM_CLIENT_HOST   — hostname used to probe the vLLM health endpoint
#   USAGE_PROXY_HOST   — hostname used to probe the proxy health endpoint
#
# The cleanup() function and EXIT/INT/TERM trap must remain in each script
# because they reference script-local vllm_pid / proxy_pid variables.

# ---------------------------------------------------------------------------
# _kill_vllm PID
# Kills vLLM and its entire GPU worker subprocess tree, then waits for GPU
# memory to be released before the next model loads.
# ---------------------------------------------------------------------------
_kill_vllm() {
  local pid="$1"
  [[ -z "$pid" ]] && return 0
  local pgid
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" || pgid=""
  kill "$pid" 2>/dev/null || true
  # Kill the entire process group so TP workers also receive SIGTERM
  [[ -n "$pgid" && "$pgid" != "0" && "$pgid" != "1" ]] && \
    kill -- "-$pgid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  # Allow CUDA contexts to be torn down (process group kill above handles workers;
  # this sleep gives the kernel time to reclaim GPU memory before the next model
  # or the nvidia-smi check below).  Do NOT pkill by name — that would kill sibling
  # vLLM instances in a parallel multi-model run.
  sleep 8
  # Log how much GPU memory remains so we can diagnose any future OOM
  local used_mb
  used_mb="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null \
    | awk 'BEGIN{s=0}{s+=$1}END{print s}')" || used_mb="?"
  echo "[vllm-lifecycle] GPU memory after vLLM stop: ${used_mb} MiB used"
}

# ---------------------------------------------------------------------------
# wait_for_vllm PORT DEADLINE
# Polls http://VLLM_CLIENT_HOST:PORT/v1/models until it responds or DEADLINE
# seconds elapse. Returns 0 on success, 1 on timeout.
# ---------------------------------------------------------------------------
wait_for_vllm() {
  local port="$1"
  local deadline="$2"
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

# ---------------------------------------------------------------------------
# wait_for_proxy PORT [DEADLINE]
# Polls http://USAGE_PROXY_HOST:PORT/healthz until it responds or DEADLINE
# seconds elapse (default 30). Returns 0 on success, 1 on timeout.
# ---------------------------------------------------------------------------
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
