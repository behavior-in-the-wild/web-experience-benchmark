#!/bin/bash
set -a
source /dev/shm/ayush/web-experience-benchmark/.env
set +a

echo "[$(date)] Starting cwv_baseline_scores sync..."
aws s3 sync /dev/shm/ayush/web-experience-benchmark/cwv_baseline_scores \
  s3://crawldatafromgcp/somesh/swe_bench_final_dumps/cwv_baseline_scores_input_100/ \
  --region us-east-1 --no-progress
echo "[$(date)] cwv_baseline_scores DONE"

echo "[$(date)] Starting final_result_dumps sync..."
aws s3 sync /dev/shm/ayush/web-experience-benchmark/final_result_dumps \
  s3://crawldatafromgcp/somesh/swe_bench_final_dumps/final_result_dumps_input_100/ \
  --region us-east-1 --no-progress
echo "[$(date)] final_result_dumps DONE"
