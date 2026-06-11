#!/usr/bin/env bash
# cleanup_to_93.sh
# ─────────────────────────────────────────────────────────────
# 1) Remove the 7 invalid job folders from every model run dir
#    AND backup_regression so each model has exactly 93 folders.
# 2) Strip "scrape artifacts" from the remaining folders, keeping
#    only the 4 key eval JSONs + the agent patch file:
#      visual.json, mobile.json, desktop.json, baseline_meta.json,
#      *.patch, usage.json, agent.log
#
# Usage:
#   bash scripts/cleanup_to_93.sh           # live run
#   DRY=1 bash scripts/cleanup_to_93.sh    # dry-run: print only
# ─────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY="${DRY:-0}"

BAD_IDS=(201 241 259 826 2248 2946 3527)

# Files to KEEP in each job folder (everything else deleted)
KEEP_FILES=(
  visual.json
  mobile.json
  desktop.json
  baseline_meta.json
  usage.json
  agent.log
)
# Also keep any *.patch file (matched by glob below)

log()  { echo "[cleanup] $*"; }
doit() {
  if [[ "$DRY" == "1" ]]; then
    echo "  [dry] $*"
  else
    eval "$*"
  fi
}

# ─────────────────────────────────────────────────────────────
# Step 1: Remove bad ID folders
# ─────────────────────────────────────────────────────────────
remove_bad_ids() {
  local results_dir="$1"
  local label_suffix="$2"   # e.g. _template_opencode_os or _template_gemini

  [[ -d "$results_dir" ]] || return 0

  local removed=0
  for id in "${BAD_IDS[@]}"; do
    local folder="$results_dir/${id}${label_suffix}"
    if [[ -d "$folder" ]]; then
      log "  remove: $folder"
      doit "rm -rf '$folder'"
      removed=$((removed + 1))
    fi
  done
  # Also remove any "scratch" folder (bare "scratch" OR "scratch_<suffix>")
  for scratch_name in "scratch" "scratch${label_suffix}"; do
    local scratch="$results_dir/$scratch_name"
    if [[ -d "$scratch" ]]; then
      log "  remove: $scratch"
      doit "rm -rf '$scratch'"
      removed=$((removed + 1))
    fi
  done
  [[ $removed -gt 0 ]] && log "  → removed $removed folder(s) from $results_dir"
}

# ─────────────────────────────────────────────────────────────
# Step 2: Strip artifacts inside each remaining job folder
# ─────────────────────────────────────────────────────────────
strip_artifacts() {
  local results_dir="$1"

  [[ -d "$results_dir" ]] || return 0

  for job_dir in "$results_dir"/*/; do
    [[ -d "$job_dir" ]] || continue

    # Delete everything that is NOT in KEEP_FILES and NOT a *.patch
    for item in "$job_dir"*; do
      [[ -e "$item" ]] || continue
      local base
      base="$(basename "$item")"

      # Keep *.patch files
      [[ "$base" == *.patch ]] && continue

      # Keep files in the KEEP list
      local keep=0
      for kf in "${KEEP_FILES[@]}"; do
        [[ "$base" == "$kf" ]] && keep=1 && break
      done
      [[ $keep -eq 1 ]] && continue

      # Delete anything else (screenshots, visual_v2_work, host.log, *.stderr, *.txt, etc.)
      if [[ -d "$item" ]]; then
        doit "rm -rf '$item'"
      else
        doit "rm -f '$item'"
      fi
    done
  done
}

# ─────────────────────────────────────────────────────────────
# Process all model run dirs
# ─────────────────────────────────────────────────────────────

declare -A GROUP_SUFFIX=(
  ["oss_model_runs"]="_template_opencode_os"
  ["oss_scale_eval_run"]="_template_opencode_os"
  ["closed_model_runs"]="_template_gemini"
)

declare -A GROUP_MODELS=(
  ["oss_model_runs"]="gemma-4-31b-it glm-4.7-flash qwen3-coder-next devstral-2-123b minimax-m2.7"
  ["oss_scale_eval_run"]="qwen3.5-9b qwen3.5-27b qwen3.5-35b-a3b qwen3.5-122b-a10b qwen3.5-397b-a17b"
  ["closed_model_runs"]="gemini-2-5-flash gemini-2-5-pro"
)

for group in oss_model_runs oss_scale_eval_run closed_model_runs; do
  suffix="${GROUP_SUFFIX[$group]}"
  for model in ${GROUP_MODELS[$group]}; do
    results="$ROOT/$group/$model/results"
    log "── $group/$model ──"
    remove_bad_ids "$results" "$suffix"
    strip_artifacts "$results"
    count=$(ls "$results" 2>/dev/null | wc -l || echo "?")
    log "  → $count folders remaining"
  done
done

# backup_regression (mixed suffixes — detect per model)
log "── backup_regression ──"
for model in $(ls "$ROOT/backup_regression/" 2>/dev/null); do
  results="$ROOT/backup_regression/$model/results"
  [[ -d "$results" ]] || continue

  # Auto-detect suffix from first folder name
  first=$(ls "$results" | head -1)
  suffix=""
  [[ "$first" == *_template_opencode_os ]] && suffix="_template_opencode_os"
  [[ "$first" == *_template_gemini      ]] && suffix="_template_gemini"

  log "  $model (suffix=$suffix)"
  remove_bad_ids "$results" "$suffix"
  strip_artifacts "$results"
  count=$(ls "$results" 2>/dev/null | wc -l || echo "?")
  log "  → $count folders remaining"
done

log ""
log "Done. Final folder counts:"
for group in oss_model_runs oss_scale_eval_run closed_model_runs; do
  for model in ${GROUP_MODELS[$group]}; do
    count=$(ls "$ROOT/$group/$model/results" 2>/dev/null | wc -l || echo "?")
    echo "  $group/$model: $count"
  done
done
echo "  --- backup_regression ---"
for model in $(ls "$ROOT/backup_regression/" 2>/dev/null); do
  count=$(ls "$ROOT/backup_regression/$model/results" 2>/dev/null | wc -l || echo "?")
  echo "  backup_regression/$model: $count"
done
