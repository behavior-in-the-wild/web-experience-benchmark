#!/usr/bin/env bash
# =============================================================================
# prepare_dataset.sh
#
# One-shot pipeline: mirror → filter → validate → build input.jsonl
#
# Stages:
#   1. fetch_live_assets.py     — Playwright-mirror pages from JSONL
#   2. check_minified_fast.py   — flag already-minified pages
#   3. compare_local_vs_live.py — validate mirror fidelity (console errors)
#   4. build_input_jsonl.py     — assemble harness_live_bench/SAMPLE/input.jsonl
#
# Usage:
#   bash prepare_dataset.sh [OPTIONS]
#
# Options:
#   --limit N            Process only first N JSONL entries (default: 5)
#   --workers N          Playwright parallel workers for stage 1 (default: 3)
#   --compare-workers N  Playwright workers for stage 3 (default: 2)
#   --max-new-errors N   Max new console errors allowed in mirror (default: 5)
#   --jsonl PATH         Input JSONL file (default: EDSSites_CWV_joined_top50_pages_top10.jsonl)
#   --artifacts PATH     Root folder for all run outputs (default: .eds_live_bench_artifacts)
#   --mirrors PATH       Mirror output root (default: <artifacts>/live_assets_eds)
#   --output PATH        input.jsonl output path (default: harness_live_bench/SAMPLE/input.jsonl)
#   --skip-compare       Skip stage 3 (mirror validation) — faster, less safe
#   --skip-minified      Skip stage 2 (minification check)
#   --force-stage N      Force re-run of stage N even if output exists (1-4)
# =============================================================================
set -euo pipefail

# =============================================================================
# Defaults
# =============================================================================
LIMIT=5
WORKERS=3
COMPARE_WORKERS=2
MAX_NEW_ERRORS=5
JSONL="EDSSites_CWV_joined_top50_pages_top10.jsonl"
ARTIFACTS_DIR=".eds_live_bench_artifacts"   # all run outputs live here
MIRRORS="live_assets_eds"              # relative to ARTIFACTS_DIR by default
OUTPUT="harness_live_bench/SAMPLE/input.jsonl"
SKIP_COMPARE=0
SKIP_MINIFIED=0
FORCE_STAGES=""

# =============================================================================
# Argument parsing
# =============================================================================
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)            shift; LIMIT="$1" ;;
    --workers)          shift; WORKERS="$1" ;;
    --compare-workers)  shift; COMPARE_WORKERS="$1" ;;
    --max-new-errors)   shift; MAX_NEW_ERRORS="$1" ;;
    --jsonl)            shift; JSONL="$1" ;;
    --artifacts)        shift; ARTIFACTS_DIR="$1" ;;
    --mirrors)          shift; MIRRORS="$1" ;;
    --output)           shift; OUTPUT="$1" ;;
    --skip-compare)     SKIP_COMPARE=1 ;;
    --skip-minified)    SKIP_MINIFIED=1 ;;
    --force-stage)      shift; FORCE_STAGES="$FORCE_STAGES $1" ;;
    -h|--help)
      echo "Usage: ./prepare_dataset.sh [options]"
      echo "  --limit N|all        Process only first N JSONL entries (default: 5)"
      echo "  --workers N          Playwright workers for stage 1 (default: 3)"
      echo "  --compare-workers N  Playwright workers for stage 3 (default: 2)"
      echo "  --max-new-errors N   Max new console errors allowed in mirror (default: 5)"
      echo "  --jsonl PATH         Input JSONL file (default: EDSSites_CWV_joined...)"
      echo "  --artifacts PATH     Root folder for all run outputs (default: .eds_live_bench_artifacts)"
      echo "  --mirrors PATH       Mirror output root (default: <artifacts>/live_assets_eds)"
      echo "  --output PATH        input.jsonl output path (default: harness_live_bench/SAMPLE/input.jsonl)"
      echo "  --skip-compare       Skip stage 3 (mirror validation)"
      echo "  --skip-minified      Skip stage 2 (minification check)"
      echo "  --force-stage N      Force re-run of stage N even if output exists (1-4)"
      exit 0
      ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

# Helper: check if a stage should be forced
should_force() { [[ " $FORCE_STAGES " == *" $1 "* ]]; }

# =============================================================================
# Resolve script location → project root
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

SCRIPTS="$ROOT/scripts/live_pages_benchmark"
JSONL_ABS="$(cd "$(dirname "$JSONL")" && pwd)/$(basename "$JSONL")"

