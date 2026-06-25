#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/.." && pwd)"

PATCH_ROOT="$REPO_ROOT/final_result_dumps/main_bench"
CSV_PATH="$HARNESS_DIR/SAMPLE/input_100.csv"
OUT_DIR="$HARNESS_DIR/out/final_main_bench_current_rerun_$(date +%Y%m%d_%H%M%S)"
PARALLEL=20
NUM_RUNS_VALUE="${NUM_RUNS:-20}"
SMOKE_ONE_PER_MODEL=0
IDS_FILTER=""
MODELS_FILTER=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: harness/scripts/run_main_bench_patch_rerun.sh [options]

Rerun CWV measurement for archived final_result_dumps/main_bench patches.
Aider and Codex are intentionally excluded from this final-rerun wrapper.

Options:
  --patch-root DIR          Archived patch root (default: final_result_dumps/main_bench)
  --csv PATH                Input CSV (default: harness/SAMPLE/input_100.csv)
  --out-dir DIR             Output root (default: harness/out/final_main_bench_current_rerun_<ts>)
  --parallel N              evaluate.sh parallelism per model (default: 20)
  --num-runs N              CWV runs per device (default: NUM_RUNS env or 20)
  --models A,B              Limit archived model dirs/config names
  --ids 1,2,3               Limit every model to these CSV IDs
  --smoke-one-per-model     Pick one archived patch row per model
  --dry-run                 Print planned commands only
  --help                    Show this help

Examples:
  # Fast plumbing smoke across all non-Aider/Codex models.
  harness/scripts/run_main_bench_patch_rerun.sh --smoke-one-per-model --num-runs 1

  # Final CWV-only rerun, 20 runs/device, no PSI, no visual, no agents.
  harness/scripts/run_main_bench_patch_rerun.sh --parallel 20 --num-runs 20
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --patch-root)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --patch-root DIR" >&2; exit 1; }
      PATCH_ROOT="$1"; shift ;;
    --csv)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --csv PATH" >&2; exit 1; }
      CSV_PATH="$1"; shift ;;
    --out-dir)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --out-dir DIR" >&2; exit 1; }
      OUT_DIR="$1"; shift ;;
    --parallel)
      shift; [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N" >&2; exit 1; }
      PARALLEL="$1"; shift ;;
    --num-runs)
      shift; [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --num-runs N" >&2; exit 1; }
      NUM_RUNS_VALUE="$1"; shift ;;
    --models)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --models A,B" >&2; exit 1; }
      MODELS_FILTER="$1"; shift ;;
    --ids)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --ids 1,2,3" >&2; exit 1; }
      IDS_FILTER="$1"; shift ;;
    --smoke-one-per-model)
      SMOKE_ONE_PER_MODEL=1; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1 ;;
  esac
done

