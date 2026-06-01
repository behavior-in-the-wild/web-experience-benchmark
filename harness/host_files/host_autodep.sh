#!/usr/bin/env bash
# =============================================================
#  Dispatcher: translates evaluate.sh's calling convention into the env-var
#  contract expected by the per-repo scripts in autodep_final_100_host_scripts/.
#
#  evaluate.sh calls:    bash $SCRIPT_DIR/$HOST_FILE_PATH <REPO_DIR> <LOG_FILE>
#                        with PORT and REPO_ID exported in the env.
#
#  autodep scripts want: REPO_DIR=<...> PORT=<...> bash <name>__host.sh
#
#  We resolve the right autodep script from REPO_ID by globbing for
#      *__<org>__<repo>__host.sh
#  in $AUTODEP_ROOT (default: project_root/autodep_final_100_host_scripts/).
# =============================================================
set -euo pipefail

REPO_DIR="${1:?usage: host_autodep.sh REPO_DIR LOG_FILE}"
LOG_FILE="${2:?usage: host_autodep.sh REPO_DIR LOG_FILE}"

# REPO_ID is exported by evaluate.sh (line 452). Format: "org/repo".
REPO_ID="${REPO_ID:?REPO_ID not set in environment; evaluate.sh should export this}"

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTODEP_ROOT="${AUTODEP_ROOT:-$(cd "$HARNESS_DIR/.." && pwd)/autodep_final_100_host_scripts}"

if [[ ! -d "$AUTODEP_ROOT" ]]; then
  echo "[autodep] ERROR: AUTODEP_ROOT does not exist: $AUTODEP_ROOT" | tee -a "$LOG_FILE"
  exit 2
fi

# Map "org/repo" to the on-disk filename "NNN__org__repo__host.sh".
# Repos with '/' in the path get '/' rewritten to '__' (matches generator).
NORMALIZED="${REPO_ID//\//__}"
shopt -s nullglob
matches=( "$AUTODEP_ROOT"/*"__${NORMALIZED}__host.sh" )
shopt -u nullglob

if [[ ${#matches[@]} -eq 0 ]]; then
  echo "[autodep] ERROR: no autodep script found for REPO_ID='$REPO_ID' (looked for '*__${NORMALIZED}__host.sh' in $AUTODEP_ROOT)" | tee -a "$LOG_FILE"
  exit 3
fi
if [[ ${#matches[@]} -gt 1 ]]; then
  echo "[autodep] WARN: multiple matches for REPO_ID='$REPO_ID', using ${matches[0]}" | tee -a "$LOG_FILE"
fi
AUTODEP_SCRIPT="${matches[0]}"

# PORT is exported by evaluate.sh per-slot. Forward it explicitly so the
# autodep script's default ${PORT:-3000} doesn't override.
PORT="${PORT:?PORT not set in environment; evaluate.sh should export this}"

echo "[autodep] REPO_ID=$REPO_ID PORT=$PORT REPO_DIR=$REPO_DIR" | tee -a "$LOG_FILE"
echo "[autodep] Dispatching to: $AUTODEP_SCRIPT" | tee -a "$LOG_FILE"

# The autodep scripts do their own `cd "$REPO_DIR"` based on the env var.
# They also internally tee to "$REPO_DIR/deploy_logs/host_runtime.log"; we
# additionally pipe their combined stdout/stderr into the evaluate.sh log so
# everything appears in one place.
exec env REPO_DIR="$REPO_DIR" PORT="$PORT" bash "$AUTODEP_SCRIPT" >>"$LOG_FILE" 2>&1
