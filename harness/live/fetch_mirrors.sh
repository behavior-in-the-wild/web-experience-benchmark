#!/usr/bin/env bash
# Fetch mirrors for all URLs in live_filtered_top3.jsonl using fetch_live_assets.py.
# Writes to live_assets_eds/<domain_slug>/<page_slug>/ (Playwright-based mirror).
#
# Usage:
#   bash harness/live/fetch_mirrors.sh [--workers N] [--limit N]
set -euo pipefail

LIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$(cd "$LIVE_DIR/.." && pwd)"
REPO_ROOT="$(cd "$HARNESS/.." && pwd)"
SCRIPTS="$REPO_ROOT/scripts/live_pages_benchmark"

JSONL="${JSONL:-$HARNESS/SAMPLE/live_filtered_top3.jsonl}"
MIRRORS_ROOT="${MIRRORS_ROOT:-$REPO_ROOT/live_assets_eds}"
WORKERS="${WORKERS:-3}"
LIMIT="${LIMIT:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers) shift; WORKERS="$1"; shift ;;
    --limit)   shift; LIMIT="$1";   shift ;;
    --jsonl)   shift; JSONL="$1";   shift ;;
    --mirrors) shift; MIRRORS_ROOT="$1"; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

[[ -f "$JSONL" ]]                              || { echo "Missing JSONL: $JSONL"; exit 1; }
[[ -f "$SCRIPTS/fetch_live_assets.py" ]]       || { echo "Missing fetch_live_assets.py"; exit 1; }
[[ "$MIRRORS_ROOT" = /* ]] || MIRRORS_ROOT="$(cd "$(dirname "$MIRRORS_ROOT")" && pwd)/$(basename "$MIRRORS_ROOT")"

# Activate venv (playwright must be installed)
[[ -f "$REPO_ROOT/.venv/bin/activate" ]] && source "$REPO_ROOT/.venv/bin/activate"

mkdir -p "$MIRRORS_ROOT"

# Convert live_filtered_top3.jsonl → EDSSites-compatible JSONL
# (fetch_live_assets.py expects: {"domain": "https://...", "cwv_top10_pages": [{"url": "..."}]})
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
INPUT_JSONL="$WORK_DIR/fetch_input.jsonl"

python3 - "$JSONL" "$INPUT_JSONL" "${LIMIT:-}" << 'PY'
import json, sys
from urllib.parse import urlparse

src, dst, limit_s = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
limit = int(limit_s) if limit_s else None

# Group pages by domain (netloc without www)
from collections import defaultdict
domain_pages = defaultdict(list)
seen_urls = set()
n = 0

with open(src, encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        # Get URL from JSONL
        url = d.get('input', {}).get('url', '') or d.get('url', '') or \
              d.get('analysis_result', {}).get('url', '')
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        parsed = urlparse(url)
        host = parsed.netloc or parsed.path
        if host.startswith("www."):
            host = host[4:]
        domain_key = f"https://{host}"
        domain_pages[domain_key].append({"url": url})

        n += 1
        if limit and n >= limit:
            break

with open(dst, 'w', encoding='utf-8') as f:
    for domain, pages in domain_pages.items():
        f.write(json.dumps({"domain": domain, "cwv_top10_pages": pages}) + "\n")

total_pages = sum(len(v) for v in domain_pages.values())
print(f"[fetch-mirrors] {len(domain_pages)} domains, {total_pages} pages → {dst}")
PY

echo "[fetch-mirrors] Input JSONL:   $JSONL"
echo "[fetch-mirrors] Mirrors root:  $MIRRORS_ROOT"
echo "[fetch-mirrors] Workers:       $WORKERS"
[[ -n "$LIMIT" ]] && echo "[fetch-mirrors] LIMIT=$LIMIT"
echo ""

python3 "$SCRIPTS/fetch_live_assets.py" \
  --input   "$INPUT_JSONL" \
  --output  "$MIRRORS_ROOT" \
  --workers "$WORKERS"

echo ""
echo "[fetch-mirrors] Done. Mirrors written to: $MIRRORS_ROOT"
echo "[fetch-mirrors] Directories created:"
ls "$MIRRORS_ROOT" | grep -v "^nasm.org$" | head -20 || true
echo "  nasm.org/ (test mirror, pre-existing)"