# ── All run outputs live under a single artifacts root ────────────────────────
# 1. Make ARTIFACTS_DIR absolute (relative → under repo root)
[[ "$ARTIFACTS_DIR" = /* ]] || ARTIFACTS_DIR="$ROOT/$ARTIFACTS_DIR"
mkdir -p "$ARTIFACTS_DIR"

# 2. Resolve MIRRORS to an absolute path:
#    - absolute path  → use as-is
#    - bare name (no slash) → under ARTIFACTS_DIR  (default: live_assets_eds)
#    - relative path (has slash) → relative to ROOT
if [[ "$MIRRORS" = /* ]]; then
  MIRRORS_ABS="$MIRRORS"
elif [[ "$MIRRORS" != */* ]]; then
  MIRRORS_ABS="$ARTIFACTS_DIR/$MIRRORS"   # bare name → inside artifacts
else
  MIRRORS_ABS="$ROOT/$MIRRORS"            # relative path → relative to root
fi
OUTPUT_ABS="$ROOT/$OUTPUT"

# Work directory for intermediate files
WORK_DIR="$ARTIFACTS_DIR/.pipeline_work"
mkdir -p "$WORK_DIR"

MINIFIED_JSONL="$WORK_DIR/minified_check.jsonl"
COMPARISON_DIR="$ARTIFACTS_DIR/comparison_results"

