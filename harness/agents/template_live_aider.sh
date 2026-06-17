#!/usr/bin/env bash
set -euo pipefail

# Route all temp files to /dev/shm (overlay /tmp is small; /dev/shm has ~1 TB free)
export TMPDIR="${BENCH_TMPDIR:-/dev/shm}"

# ============================================================
# Aider agent for harness
# Two-phase: Plan (read-only, write plan.md) → Execute
#
# Receives from evaluate.sh:
#   CWV_FIELD_MOBILE, CWV_FIELD_DESKTOP       — real-user CrUX data
#   CWV_SYNTHETIC_MOBILE, CWV_SYNTHETIC_DESKTOP — pre-agent Lighthouse on local mirror
#   LCP_ENTRIES_MOBILE, LCP_ENTRIES_DESKTOP
#   PAGE_URL, DOMAIN
# ============================================================

REPO_DIR="$1"
TASK_SPEC="$2"
LOG="$3"
PATCH_FILE="${4:-/dev/null}"

PAGE_URL="${PAGE_URL:-unknown}"
DOMAIN="${DOMAIN:-unknown}"

# Dual CWV context (set by evaluate.sh)
CWV_FIELD_MOBILE="${CWV_FIELD_MOBILE:-null}"
CWV_FIELD_DESKTOP="${CWV_FIELD_DESKTOP:-null}"
CWV_SYNTHETIC_MOBILE="${CWV_SYNTHETIC_MOBILE:-null}"
CWV_SYNTHETIC_DESKTOP="${CWV_SYNTHETIC_DESKTOP:-null}"
LCP_ENTRIES_DESKTOP="${LCP_ENTRIES_DESKTOP:-null}"
LCP_ENTRIES_MOBILE="${LCP_ENTRIES_MOBILE:-null}"

AIDER_MODEL="${AIDER_MODEL:-azure/gpt-5}"

mkdir -p "$(dirname "$LOG")"
cd "$REPO_DIR"

# Clean any leftover aider artefacts
git reset --hard HEAD 2>/dev/null || true
git clean -fd
rm -f .aider* 2>/dev/null || true
rm -rf .aider.tags.cache* 2>/dev/null || true

echo "[agent_aider] Starting live-page aider agent" > "$LOG"
echo "[agent_aider] PAGE_URL=$PAGE_URL DOMAIN=$DOMAIN" >> "$LOG"

# ── Write cwv_context.json so aider can read it as a --read file ──────────────
CWV_CONTEXT_FILE="$REPO_DIR/cwv_context.json"
python3 - <<PYSCRIPT > "$CWV_CONTEXT_FILE"
import json
def load_json_var(s):
    try:
        return json.loads(s)
    except Exception:
        return None

ctx = {
    "page_url":   "$PAGE_URL",
    "domain":     "$DOMAIN",
    "field_cwv": {
        "note":    "Real-user data from Google CrUX (live page traffic)",
        "mobile":  load_json_var("""$CWV_FIELD_MOBILE"""),
        "desktop": load_json_var("""$CWV_FIELD_DESKTOP"""),
    },
    "synthetic_cwv": {
        "note":    "Lighthouse measured on this local mirror just before the agent ran",
        "mobile":  load_json_var("""$CWV_SYNTHETIC_MOBILE"""),
        "desktop": load_json_var("""$CWV_SYNTHETIC_DESKTOP"""),
        "lcp_entries_mobile":  load_json_var("""$LCP_ENTRIES_MOBILE"""),
        "lcp_entries_desktop": load_json_var("""$LCP_ENTRIES_DESKTOP"""),
    },
}
print(json.dumps(ctx, indent=2))
PYSCRIPT
echo "[agent_aider] cwv_context.json written" >> "$LOG"

PLAN_FILE="$REPO_DIR/plan.md"
PLAN_PROMPT="$(mktemp)"
EXEC_PROMPT="$(mktemp)"
trap 'rm -f "$PLAN_PROMPT" "$EXEC_PROMPT"' EXIT

touch "$PLAN_FILE"

# Load specific suggestion if running inside suggestions eval pipeline
SUGGESTION_CONTENT=""
if [[ -n "${EVAL_SUGGESTION_FILE:-}" && -f "${EVAL_SUGGESTION_FILE}" ]]; then
  SUGGESTION_CONTENT="$(python3 -c "
import json, sys
d = json.load(open('$EVAL_SUGGESTION_FILE'))
print('Title:       ' + d.get('title',''))
print('Metric:      ' + d.get('metric',''))
print('Priority:    ' + d.get('priority',''))
print('Description: ' + d.get('description',''))
print('Implementation: ' + d.get('implementation',''))
" 2>/dev/null || cat "$EVAL_SUGGESTION_FILE")"
  echo "[agent_aider] Loaded suggestion from EVAL_SUGGESTION_FILE" >> "$LOG"
fi

# ============================================================
# PHASE 1 — Planning (write ONLY to plan.md)
# ============================================================
cat > "$PLAN_PROMPT" <<EOF
You are a web performance analyst optimizing a mirrored live web page.

Page URL: $PAGE_URL
Domain:   $DOMAIN

You have access to cwv_context.json (read-only) which contains two CWV baselines:

1. **Field CWV** (Google CrUX — real user data from the live site):
   Mobile:  $CWV_FIELD_MOBILE
   Desktop: $CWV_FIELD_DESKTOP

2. **Synthetic CWV** (Lighthouse on this LOCAL mirror, measured moments ago):
   Mobile:  $CWV_SYNTHETIC_MOBILE
   Desktop: $CWV_SYNTHETIC_DESKTOP

Your changes will be benchmarked using the SAME Lighthouse method — prioritize
improvements that Lighthouse can detect locally (LCP element loading, CLS from
unsized images, render-blocking JS/CSS).

