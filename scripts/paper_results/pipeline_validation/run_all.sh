#!/usr/bin/env bash
# Reproducible driver for the CWV pipeline-validation suite.
# Reuses production hosting/measurement infra (docker_tool.hosting,
# cwv_tool.cwv_benchmark, cwv_tool.performance_testing) via _val_lib.py.
#
# Usage:
#   bash scripts/paper_results/pipeline_validation/run_all.sh [N_SITES]
#
# Tests 4 and 5 read only existing dumps (fast). Tests 1-3 reconstruct sites
# (clone @ commit, optional git apply) and take fresh controlled measurements.
set -uo pipefail
cd /dev/shm/ayush/web-experience-benchmark

export PYTHONPATH=src
export TMPDIR=/dev/shm/ayush/web-experience-benchmark/.tmp
export WEB_BENCH_REPO_CACHE=/dev/shm/ayush/web-experience-benchmark/.repo_cache
export SANDBOX_LOCK_DIR=/dev/shm/ayush/web-experience-benchmark/.sandbox_locks
export CWV_SANDBOX=0           # single-site runs: no slot scheduling
export VAL_HOST_MODE=${VAL_HOST_MODE:-local}
mkdir -p "$TMPDIR" "$WEB_BENCH_REPO_CACHE" "$SANDBOX_LOCK_DIR"

N=${1:-6}
D=scripts/paper_results/pipeline_validation

echo "########## existing-data tests (no measurement) ##########"
python3 $D/test4_network_attribution.py
echo; python3 $D/test5_regression_agreement.py

echo; echo "########## live measurement tests ##########"
python3 $D/00_select_sample.py "$N"
echo; python3 $D/test1_aa_null.py
echo; python3 $D/test2_settle_sweep.py
echo; python3 $D/test3_interaction_toggle.py
echo; echo "ALL DONE. JSON artifacts in $D/test*.json"
