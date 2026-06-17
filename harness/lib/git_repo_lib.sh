#!/usr/bin/env bash
# Shared Git clone/checkout helpers for benchmark harness scripts.

bench_git_clean_commit() {
  local commit="${1:-}"
  [[ "$commit" == " " || "$commit" == "null" ]] && commit=""
  printf '%s' "$commit"
}

bench_git_clone_checkout() {
  local REPO_ID="$1"
  local COMMIT_ID="$2"
  local DEST_DIR="$3"
  local LOG_PREFIX="${4:-[git]}"
  local JOB_ID="${5:-unknown}"

  local COMMIT_CLEAN
  COMMIT_CLEAN="$(bench_git_clean_commit "$COMMIT_ID")"

  BENCH_GIT_REQUESTED_COMMIT="$COMMIT_CLEAN"
  BENCH_GIT_ACTUAL_COMMIT="unknown"
  BENCH_GIT_COMMIT_FALLBACK="false"
  BENCH_GIT_CHECKOUT_METHOD="direct_fetch"
  BENCH_GIT_CHECKOUT_ERROR=""

  rm -rf "$DEST_DIR"
  mkdir -p "$DEST_DIR"

  if [[ -n "$COMMIT_CLEAN" ]]; then
    echo "$LOG_PREFIX Fetching pinned commit $COMMIT_CLEAN for $REPO_ID ..."
    git -C "$DEST_DIR" init -q
    git -C "$DEST_DIR" remote add origin "https://github.com/${REPO_ID}.git"

    local fetch_err attempt
    fetch_err="$(mktemp -p "${TMPDIR:-/tmp}")"
    for attempt in 1 2 3; do
      : >"$fetch_err"
      if GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -C "$DEST_DIR" \
           -c credential.helper='' -c http.extraHeader='' -c http.version=HTTP/1.1 \
           fetch --depth 1 --no-tags origin "$COMMIT_CLEAN" >/dev/null 2>"$fetch_err" \
         && git -C "$DEST_DIR" checkout --detach FETCH_HEAD >/dev/null 2>>"$fetch_err"; then
        BENCH_GIT_CHECKOUT_METHOD="direct_fetch"
        BENCH_GIT_ACTUAL_COMMIT="$(git -C "$DEST_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
        rm -f "$fetch_err"
        return 0
      fi
      [[ "$attempt" -lt 3 ]] && sleep 5
    done

    BENCH_GIT_CHECKOUT_ERROR="$(tr '\n' ' ' < "$fetch_err" | tr -s '[:space:]' ' ' | cut -c1-500)"
    rm -f "$fetch_err"
    echo "$LOG_PREFIX ERROR: direct fetch failed for pinned commit $COMMIT_CLEAN (ID=$JOB_ID)"
    [[ -n "$BENCH_GIT_CHECKOUT_ERROR" ]] && echo "$LOG_PREFIX ERROR: git said: $BENCH_GIT_CHECKOUT_ERROR"
    rm -rf "$DEST_DIR"
    return 1
  fi

  echo "$LOG_PREFIX Cloning $REPO_ID ..."
  if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
       git -c credential.helper='' -c http.extraHeader='' \
       -c http.version=HTTP/1.1 \
       clone "https://github.com/${REPO_ID}.git" "$DEST_DIR" >/dev/null 2>&1; then
    echo "$LOG_PREFIX Retry clone in 10s (ID=$JOB_ID) ..."
    sleep 10
    rm -rf "$DEST_DIR"
    if ! GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
         git -c credential.helper='' -c http.extraHeader='' \
         -c http.version=HTTP/1.1 \
         clone "https://github.com/${REPO_ID}.git" "$DEST_DIR" >/dev/null 2>&1; then
      echo "$LOG_PREFIX ERROR: clone failed after retry (ID=$JOB_ID)"
      return 1
    fi
  fi

  BENCH_GIT_ACTUAL_COMMIT="$(git -C "$DEST_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [[ -n "$COMMIT_CLEAN" ]]; then
    BENCH_GIT_COMMIT_FALLBACK="true"
    BENCH_GIT_CHECKOUT_METHOD="head_fallback"
  else
    BENCH_GIT_CHECKOUT_METHOD="default_branch"
  fi
  return 0
}

bench_git_write_meta() {
  local OUT_FILE="$1"
  python3 - "$OUT_FILE" \
    "$BENCH_GIT_REQUESTED_COMMIT" \
    "$BENCH_GIT_ACTUAL_COMMIT" \
    "$BENCH_GIT_COMMIT_FALLBACK" \
    "$BENCH_GIT_CHECKOUT_METHOD" \
    "$BENCH_GIT_CHECKOUT_ERROR" <<'PY'
import json
import sys

out, requested, actual, fallback, method, error = sys.argv[1:]
with open(out, "w", encoding="utf-8") as f:
    json.dump(
        {
            "requested_commit": requested,
            "actual_commit": actual,
            "commit_fallback": fallback == "true",
            "checkout_method": method,
            "checkout_error": error,
        },
        f,
    )
    f.write("\n")
PY
}

bench_git_apply_patch() {
  local WORK_DIR="$1"
  local PATCH_FILE="$2"
  local OUT_DIR="$3"
  local LOG_PREFIX="${4:-[git]}"
  local status="applied"
  local method="apply"
  local error=""

  mkdir -p "$OUT_DIR"

  if [[ ! -f "$PATCH_FILE" || ! -s "$PATCH_FILE" ]]; then
    status="empty_patch"
    method="none"
    python3 - "$OUT_DIR/patch_apply.json" "$PATCH_FILE" "$status" "$method" "$error" <<'PY'
import json, sys
out, patch, status, method, error = sys.argv[1:]
json.dump({"patch_file": patch, "status": status, "method": method, "error": error}, open(out, "w"))
open(out, "a").write("\n")
PY
    return 0
  fi

  local err_file
  err_file="$(mktemp -p "${TMPDIR:-/tmp}")"
  if git -C "$WORK_DIR" apply --whitespace=nowarn "$PATCH_FILE" >/dev/null 2>"$err_file"; then
    status="applied"
    method="apply"
  elif git -C "$WORK_DIR" apply --3way --whitespace=nowarn "$PATCH_FILE" >/dev/null 2>"$err_file"; then
    status="applied"
    method="apply_3way"
  else
    status="failed"
    method="apply_3way"
    error="$(tr '\n' ' ' < "$err_file" | tr -s '[:space:]' ' ' | cut -c1-1000)"
  fi
  rm -f "$err_file"

  python3 - "$OUT_DIR/patch_apply.json" "$PATCH_FILE" "$status" "$method" "$error" <<'PY'
import json, sys
out, patch, status, method, error = sys.argv[1:]
json.dump({"patch_file": patch, "status": status, "method": method, "error": error}, open(out, "w"))
open(out, "a").write("\n")
PY

  if [[ "$status" == "failed" ]]; then
    echo "$LOG_PREFIX WARN: patch apply failed: $error"
    return 1
  fi
  return 0
}
