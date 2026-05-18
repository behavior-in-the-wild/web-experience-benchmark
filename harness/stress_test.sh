#!/usr/bin/env bash
# =============================================================================
# Stress test: agent + visual only, diverse frameworks, multiple agent backends
#
# Runs 10 rows (one per framework) × 2 agents (OpenCode GPT-5, Aider GPT-5)
# = 20 jobs at parallel=10. Skips all CWV/PSI measurement; keeps agent run
# and visual regression check so outputs can be validated.
#
# Usage:
#   ./harness/stress_test.sh [-- evaluate.sh extra args...]
#
# Required env (from .env or shell):
#   AZURE_OPENAI_API_KEY
#   AZURE_OPENAI_ENDPOINT
#
# Optional overrides:
#   STRESS_MODEL      Azure deployment name (default: gpt-5)
#   STRESS_PARALLEL   Concurrency (default: 10)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Forward any trailing args to evaluate.sh ─────────────────────────────────
extra_args=()
if [[ $# -gt 0 && "$1" == "--" ]]; then
  shift; extra_args=("$@")
elif [[ $# -gt 0 ]]; then
  extra_args=("$@")
fi

# ── Config ────────────────────────────────────────────────────────────────────
STRESS_MODEL="${STRESS_MODEL:-gpt-5}"
STRESS_PARALLEL="${STRESS_PARALLEL:-10}"
INPUT_CSV="$SCRIPT_DIR/SAMPLE/input.csv"

# Agents: OpenCode (GPT-5 via Azure) + Aider (GPT-5 via Azure)
# Comma-separated — evaluate.sh splits on commas.
AGENTS="agents/template_opencodegpt51codex.sh,agents/template_aider.sh"

# ── Checks ────────────────────────────────────────────────────────────────────
[[ -f "$INPUT_CSV" ]] || { echo "Missing input CSV: $INPUT_CSV"; exit 1; }
[[ -f "$SCRIPT_DIR/evaluate.sh" ]] || { echo "Missing evaluate.sh"; exit 1; }

if [[ -z "${AZURE_OPENAI_API_KEY:-}" || -z "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
  echo "ERROR: AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set."
  exit 1
fi

# ── Build stress CSV: one row per framework ───────────────────────────────────
STRESS_CSV="$(mktemp /tmp/stress.XXXXXX)"
python3 - "$INPUT_CSV" "$STRESS_CSV" << 'PY'
import csv, sys

src, dst = sys.argv[1], sys.argv[2]

# Frameworks to include — one row each for diversity
TARGET_FRAMEWORKS = {
    "Express", "Hexo", "Hugo", "Jekyll", "Next.js",
    "Pelican", "Quarto", "React", "Static HTML", "Vue",
}

with open(src, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    rows_by_fw: dict[str, dict] = {}
    for row in reader:
        fw = row.get("FRAMEWORK", "")
        if fw in TARGET_FRAMEWORKS and fw not in rows_by_fw:
            rows_by_fw[fw] = row

with open(dst, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    # Write in a stable order matching TARGET_FRAMEWORKS list
    for fw in sorted(TARGET_FRAMEWORKS):
        if fw in rows_by_fw:
            writer.writerow(rows_by_fw[fw])

picked = sorted(rows_by_fw.keys())
print(f"[stress] Picked {len(picked)} rows: {', '.join(picked)}")
PY

echo "[stress] Model:    azure/$STRESS_MODEL"
echo "[stress] Agents:   $AGENTS"
echo "[stress] Parallel: $STRESS_PARALLEL"
echo "[stress] CSV:      $STRESS_CSV"
echo "[stress] Jobs:     $(python3 -c "
import csv
with open('$STRESS_CSV') as f:
    n = sum(1 for _ in csv.DictReader(f))
agents = len('$AGENTS'.split(','))
print(f'{n} rows × {agents} agents = {n*agents} total jobs')
")"
echo ""

# ── Run evaluate.sh ───────────────────────────────────────────────────────────
# Skip CWV + both PSI phases; visual regression check runs (bore + visual_validate.py).
# Model is forced via OPENCODE_MODEL and AIDER_MODEL env overrides.
CSV="$STRESS_CSV" \
EVAL_AGENTS="$AGENTS" \
OPENCODE_MODEL="azure/$STRESS_MODEL" \
AZURE_OPENAI_API_DEPLOYMENT_NAME="$STRESS_MODEL" \
AIDER_MODEL="azure/$STRESS_MODEL" \
  bash "$SCRIPT_DIR/evaluate.sh" \
    --parallel "$STRESS_PARALLEL" \
    --skip-init-psi \
    --skip-final-psi \
    --skip-cwv \
    "${extra_args[@]}"

echo "[stress] Done."
