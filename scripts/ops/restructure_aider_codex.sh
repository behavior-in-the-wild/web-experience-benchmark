#!/usr/bin/env bash
# Restructure flat results_aider/ and results_codex/ into the closed_model_runs/
# directory layout expected by run_cwv_evals_aider_codex_row.sh.
#
# Input layout (flat files, all in one directory):
#   results_aider/{tid}_template_aider.patch
#   results_aider/{tid}_template_aider_agent.log
#   results_aider/{tid}_template_aider_desktop.json
#   results_aider/{tid}_template_aider_mobile.json
#   results_aider/{tid}_template_aider_cwv_stderr.txt
#   results_aider/{tid}_template_aider_host.log
#
# Output layout (per-job dirs):
#   closed_model_runs/aider/results/{tid}_template_aider/
#     {tid}_template_aider.patch
#     agent.log
#     desktop.json
#     mobile.json
#     cwv_stderr.txt
#     host.log
#
# Also skips any TID that does NOT have Phase 1 completed (no plan.md written).
# Based on current analysis all 30 aider and 30 codex pass — but guard is kept.
#
# Usage:
#   bash scripts/restructure_aider_codex.sh            # dry-run (echo only)
#   bash scripts/restructure_aider_codex.sh --apply    # actually copy
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

declare -A TOOL_PLAN_PATTERN=(
  [aider]="Phase 1 complete"
  [codex]="plan.md size="
)

restructure_tool() {
  local tool="$1"           # aider | codex
  local src_dir="$ROOT/results_${tool}"
  local dst_base="$ROOT/closed_model_runs/${tool}/results"
  local plan_pattern="${TOOL_PLAN_PATTERN[$tool]}"

  if [[ ! -d "$src_dir" ]]; then
    echo "[restructure] ERROR: source dir not found: $src_dir" >&2
    return 1
  fi

  local kept=0 skipped=0

  for patch_file in "$src_dir"/*_template_${tool}.patch; do
    [[ -f "$patch_file" ]] || continue
    local fname
    fname="$(basename "$patch_file")"
    # e.g. 101_template_aider.patch → tid=101, job_label=101_template_aider
    local job_label="${fname%.patch}"                      # 101_template_aider
    local tid="${job_label%%_template_*}"                  # 101

    # Verify plan.md was actually written in Phase 1
    local agent_log="$src_dir/${job_label}_agent.log"
    if [[ ! -f "$agent_log" ]]; then
      echo "[restructure] SKIP $tid ($tool): no agent.log"
      (( skipped++ )) || true
      continue
    fi
    if ! grep -q "$plan_pattern" "$agent_log" 2>/dev/null; then
      echo "[restructure] SKIP $tid ($tool): plan.md not completed in Phase 1"
      (( skipped++ )) || true
      continue
    fi

    local dst_dir="$dst_base/$job_label"

    if [[ "$APPLY" == "1" ]]; then
      mkdir -p "$dst_dir"

      # patch file
      cp "$patch_file" "$dst_dir/${job_label}.patch"

      # agent log
      cp "$agent_log" "$dst_dir/agent.log"

      # CWV outputs
      [[ -f "$src_dir/${job_label}_desktop.json"   ]] && cp "$src_dir/${job_label}_desktop.json"   "$dst_dir/desktop.json"
      [[ -f "$src_dir/${job_label}_mobile.json"    ]] && cp "$src_dir/${job_label}_mobile.json"    "$dst_dir/mobile.json"
      [[ -f "$src_dir/${job_label}_cwv_stderr.txt" ]] && cp "$src_dir/${job_label}_cwv_stderr.txt" "$dst_dir/cwv_stderr.txt"
      [[ -f "$src_dir/${job_label}_host.log"       ]] && cp "$src_dir/${job_label}_host.log"       "$dst_dir/host.log"

      echo "[restructure] OK $tid ($tool) → $dst_dir"
    else
      echo "[restructure] DRY-RUN $tid ($tool) → $dst_dir"
    fi
    (( kept++ )) || true
  done

  echo "[restructure] $tool: kept=$kept  skipped=$skipped"
}

echo "=== Restructuring aider ==="
restructure_tool aider

echo ""
echo "=== Restructuring codex ==="
restructure_tool codex

if [[ "$APPLY" == "0" ]]; then
  echo ""
  echo "Dry-run complete. Run with --apply to copy files."
fi
