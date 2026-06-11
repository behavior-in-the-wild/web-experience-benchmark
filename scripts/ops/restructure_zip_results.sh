#!/usr/bin/env bash
# Restructures results_unzipped/ into closed_model_runs/ following
# the same layout as existing gemini-2-5-pro / oss_model_runs dirs.
set -euo pipefail

ROOT="/dev/shm/ayush/web-experience-benchmark"
SRC="$ROOT/results_unzipped/results"
DEST="$ROOT/closed_model_runs"

process_model() {
  local zip_dir="$1" slug="$2" agent="$3"
  local src_dir="$SRC/$zip_dir"
  local dest_model="$DEST/$slug"
  echo "=== $zip_dir → $slug (agent: $agent) ==="
  mkdir -p "$dest_model/results"
  local new=0 skipped=0

  for patch in "$src_dir"/*_template_${agent}.patch; do
    [ -f "$patch" ] || continue
    local filename; filename=$(basename "$patch")
    local site_id="${filename%%_template_*}"
    local site_dir="$dest_model/results/${site_id}_template_${agent}"
    if [ -d "$site_dir" ]; then skipped=$((skipped+1)); continue; fi
    mkdir -p "$site_dir"
    cp "$patch" "$site_dir/"
    local b="$src_dir/${site_id}_template_${agent}"
    [ -f "${b}_agent.log" ]      && cp "${b}_agent.log"      "$site_dir/agent.log"
    [ -f "${b}_plan.md" ]        && cp "${b}_plan.md"        "$site_dir/agent.log_plan.md"
    [ -f "${b}_usage.json" ]     && cp "${b}_usage.json"     "$site_dir/agent.log_usage.json"
    [ -f "${b}_cwv.env" ]        && cp "${b}_cwv.env"        "$site_dir/cwv.env"
    [ -f "${b}_phase1.ndjson" ]  && cp "${b}_phase1.ndjson"  "$site_dir/phase1.ndjson"
    [ -f "${b}_phase2.ndjson" ]  && cp "${b}_phase2.ndjson"  "$site_dir/phase2.ndjson"
    new=$((new+1))
  done

  # Phase prompts at model level (same for all sites)
  for phase in 1 2; do
    if [ ! -f "$dest_model/phase${phase}_prompt.txt" ]; then
      local first; first=$(ls "$src_dir"/*_template_${agent}_phase${phase}_prompt.txt 2>/dev/null | head -1 || true)
      [ -n "$first" ] && cp "$first" "$dest_model/phase${phase}_prompt.txt"
    fi
  done

  local total; total=$(ls -d "$dest_model/results"/*/ 2>/dev/null | wc -l)
  echo "  added=$new  skipped=$skipped  total=$total"
}

process_model  results_100_CC_Opus4.6         cc-opus-4.6        claudecode
process_model  results_100_CC_Sonnet4.6        cc-sonnet-4.6      claudecode
process_model  results_100_OC_Gemini2.5-flash  gemini-2-5-flash   gemini
process_model  results_100_OC_Gemini2.5-pro    gemini-2-5-pro     gemini
process_model  results_100_OC_GPT4.1           gpt-4.1            opencodegpt41
process_model  results_100_OC_GPT5             gpt-5              opencode
process_model  results_100_OC_GPT51Codex       gpt-5.1-codex      opencodegpt51codex

echo ""
echo "=== Final closed_model_runs ==="
for slug in "$DEST"/*/; do
  n=$(ls -d "$slug"results/*/ 2>/dev/null | wc -l)
  printf "  %-25s %d sites\n" "$(basename "$slug")" "$n"
done
