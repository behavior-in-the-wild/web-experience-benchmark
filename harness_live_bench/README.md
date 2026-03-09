# harness_live_bench

Benchmarks coding agents on **mirrored live web pages** (from `live_assets_eds/`).
Structurally mirrors `harness/` for static GitHub repos but adapted for real pages.

## Prerequisites

```bash
# From project root:
playwright install chromium
pip install playwright tqdm

# Copy and fill in .env
cp harness_live_bench/.env.example harness_live_bench/.env
```

## Dataset

`SAMPLE/input.jsonl` — one JSON line per page:

```json
{
  "id":        "<domain_slug>__<page_slug>",
  "domain":    "https://worldbank.org",
  "page_url":  "https://www.worldbank.org/ext/en/home",
  "mirror_dir":"worldbank.org/ext__en__home",
  "baseline":  { "desktop": { "lcp": 2776, "cls": 0.003, "inp": 88, "ttfb": 954 },
                 "mobile":  { "lcp": 3813, "cls": 0.034, "inp": 264, "ttfb": 1905 } }
}
```

Regenerate (after adding more mirrors with `fetch_live_assets.py`):

```bash
python3 scripts/live_pages_benchmark/build_input_jsonl.py \
  --jsonl  EDSSites_CWV_joined_top50_pages_top10.jsonl \
  --mirrors live_assets_eds \
  --output  harness_live_bench/SAMPLE/input.jsonl
```

## Run

```bash
# 1. Configure agents in evaluate.sh AGENTS=(...) array
# 2. Run
cd harness_live_bench
bash evaluate.sh [--limit N]
```

## How it works

```
input.jsonl
    │
    ▼ (for each page × agent)
    1. cp -r live_assets_eds/<mirror_dir>/ run_dir/repo/
    2. git init + baseline commit
    ─── Step 0: pre-agent synthetic CWV ───────────────────────────
    3. python -m http.server (pristine mirror)
    4. cwv_benchmark.py  → pre_mobile.json / pre_desktop.json
    5. Kill server
    ─── Agent run ──────────────────────────────────────────────────
    6. export PAGE_URL, DOMAIN, CWV_FIELD_MOBILE, CWV_FIELD_DESKTOP
              CWV_SYNTHETIC_MOBILE, CWV_SYNTHETIC_DESKTOP,
              LCP_ENTRIES_MOBILE, LCP_ENTRIES_DESKTOP
    7. bash agents/<agent>.sh repo/ tasks/optimize_cwv.txt …
    8. Normalize patch (apply git diff)
    ─── Post-agent measurement ─────────────────────────────────────
    9. python -m http.server (patched mirror)
   10. cwv_benchmark.py  → post_mobile.json / post_desktop.json
   11. Kill server; clean up
```

## Agent contract (identical to harness/)

```
bash agent.sh <REPO_DIR> <TASK_SPEC> <LOG_FILE> <PATCH_FILE>
```

The agent receives these env vars:

| Variable | Source |
|---|---|
| `CWV_FIELD_MOBILE` | CrUX real-user data (JSON) |
| `CWV_FIELD_DESKTOP` | CrUX real-user data (JSON) |
| `CWV_SYNTHETIC_MOBILE` | Lighthouse on local mirror, pre-agent (JSON) |
| `CWV_SYNTHETIC_DESKTOP` | Lighthouse on local mirror, pre-agent (JSON) |
| `LCP_ENTRIES_MOBILE` | LCP element details, pre-agent |
| `LCP_ENTRIES_DESKTOP` | LCP element details, pre-agent |
| `PAGE_URL` | Original live URL |
| `DOMAIN` | Domain name |

## Output structure

```
out/<timestamp>/results/
  <ID>_<agent>_pre_mobile.json      ← synthetic CWV before agent
  <ID>_<agent>_pre_desktop.json
  <ID>_<agent>_post_mobile.json     ← synthetic CWV after agent
  <ID>_<agent>_post_desktop.json
  <ID>_<agent>.patch                ← agent diff
  <ID>_<agent>_agent.log            ← full agent output
  <ID>_<agent>_plan.md              ← Phase 1 plan (Claude agents)
```

## Adding agents

Copy any `.sh` from `harness/agents/` into `harness_live_bench/agents/` — the contract is identical.
Then add it to the `AGENTS=()` array in `evaluate.sh`.
