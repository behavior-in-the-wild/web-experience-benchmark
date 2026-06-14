#!/usr/bin/env bash
# CPU-affinity isolation test.
# Re-runs CWV on ~42 fixed patches at PARALLEL=20,10,15,5 to verify that
# LCP variance stays consistent regardless of concurrency level.
#
# Usage:
#   bash harness/run_isolation_test.sh
#
# Overrides (all optional):
#   TEST_ROOT=isolation_test           Output dir
#   LIMIT=14                           Unique sites (~14 × 3 = 42 patches)
#   NUM_RUNS=3                         CWV Lighthouse runs per patch
#   PARALLELS=20,10,15,5               Comma-separated levels to test
#   CPUS_PER_SLOT=4                    CPUs per job slot (affinity width)
#   BASE_PORT=15100                    First port (slots get BASE_PORT+slot_id)
#   SKIP_CLONE=1                       Skip re-cloning if baselines already cached
#   DUMP_MODEL=gemma-4-31b-it          Model whose patches we re-measure
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HARNESS/.." && pwd)"

export TEST_ROOT="${TEST_ROOT:-$ROOT/isolation_test}"
export LIMIT="${LIMIT:-14}"
export NUM_RUNS="${NUM_RUNS:-3}"
export PARALLELS="${PARALLELS:-20,10,15,5}"
export CPUS_PER_SLOT="${CPUS_PER_SLOT:-4}"
export BASE_PORT="${BASE_PORT:-15100}"
export SKIP_CLONE="${SKIP_CLONE:-0}"
export DUMP_MODEL="${DUMP_MODEL:-gemma-4-31b-it}"

# Activate venv
[[ -f "$ROOT/.venv/bin/activate" ]] && source "$ROOT/.venv/bin/activate"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export TMPDIR="${TMPDIR:-/dev/shm}"

echo "[isolation-test] Starting isolation test"
echo "[isolation-test] TEST_ROOT=$TEST_ROOT"
echo "[isolation-test] PARALLELS=$PARALLELS  LIMIT=$LIMIT  NUM_RUNS=$NUM_RUNS"
echo "[isolation-test] CPUS_PER_SLOT=$CPUS_PER_SLOT  BASE_PORT=$BASE_PORT"
echo ""

# Kill any lingering servers on our port range before starting
MAX_PARALLEL=$(echo "$PARALLELS" | tr ',' '\n' | sort -rn | head -1)
for p in $(seq "$BASE_PORT" "$(( BASE_PORT + MAX_PARALLEL - 1 ))"); do
    fuser -k "${p}/tcp" 2>/dev/null || true
done

time python3 "$ROOT/scripts/isolation_test/isolation_test.py"