[[ "$PATCH_ROOT" = /* ]] || PATCH_ROOT="$REPO_ROOT/$PATCH_ROOT"
[[ "$CSV_PATH" = /* ]] || CSV_PATH="$REPO_ROOT/$CSV_PATH"
[[ "$OUT_DIR" = /* ]] || OUT_DIR="$REPO_ROOT/$OUT_DIR"

[[ -d "$PATCH_ROOT" ]] || { echo "Missing patch root: $PATCH_ROOT" >&2; exit 1; }
[[ -f "$CSV_PATH" ]] || { echo "Missing CSV: $CSV_PATH" >&2; exit 1; }

if [[ "$SMOKE_ONE_PER_MODEL" == "1" && -n "$IDS_FILTER" ]]; then
  echo "--smoke-one-per-model and --ids are mutually exclusive" >&2
  exit 1
fi

# Format: archived_dump_dir|evaluate_config
# closed_cc-aider and closed_cc-codex are intentionally excluded.
MODEL_MAP=(
  "closed_cc-opus-4.6|configs/closed/claude-opus.env"
  "closed_cc-sonnet-4.6|configs/closed/cc-sonnet-4.6.env"
  "closed_oc_gemini-2-5-flash|configs/closed/gemini-flash.env"
  "closed_oc_gemini-2-5-pro|configs/closed/gemini-pro.env"
  "closed_oc_gpt-4.1|configs/closed/gpt-4.1.env"
  "closed_oc_gpt-5|configs/closed/gpt-5.env"
  "closed_oc_gpt-5.1-codex|configs/closed/gpt-5.1-codex.env"
  "open_devstral-2-123b|configs/open/devstral-2-123b.env"
  "open_gemma-4-31b-it|configs/open/gemma-4-31b-it.env"
  "open_glm-4.7-flash|configs/open/glm-4.7-flash.env"
  "open_minimax-m2.7|configs/open/minimax-m2.7.env"
  "open_qwen3-coder-next|configs/open/qwen3-coder-next.env"
  "open_qwen3.5-122b-a10b|configs/open/qwen3.5-122b-a10b.env"
  "open_qwen3.5-27b|configs/open/qwen3.5-27b.env"
  "open_qwen3.5-35b-a3b|configs/open/qwen3.5-35b-a3b.env"
  "open_qwen3.5-397b-a17b|configs/open/qwen3.5-397b-a17b.env"
  "open_qwen3.5-9b|configs/open/qwen3.5-9b.env"
)

csv_has_id() {
  local id="$1"
  python3 - "$CSV_PATH" "$id" <<'PY'
import csv
import sys

csv.field_size_limit(sys.maxsize)
csv_path, wanted = sys.argv[1:3]
with open(csv_path, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row.get("ID") == wanted:
            raise SystemExit(0)
raise SystemExit(1)
PY
}

first_archived_id() {
  local model_patch_dir="$1"
  local job id
  while IFS= read -r job; do
    [[ -d "$job" ]] || continue
    [[ -f "$job/$(basename "$job").patch" ]] || continue
    id="$(basename "$job" | sed -E 's/^([0-9]+)_.*/\1/')"
    if csv_has_id "$id"; then
      printf '%s\n' "$id"
      return 0
    fi
  done < <(find "$model_patch_dir" -maxdepth 1 -mindepth 1 -type d | sort)
  return 1
}

model_selected() {
  local dump_dir="$1"
  local config_path="$2"
  [[ -z "$MODELS_FILTER" ]] && return 0
  local base config_base want
  base="$dump_dir"
  config_base="$(basename "$config_path" .env)"
  IFS=',' read -r -a wanted <<< "$MODELS_FILTER"
  for want in "${wanted[@]}"; do
    want="${want#"${want%%[![:space:]]*}"}"
    want="${want%"${want##*[![:space:]]}"}"
    [[ "$want" == "$base" || "$want" == "$config_base" ]] && return 0
  done
  return 1
}

write_filtered_csv() {
  local ids="$1"
  local out_csv="$2"
  python3 "$SCRIPT_DIR/create_rerun_csv.py" --csv "$CSV_PATH" --ids "$ids" --out "$out_csv"
}

summarize_artifacts() {
  local model="$1"
  local model_out="$2"
  python3 - "$model" "$model_out" <<'PY'
import json
import sys
from pathlib import Path

model, model_out = sys.argv[1:3]
results = Path(model_out) / "results"
jobs = [p for p in results.iterdir() if p.is_dir()] if results.exists() else []
mobile = 0
desktop = 0
bad = 0
for job in jobs:
    for name in ("mobile.json", "desktop.json"):
        path = job / name
        if not path.exists():
            continue
        if name == "mobile.json":
            mobile += 1
        else:
            desktop += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            bad += 1
            continue
        if data.get("status") != "success":
            bad += 1
print(f"{model}\t{len(jobs)}\t{mobile}\t{desktop}\t{bad}")
PY
}

mkdir -p "$OUT_DIR" "$OUT_DIR/csv" "$OUT_DIR/logs"
STATUS_FILE="$OUT_DIR/model_status.tsv"
ARTIFACT_FILE="$OUT_DIR/artifact_summary.tsv"
printf 'model\tconfig\tpatch_dir\tcsv\tstatus\texit_code\n' > "$STATUS_FILE"
printf 'model\tjobs\tmobile_json\tdesktop_json\tnon_success_json\n' > "$ARTIFACT_FILE"

echo "[main-bench-rerun] Patch root: $PATCH_ROOT"
echo "[main-bench-rerun] CSV:        $CSV_PATH"
echo "[main-bench-rerun] Output:     $OUT_DIR"
echo "[main-bench-rerun] Parallel:   $PARALLEL"
echo "[main-bench-rerun] Num runs:   $NUM_RUNS_VALUE"
echo "[main-bench-rerun] Excluding:  closed_cc-aider, closed_cc-codex"
[[ "$SMOKE_ONE_PER_MODEL" == "1" ]] && echo "[main-bench-rerun] Mode:       smoke one archived row per model"
[[ -n "$IDS_FILTER" ]] && echo "[main-bench-rerun] IDs:        $IDS_FILTER"
[[ -n "$MODELS_FILTER" ]] && echo "[main-bench-rerun] Models:     $MODELS_FILTER"

failed=0
ran=0
for entry in "${MODEL_MAP[@]}"; do
  dump_dir="${entry%%|*}"
  config_rel="${entry#*|}"
  config_path="$HARNESS_DIR/$config_rel"
  patch_dir="$PATCH_ROOT/$dump_dir"

  model_selected "$dump_dir" "$config_rel" || continue

  if [[ ! -d "$patch_dir" ]]; then
    echo "[main-bench-rerun] WARN: missing patch dir for $dump_dir: $patch_dir"
    printf '%s\t%s\t%s\t%s\tmissing_patch_dir\t1\n' "$dump_dir" "$config_rel" "$patch_dir" "" >> "$STATUS_FILE"
    failed=$((failed + 1))
    continue
  fi
  [[ -f "$config_path" ]] || { echo "Missing config: $config_path" >&2; exit 1; }

  run_csv="$CSV_PATH"
  if [[ "$SMOKE_ONE_PER_MODEL" == "1" ]]; then
    if ! id="$(first_archived_id "$patch_dir")"; then
      echo "[main-bench-rerun] WARN: no archived CSV row found for $dump_dir"
      printf '%s\t%s\t%s\t%s\tno_archived_csv_row\t1\n' "$dump_dir" "$config_rel" "$patch_dir" "" >> "$STATUS_FILE"
      failed=$((failed + 1))
      continue
    fi
    run_csv="$OUT_DIR/csv/${dump_dir}_smoke_${id}.csv"
    write_filtered_csv "$id" "$run_csv"
  elif [[ -n "$IDS_FILTER" ]]; then
    run_csv="$OUT_DIR/csv/${dump_dir}_ids.csv"
    write_filtered_csv "$IDS_FILTER" "$run_csv"
  fi

  model_out="$OUT_DIR/$dump_dir"
  log_file="$OUT_DIR/logs/${dump_dir}.log"
  cmd=(
    env
    "EVAL_OUT_DIR=$model_out"
    "NUM_RUNS=$NUM_RUNS_VALUE"
    "PARALLEL=$PARALLEL"
    "HOST_SANDBOX=${HOST_SANDBOX:-0}"
    "CWV_MEASURE_SANDBOX=${CWV_MEASURE_SANDBOX:-local}"
    "SANDBOX_MODE=${SANDBOX_MODE:-local}"
    bash "$HARNESS_DIR/evaluate.sh"
    --config "$config_path"
    --csv "$run_csv"
    --parallel "$PARALLEL"
    --skip-agent
    --patch-results-dir "$patch_dir"
    --skip-init-psi
    --skip-final-psi
    --skip-visual
    --no-serve-model
  )

  echo "[main-bench-rerun] ===== $dump_dir ====="
  echo "[main-bench-rerun] config=$config_rel patch_dir=$patch_dir csv=$run_csv"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[main-bench-rerun] DRY:'
    printf ' %q' "${cmd[@]}"
    printf '\n'
    printf '%s\t%s\t%s\t%s\tdry_run\t0\n' "$dump_dir" "$config_rel" "$patch_dir" "$run_csv" >> "$STATUS_FILE"
    ran=$((ran + 1))
    continue
  fi

  set +e
  "${cmd[@]}" >"$log_file" 2>&1
  rc=$?
  set -e
  summary="$(summarize_artifacts "$dump_dir" "$model_out")"
  printf '%s\n' "$summary" >> "$ARTIFACT_FILE"
  IFS=$'\t' read -r _summary_model summary_jobs summary_mobile summary_desktop summary_bad <<< "$summary"
  artifact_status=""
  if [[ "${summary_mobile:-0}" -eq 0 || "${summary_desktop:-0}" -eq 0 ]]; then
    rc=1
    artifact_status="missing_cwv"
  elif [[ "${summary_bad:-0}" -gt 0 ]]; then
    artifact_status="non_success_cwv"
  fi
  if [[ $rc -eq 0 ]]; then
    status="ok"
    echo "[main-bench-rerun] OK: $dump_dir"
  else
    status="${artifact_status:-failed}"
    failed=$((failed + 1))
    echo "[main-bench-rerun] FAILED: $dump_dir status=$status rc=$rc log=$log_file"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$dump_dir" "$config_rel" "$patch_dir" "$run_csv" "$status" "$rc" >> "$STATUS_FILE"
  ran=$((ran + 1))
done

if [[ "$ran" -eq 0 ]]; then
  echo "[main-bench-rerun] No models selected." >&2
  exit 1
fi

echo "[main-bench-rerun] Status: $STATUS_FILE"
echo "[main-bench-rerun] Artifacts: $ARTIFACT_FILE"
echo "[main-bench-rerun] Complete: ran=$ran failed=$failed"
[[ "$failed" -eq 0 ]]