# =============================================================================
# Helpers
# =============================================================================
log() { echo; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; echo "▶  $*"; echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"; }
ok()  { echo "✓  $*"; }
warn(){ echo "⚠  $*"; }

# =============================================================================
# Pipeline timing
# =============================================================================
PIPELINE_START=$(date +%s)
stage_times=""
record_stage_time() {
  local stage="$1" start="$2" end="$3"
  local duration=$(( end - start ))
  stage_times="$stage_times\"stage_${stage}_seconds\": $duration, "
}

# =============================================================================
# Sanity checks
# =============================================================================
[[ -f "$JSONL_ABS" ]]          || { echo "ERROR: JSONL not found: $JSONL_ABS"; exit 1; }
[[ -f "$SCRIPTS/fetch_live_assets.py" ]]    || { echo "ERROR: fetch_live_assets.py missing"; exit 1; }
[[ -f "$SCRIPTS/check_minified_fast.py" ]]  || { echo "ERROR: check_minified_fast.py missing"; exit 1; }
[[ -f "$SCRIPTS/compare_local_vs_live.py" ]] || { echo "ERROR: compare_local_vs_live.py missing"; exit 1; }
[[ -f "$SCRIPTS/build_input_jsonl.py" ]]     || { echo "ERROR: build_input_jsonl.py missing"; exit 1; }

echo "============================================="
echo " Live Benchmark Dataset Preparation Pipeline"
echo "============================================="
echo "  JSONL:         $JSONL_ABS"
echo "  Mirrors root:  $MIRRORS_ABS"
echo "  Output:        $OUTPUT_ABS"
echo "  --limit:       $LIMIT  (JSONL entries)"
echo "  --workers:     $WORKERS"
echo "  Skip minified check: $SKIP_MINIFIED"
echo "  Skip compare:        $SKIP_COMPARE"
echo "  Max new errors:      $MAX_NEW_ERRORS"
echo "  Force stages:        ${FORCE_STAGES:-none}"
echo "============================================="
echo

# =============================================================================
# Write a --limit-scoped JSONL slice (avoids passing domain-counts directly
# to scripts that don't support --limit natively for the JSONL input mode)
# =============================================================================
SLICED_JSONL="$WORK_DIR/input_slice.jsonl"
python3 - <<PYSCRIPT "$JSONL_ABS" "$LIMIT" "$SLICED_JSONL"
import json, sys
src, limit_str, dst = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    limit = int(limit_str)
except ValueError:
    limit = float("inf")

with open(src) as fin, open(dst, "w") as fout:
    for i, line in enumerate(fin):
        if i >= limit: break
        line = line.strip()
        if line: fout.write(line + "\n")
print(f"[slice] {limit_str} entries → {dst}", file=sys.stderr)
PYSCRIPT
ok "Sliced JSONL to $LIMIT entries → $SLICED_JSONL"

# =============================================================================
# STAGE 1 — Mirror live pages
# =============================================================================
STAGE1_START=$(date +%s)
log "STAGE 1 / 4 — Mirroring live pages (Playwright)"
echo "  Input:   $SLICED_JSONL"
echo "  Output:  $MIRRORS_ABS"
echo "  Workers: $WORKERS"
echo

python3 "$SCRIPTS/fetch_live_assets.py" \
  --input  "$SLICED_JSONL" \
  --output "$MIRRORS_ABS" \
  --workers "$WORKERS"

STAGE1_END=$(date +%s)
record_stage_time 1 "$STAGE1_START" "$STAGE1_END"
ok "Stage 1 complete ($(( STAGE1_END - STAGE1_START ))s)"

# =============================================================================
# STAGE 2 — Minification filter
# =============================================================================
STAGE2_START=$(date +%s)
if [[ "$SKIP_MINIFIED" -eq 1 ]]; then
  warn "Stage 2 skipped (--skip-minified)"
  # Write a stub that marks everything as non-minified so build_input_jsonl still works
  python3 - <<PYSCRIPT "$SLICED_JSONL" "$MINIFIED_JSONL"
import json, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as fin, open(dst, "w") as fout:
    for line in fin:
        row = json.loads(line.strip())
        domain = row.get("domain","")
        for page in row.get("cwv_top10_pages", []):
            url = page.get("url","")
            if url:
                fout.write(json.dumps({"domain":domain,"page_url":url,"is_minified":False,"signal":None,"matched_urls":[],"status":"skipped"}) + "\n")
PYSCRIPT
else
  log "STAGE 2 / 4 — Minification check"
  echo "  Input:   $SLICED_JSONL"
  echo "  Mirrors: $MIRRORS_ABS"
  echo "  Output:  $MINIFIED_JSONL"
  echo

  # Use --mirrors to check local mirrors instead of fetching live pages
  python3 "$SCRIPTS/check_minified_fast.py" \
    --jsonl    "$SLICED_JSONL" \
    --output   "$MINIFIED_JSONL" \
    --mirrors  "$MIRRORS_ABS" \
    --workers  40

  MINIFIED_COUNT=$(python3 -c "
import json
n = sum(1 for l in open('$MINIFIED_JSONL') if json.loads(l).get('is_minified'))
print(n)
")
  NONMIN_COUNT=$(python3 -c "
import json
n = sum(1 for l in open('$MINIFIED_JSONL') if not json.loads(l).get('is_minified'))
print(n)
")
  ok "Stage 2 complete — $MINIFIED_COUNT minified (will exclude), $NONMIN_COUNT non-minified (will include)"
fi
STAGE2_END=$(date +%s)
record_stage_time 2 "$STAGE2_START" "$STAGE2_END"

# =============================================================================
# STAGE 3 — Mirror validation (compare local vs live)
# =============================================================================
STAGE3_START=$(date +%s)
if [[ "$SKIP_COMPARE" -eq 1 ]]; then
  warn "Stage 3 skipped (--skip-compare)"
else
  log "STAGE 3 / 4 — Mirror validation (console errors + screenshots)"
  echo "  Assets: $MIRRORS_ABS"
  echo "  Output: $COMPARISON_DIR"
  echo "  Workers: $COMPARE_WORKERS"
  echo

  FORCE_FLAG=""
  if should_force 3; then
    FORCE_FLAG="--force"
    echo "  (--force: re-running all comparisons)"
  fi

  python3 "$SCRIPTS/compare_local_vs_live.py" \
    --assets-dir "$MIRRORS_ABS" \
    --output     "$COMPARISON_DIR" \
    --workers    "$COMPARE_WORKERS" \
    $FORCE_FLAG

  # Summarize
  PASSED=$(python3 - <<PYSCRIPT "$COMPARISON_DIR" "$MAX_NEW_ERRORS"
import json, pathlib, sys
comp_dir, max_errs = pathlib.Path(sys.argv[1]), int(sys.argv[2])
passed = failed = 0
for p in comp_dir.rglob("comparison.json"):
    d = json.loads(p.read_text())
    devices = d.get("devices", {})
    if devices:
        worst = max(
            len(dd.get("console_diff", {}).get("new_errors_local", []))
            for dd in devices.values()
        )
    else:
        worst = len(d.get("console_diff", {}).get("new_errors_local", []))
    if worst <= max_errs:
        passed += 1
    else:
        failed += 1
print(f"{passed} passed, {failed} failed (>{max_errs} new errors)")
PYSCRIPT
)
  ok "Stage 3 complete — $PASSED"
fi
STAGE3_END=$(date +%s)
record_stage_time 3 "$STAGE3_START" "$STAGE3_END"

# =============================================================================
# STAGE 4 — Build input.jsonl (using standalone build_input_jsonl.py)
# =============================================================================
STAGE4_START=$(date +%s)
log "STAGE 4 / 4 — Building harness dataset (input.jsonl)"
echo "  JSONL:       $SLICED_JSONL"
echo "  Mirrors:     $MIRRORS_ABS"
echo "  Min check:   $MINIFIED_JSONL"
echo "  Compare dir: $COMPARISON_DIR"
echo "  Output:      $OUTPUT_ABS"
echo

mkdir -p "$(dirname "$OUTPUT_ABS")"

# Build arguments for optional flags
BUILD_ARGS=""
if [[ "$SKIP_MINIFIED" -eq 0 ]]; then
  BUILD_ARGS="$BUILD_ARGS --minified-jsonl $MINIFIED_JSONL"
fi
if [[ "$SKIP_COMPARE" -eq 0 ]]; then
  BUILD_ARGS="$BUILD_ARGS --comparison-dir $COMPARISON_DIR --max-new-errors $MAX_NEW_ERRORS"
fi

python3 "$SCRIPTS/build_input_jsonl.py" \
  --jsonl   "$SLICED_JSONL" \
  --mirrors "$MIRRORS_ABS" \
  --output  "$OUTPUT_ABS" \
  $BUILD_ARGS

STAGE4_END=$(date +%s)
record_stage_time 4 "$STAGE4_START" "$STAGE4_END"
ok "Stage 4 complete"

# Copy input.jsonl to artifacts root for easy access outside the harness
cp "$OUTPUT_ABS" "$ARTIFACTS_DIR/eds_live_bench.jsonl"
ok "Copied input.jsonl → $ARTIFACTS_DIR/eds_live_bench.jsonl"

# =============================================================================
# Pipeline manifest — reproducibility metadata
# =============================================================================
PIPELINE_END=$(date +%s)
PIPELINE_DURATION=$(( PIPELINE_END - PIPELINE_START ))

PIPELINE_MANIFEST="$WORK_DIR/pipeline_run.json"
python3 - <<PYSCRIPT "$PIPELINE_MANIFEST" "$PIPELINE_DURATION" "$LIMIT" "$WORKERS" "$COMPARE_WORKERS" "$MAX_NEW_ERRORS" "$SKIP_COMPARE" "$SKIP_MINIFIED" "$JSONL_ABS" "$MIRRORS_ABS" "$OUTPUT_ABS"
import json, sys, platform, subprocess, shutil

manifest_path = sys.argv[1]
duration_s    = int(sys.argv[2])
limit_str     = sys.argv[3]
limit         = None if limit_str == "all" else int(limit_str)
workers       = int(sys.argv[4])
compare_workers = int(sys.argv[5])
max_new_errors  = int(sys.argv[6])
skip_compare    = sys.argv[7] == "1"
skip_minified   = sys.argv[8] == "1"
jsonl_abs       = sys.argv[9]
mirrors_abs     = sys.argv[10]
output_abs      = sys.argv[11]

# Git SHA
git_sha = None
try:
    git_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
    ).strip()
except Exception:
    pass

# Python version
python_version = platform.python_version()

# Playwright version
playwright_version = None
try:
    result = subprocess.check_output(
        ["python3", "-c", "import playwright; print(playwright.__version__)"],
        text=True, stderr=subprocess.DEVNULL
    ).strip()
    playwright_version = result
except Exception:
    pass

# Chromium version (from installed browsers)
chromium_version = None
try:
    pw_path = shutil.which("playwright")
    if pw_path:
        result = subprocess.check_output(
            ["playwright", "install", "--list"], text=True, stderr=subprocess.DEVNULL
        )
        for line in result.splitlines():
            if "chromium" in line.lower():
                chromium_version = line.strip()
                break
except Exception:
    pass

# Node version
node_version = None
try:
    node_version = subprocess.check_output(
        ["node", "--version"], text=True, stderr=subprocess.DEVNULL
    ).strip()
except Exception:
    pass

# Count output lines
output_count = 0
try:
    with open(output_abs) as f:
        output_count = sum(1 for _ in f)
except Exception:
    pass

import time
manifest = {
    "pipeline_run_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "total_duration_seconds": duration_s,
    "git_sha": git_sha,
    "python_version": python_version,
    "playwright_version": playwright_version,
    "chromium_info": chromium_version,
    "node_version": node_version,
    "os": f"{platform.system()} {platform.release()}",
    "args": {
        "limit": limit,
        "workers": workers,
        "compare_workers": compare_workers,
        "max_new_errors": max_new_errors,
        "skip_compare": skip_compare,
        "skip_minified": skip_minified,
        "jsonl": jsonl_abs,
        "mirrors": mirrors_abs,
        "output": output_abs,
    },
    "output_pages": output_count,
}

with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"Pipeline manifest → {manifest_path}", file=sys.stderr)
PYSCRIPT

# =============================================================================
# Summary
# =============================================================================
echo
echo "============================================="
echo " Pipeline complete (${PIPELINE_DURATION}s)"
echo "============================================="
FINAL_COUNT=$(wc -l < "$OUTPUT_ABS" || echo 0)
echo "  Pages in input.jsonl: $FINAL_COUNT"
echo "  Output: $OUTPUT_ABS"
echo "  Pipeline manifest:    $PIPELINE_MANIFEST"
echo
echo "Next step:"
echo "  cd harness_live_bench && bash evaluate.sh"
echo "============================================="