Read cwv_context.json for full LCP element detail and lcp_entries.
Read the repository files to understand the structure.

$(if [[ -n "$SUGGESTION_CONTENT" ]]; then
  printf '## Target Suggestion to Implement\n\n%s\n\nYour PRIMARY goal is to implement the suggestion above.\n' "$SUGGESTION_CONTENT"
fi)

IMPORTANT: You MUST always write plan.md regardless of whether metrics look good or bad.
Even if scores appear healthy, there are always further optimizations possible.

Write to plan.md:
1. **Baseline Analysis**: Key metrics issues identified (cite file paths)
2. **Files to Modify**: Full list of files that need changes
3. **Proposed Changes**: For each file — what to change and why it improves CWV
4. **Expected Impact**: Estimated LCP / CLS / INP improvement per device

Rules for this phase:
- Write ONLY to plan.md
- Do NOT modify any other file
- Do NOT write actual code — describe changes in plain English
- Do NOT ask the user questions; proceed autonomously with your best judgment
EOF

echo "[agent_aider] Phase 1: Planning..." >> "$LOG"

if ! aider \
  --yes-always \
  --no-auto-commits \
  --no-pretty \
  --no-stream \
  --no-show-model-warnings \
  --no-suggest-shell-commands \
  --no-detect-urls \
  --no-gitignore \
  --architect \
  --model "$AIDER_MODEL" \
  --editor-model "$AIDER_MODEL" \
  --read "$CWV_CONTEXT_FILE" \
  --message-file "$PLAN_PROMPT" \
  >> "$LOG" 2>&1; then
    echo "[agent_aider] Phase 1 failed" >> "$LOG"
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    exit 0
fi

# Verify plan.md was written and only plan.md was touched
if [[ ! -s "$PLAN_FILE" ]]; then
  echo "[agent_aider] ERROR: plan.md empty or missing — aborting" >> "$LOG"
  git reset --hard HEAD 2>/dev/null || true
  git clean -fd
  rm -f .aider* 2>/dev/null || true
  rm -rf .aider.tags.cache* 2>/dev/null || true
  exit 0
fi

MODIFIED_FILES="$(git diff --name-only 2>/dev/null || true)"
if [[ -n "$MODIFIED_FILES" ]]; then
  NON_PLAN="$(echo "$MODIFIED_FILES" | grep -v '^plan\.md$' | grep -v '^cwv_context\.json$' || true)"
  if [[ -n "$NON_PLAN" ]]; then
    echo "[agent_aider] ERROR: Phase 1 modified non-plan files:" >> "$LOG"
    echo "$NON_PLAN" >> "$LOG"
    echo "[agent_aider] Aborting." >> "$LOG"
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    exit 0
  fi
fi

PLAN_CONTENT="$(cat "$PLAN_FILE")"
echo "[agent_aider] Phase 1 complete. Plan: $(echo "$PLAN_CONTENT" | wc -l) lines" >> "$LOG"
echo "========== PLAN START ==========" >> "$LOG"
echo "$PLAN_CONTENT" >> "$LOG"
echo "========== PLAN END ==========" >> "$LOG"

# Reset before Phase 2 (keep plan content in memory; cwv_context.json stays on disk)
git reset --hard HEAD 2>/dev/null || true
git clean -fd
rm -f .aider* 2>/dev/null || true
rm -rf .aider.tags.cache* 2>/dev/null || true

# ============================================================
# PHASE 2 — Execution (implement the plan)
# ============================================================
cat > "$EXEC_PROMPT" <<EOF
You are an expert web performance engineer optimizing a mirrored live web page.

Page: $PAGE_URL
Domain: $DOMAIN

Dual CWV baseline for reference:
  Field CWV mobile (real users):       $CWV_FIELD_MOBILE
  Field CWV desktop (real users):      $CWV_FIELD_DESKTOP
  Synthetic CWV mobile (Lighthouse):   $CWV_SYNTHETIC_MOBILE
  Synthetic CWV desktop (Lighthouse):  $CWV_SYNTHETIC_DESKTOP

You created this optimization plan. Now execute it precisely:

=== PLAN ===
$PLAN_CONTENT
============

Rules:
- Edit ONLY existing files in this repository
- Do NOT add a build system, bundler, or new external dependencies
- Do NOT change visible content or page layout
- Do NOT edit cwv_context.json
- Asset paths must remain valid (already rewritten to relative paths)

Implement all changes from the plan now.
EOF

echo "[agent_aider] Phase 2: Executing plan..." >> "$LOG"

if aider \
  --yes-always \
  --no-auto-commits \
  --no-pretty \
  --no-stream \
  --no-show-model-warnings \
  --no-suggest-shell-commands \
  --no-detect-urls \
  --no-gitignore \
  --architect \
  --model "$AIDER_MODEL" \
  --editor-model "$AIDER_MODEL" \
  --weak-model "$AIDER_MODEL" \
  --message-file "$EXEC_PROMPT" \
  >> "$LOG" 2>&1; then

    # Remove planning/context artefacts before patch capture
    rm -f "$PLAN_FILE" "$CWV_CONTEXT_FILE"
    echo "[agent_aider] Removed plan.md + cwv_context.json before patch capture" >> "$LOG"

    git diff > "$PATCH_FILE"
    echo "[agent_aider] Patch: $(wc -l < "$PATCH_FILE") lines" >> "$LOG"
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    echo "[agent_aider] Done" >> "$LOG"
    exit 0
else
    echo "[agent_aider] Phase 2 failed" >> "$LOG"
    git reset --hard HEAD 2>/dev/null || true
    git clean -fd
    rm -f .aider* 2>/dev/null || true
    rm -rf .aider.tags.cache* 2>/dev/null || true
    exit 0
fi
