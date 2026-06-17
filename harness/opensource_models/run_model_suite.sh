#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/.." && pwd)"

SUITE_CONFIG="$HARNESS_DIR/configs/suites/oss-models.env"
SOURCE_CONFIG=""
CSV_PATH="$HARNESS_DIR/SAMPLE/input_100.csv"
PARALLEL=""
LIMIT=""
MODELS_FILTER=""
RESUME_DIR=""
SERVE_MODEL=1
PATCH_RESULTS_FROM_OUTPUT=0
eval_args=()

usage() {
  cat <<'EOF'
Usage: harness/opensource_models/run_model_suite.sh [options] [-- evaluate.sh args...]

Options:
  --config PATH        Suite config with MODEL_CONFIGS=(...) entries
  --source-config PATH Source config passed to evaluate.sh
  --models A,B,C      Comma-separated model labels/config basenames to run
  --csv PATH          CSV passed to evaluate.sh
  --parallel N        Parallel jobs passed to evaluate.sh
  --limit N           Limit rows passed to evaluate.sh
  --resume-dir DIR    Existing suite output root to resume into
  --patch-results-from-output
                      Pass each model's output results dir back as --patch-results-dir
  --no-serve-model    Do not start vLLM; evaluate.sh uses MODEL_ENDPOINT/OPENAI_BASE_URL
  --help, -h          Show this message

Any args after --, or the first unknown --flag, are passed to evaluate.sh.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --config PATH"; exit 1; }
      SUITE_CONFIG="$1"; shift ;;
    --source-config)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --source-config PATH"; exit 1; }
      SOURCE_CONFIG="$1"; shift ;;
    --models)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --models A,B,C"; exit 1; }
      MODELS_FILTER="$1"; shift ;;
    --csv)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --csv PATH"; exit 1; }
      CSV_PATH="$1"; shift ;;
    --parallel)
      shift; [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --parallel N"; exit 1; }
      PARALLEL="$1"; shift ;;
    --limit)
      shift; [[ $# -gt 0 && "$1" =~ ^[0-9]+$ ]] || { echo "Usage: --limit N"; exit 1; }
      LIMIT="$1"; shift ;;
    --resume-dir)
      shift; [[ $# -gt 0 ]] || { echo "Usage: --resume-dir DIR"; exit 1; }
      RESUME_DIR="$1"; shift ;;
    --patch-results-from-output)
      PATCH_RESULTS_FROM_OUTPUT=1; shift ;;
    --no-serve-model)
      SERVE_MODEL=0; shift ;;
    --help|-h)
      usage; exit 0 ;;
    --)
      shift; eval_args+=("$@"); break ;;
    --*)
      eval_args+=("$@"); break ;;
    *)
      if [[ -z "$MODELS_FILTER" ]]; then
        MODELS_FILTER="$1"
      else
        MODELS_FILTER="$MODELS_FILTER,$1"
      fi
      shift ;;
  esac
done

if [[ "$SUITE_CONFIG" != /* ]]; then
  if [[ -f "$SUITE_CONFIG" ]]; then
    SUITE_CONFIG="$(cd "$(dirname "$SUITE_CONFIG")" && pwd)/$(basename "$SUITE_CONFIG")"
  elif [[ -f "$HARNESS_DIR/$SUITE_CONFIG" ]]; then
    SUITE_CONFIG="$HARNESS_DIR/$SUITE_CONFIG"
  else
    SUITE_CONFIG="$REPO_ROOT/$SUITE_CONFIG"
  fi
fi
[[ -f "$SUITE_CONFIG" ]] || { echo "Missing suite config: $SUITE_CONFIG"; exit 1; }

# shellcheck disable=SC1090
source "$SUITE_CONFIG"
[[ ${#MODEL_CONFIGS[@]} -gt 0 ]] || { echo "Suite config has no MODEL_CONFIGS entries: $SUITE_CONFIG"; exit 1; }

[[ "$CSV_PATH" = /* ]] || CSV_PATH="$(cd "$(dirname "$CSV_PATH")" && pwd)/$(basename "$CSV_PATH")"
[[ -f "$CSV_PATH" ]] || { echo "Missing CSV: $CSV_PATH"; exit 1; }

if [[ -n "$RESUME_DIR" ]]; then
  [[ "$RESUME_DIR" = /* ]] || RESUME_DIR="$(cd "$RESUME_DIR" && pwd)"
  [[ -d "$RESUME_DIR" ]] || { echo "Missing resume dir: $RESUME_DIR"; exit 1; }
  LOG_DIR="$RESUME_DIR"
else
  RUN_TS="$(date +%Y%m%d_%H%M%S)"
  suite_name="$(basename "$SUITE_CONFIG" .env)"
  LOG_DIR="$HARNESS_DIR/out/$suite_name/$RUN_TS"
  mkdir -p "$LOG_DIR"
fi

model_name_for_config() {
  local cfg="$1"
  (
    MODEL_NAME=""
    RESULTS_LABEL=""
    # shellcheck disable=SC1090
    source "$cfg"
    printf '%s\n' "${MODEL_NAME:-${RESULTS_LABEL:-$(basename "$cfg" .env)}}"
  )
}

config_selected() {
  local cfg="$1"
  [[ -z "$MODELS_FILTER" ]] && return 0
  local name base want
  name="$(model_name_for_config "$cfg")"
  base="$(basename "$cfg" .env)"
  IFS=',' read -r -a _wanted <<< "$MODELS_FILTER"
  for want in "${_wanted[@]}"; do
    want="${want#"${want%%[![:space:]]*}"}"
    want="${want%"${want##*[![:space:]]}"}"
    [[ "$want" == "$name" || "$want" == "$base" ]] && return 0
  done
  return 1
}

echo "[suite] Config:  $SUITE_CONFIG"
echo "[suite] CSV:     $CSV_PATH"
echo "[suite] Output:  $LOG_DIR"
[[ -n "$MODELS_FILTER" ]] && echo "[suite] Models:  $MODELS_FILTER"

ran=0
for cfg_rel in "${MODEL_CONFIGS[@]}"; do
  cfg="$cfg_rel"
  [[ "$cfg" = /* ]] || cfg="$HARNESS_DIR/configs/$cfg"
  [[ -f "$cfg" ]] || { echo "Missing model config: $cfg"; exit 1; }
  config_selected "$cfg" || continue

  model_name="$(model_name_for_config "$cfg")"
  model_dir="$LOG_DIR/$model_name"
  mkdir -p "$model_dir"

  args=(--config "$cfg" --csv "$CSV_PATH")
  [[ -n "$SOURCE_CONFIG" ]] && args+=(--source-config "$SOURCE_CONFIG")
  [[ -n "$PARALLEL" ]] && args+=(--parallel "$PARALLEL")
  [[ -n "$LIMIT" ]] && args+=(--limit "$LIMIT")
  [[ "$PATCH_RESULTS_FROM_OUTPUT" == "1" ]] && args+=(--patch-results-dir "$model_dir/results")
  if [[ "$SERVE_MODEL" == "1" ]]; then
    args+=(--serve-model)
  else
    args+=(--no-serve-model)
  fi
  args+=("${eval_args[@]}")

  echo "[suite] ===== $model_name ====="
  EVAL_OUT_DIR="$model_dir" bash "$HARNESS_DIR/evaluate.sh" "${args[@]}"
  ran=$((ran + 1))
done

[[ "$ran" -gt 0 ]] || { echo "[suite] No model configs matched."; exit 1; }
echo "[suite] Complete: $LOG_DIR"
