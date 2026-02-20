# PSI Speed Insights Script

Standalone Python CLI that queries the **internal Adobe PSI service** to fetch Lighthouse-based PageSpeed Insights for any public URL, without touching the Google public API or requiring an API key.

**Script location:** `scripts/helper_scripts/psi_speed_insights.py`

---

## How It Works

The script sends an HTTP GET request to the internal PSI endpoint (`https://psi.experiencecloud.live`), which runs a full Lighthouse audit on the target URL and returns the same JSON structure as Google's PageSpeed Insights API. The script then parses the response and produces a formatted terminal report.

```
 ┌──────────┐       GET /?url=...&strategy=...&category=...       ┌─────────────────────────┐
 │  Script  │ ──────────────────────────────────────────────────▶ │  psi.experiencecloud.live│
 │  (CLI)   │ ◀────────────────────────────────────────────────── │  (Lighthouse runner)     │
 └──────────┘              JSON (lighthouseResult)                └─────────────────────────┘
       │
       ▼
  Parse & display:
  - Category scores (Performance, Accessibility, Best Practices, SEO)
  - Core Web Vitals (LCP, FCP, TBT, CLS, Speed Index, TTI)
  - LCP element details (snippet, selector, size)
  - Optimization opportunities with estimated savings
```

This is the same service that `psi-client.js` in the audit-worker uses. The Python implementation mirrors that pattern so it can be reused in the `cwv_optimizer` pipeline or any other Python tooling.

---

## Prerequisites

| Dependency | Version | Notes |
|---|---|---|
| Python | >= 3.10 | Uses `dict[]` / `X \| Y` type syntax |
| `requests` | any | HTTP client |
| `rich` | >= 13.0 | Terminal formatting (already in `pyproject.toml`) |

Install if needed:

```bash
pip install requests rich
```

No API key is required. The internal PSI service authenticates via the `Spacecat/1.0` User-Agent header.

---

## Input (CLI Arguments)

| Flag | Type | Default | Description |
|---|---|---|---|
| `-u`, `--url` | `str` | `https://someshsingh22.github.io` | Public URL to audit |
| `--strategy` | `mobile` \| `desktop` | `mobile` | Lighthouse device emulation strategy |
| `--both` | flag | off | Run both mobile and desktop in one invocation |
| `--categories` | `str[]` | `performance accessibility best-practices seo` | Which Lighthouse categories to request |
| `--json FILE` | `str` | none | Save the full raw JSON response to a file |
| `--raw` | flag | off | Dump raw JSON to stdout (for piping) |
| `-v`, `--verbose` | flag | off | Print request timing |

---

## Output

### Terminal Report (default)

The script prints four sections to the terminal using `rich` tables:

**1. Category Scores** -- Each Lighthouse category with a 0-100 score, color-coded:

| Color | Meaning |
|---|---|
| Green | 90-100 (Good) |
| Yellow | 50-89 (Needs work) |
| Red | 0-49 (Poor) |

**2. Core Metrics** -- Six key performance metrics with values and ratings:

| Metric | Good threshold | Poor threshold | Unit |
|---|---|---|---|
| Largest Contentful Paint (LCP) | <= 2500 | > 4000 | ms |
| First Contentful Paint (FCP) | <= 1800 | > 3000 | ms |
| Total Blocking Time (TBT) | <= 200 | > 600 | ms |
| Cumulative Layout Shift (CLS) | <= 0.1 | > 0.25 | score |
| Speed Index | <= 3400 | > 5800 | ms |
| Time to Interactive (TTI) | <= 3800 | > 7300 | ms |

**3. LCP Element** -- If detected, shows the DOM snippet, CSS selector, and bounding-box size of the element that triggered Largest Contentful Paint.

**4. Optimization Opportunities** -- Failing audits with estimated time/size savings. Covers 17 audit types including unused CSS/JS, render-blocking resources, unminified assets, image optimization, and more.

### JSON Output (`--json` / `--raw`)

When `--json report.json` is passed, the file contains the full raw Lighthouse JSON. Structure:

```
{
  "lighthouseResult": {
    "requestedUrl": "https://...",
    "finalDisplayedUrl": "https://...",
    "lighthouseVersion": "13.0.0",
    "fetchTime": "2026-02-20T...",
    "categories": {
      "performance": { "score": 0.68, ... },
      "accessibility": { "score": 0.98, ... },
      ...
    },
    "audits": {
      "largest-contentful-paint": { "numericValue": 10300, "displayValue": "10.3 s", ... },
      "first-contentful-paint": { ... },
      "unused-javascript": { "details": { "overallSavingsMs": 310, "overallSavingsBytes": 60300 }, ... },
      ...
    }
  }
}
```

When `--both` is used with `--json`, the file contains a top-level object keyed by strategy:

```
{
  "mobile": { "lighthouseResult": { ... } },
  "desktop": { "lighthouseResult": { ... } }
}
```

---

## Usage Examples

### Basic (mobile, default URL)

```bash
python scripts/helper_scripts/psi_speed_insights.py
```

### Audit a custom URL on desktop

```bash
python scripts/helper_scripts/psi_speed_insights.py -u https://www.adobe.com --strategy desktop
```

### Run both strategies and save JSON

```bash
python scripts/helper_scripts/psi_speed_insights.py --both --json report.json -v
```

### Only request performance category

```bash
python scripts/helper_scripts/psi_speed_insights.py --categories performance
```

### Pipe raw JSON to jq

```bash
python scripts/helper_scripts/psi_speed_insights.py --raw | jq '.lighthouseResult.categories.performance.score'
```

### Override the PSI endpoint (e.g., local or staging)

```bash
PSI_API_URL=http://localhost:8080 python scripts/helper_scripts/psi_speed_insights.py
```

---

## Using `run_psi()` as a Library

The core function is importable for use in other Python code:

```python
import sys
sys.path.insert(0, "scripts/helper_scripts")

from psi_speed_insights import run_psi, parse_result

raw = run_psi("https://someshsingh22.github.io", strategy="mobile")
result = parse_result(raw, "mobile")

print(f"Performance: {result.categories.get('Performance', 'N/A')}")
for metric_id, metric in result.metrics.items():
    print(f"  {metric['title']}: {metric['display']} ({metric['rating']})")
```

Key functions:

| Function | Signature | Returns |
|---|---|---|
| `run_psi` | `(url, strategy="mobile", categories=None) -> dict` | Raw JSON from PSI API |
| `parse_result` | `(raw_json, strategy) -> PSIResult` | Structured dataclass with scores, metrics, opportunities |
| `print_report` | `(PSIResult) -> None` | Pretty-prints to terminal |

---

## Configuration

All defaults are set at the top of the script as module-level constants:

| Constant | Value | Purpose |
|---|---|---|
| `PSI_BASE_URL` | `https://psi.experiencecloud.live` | Internal PSI endpoint (overridable via `PSI_API_URL` env var) |
| `DEFAULT_URL` | `https://someshsingh22.github.io` | Target URL when `-u` is omitted |
| `DEFAULT_STRATEGY` | `mobile` | Default device strategy |
| `DEFAULT_CATEGORIES` | `[performance, accessibility, best-practices, seo]` | All four Lighthouse categories |
| `REQUEST_TIMEOUT` | `120` | Seconds before the HTTP request times out |
| `USER_AGENT` | `Spacecat/1.0` | Required header for the internal PSI service |

---

## Relationship to Other Tools in This Repo

| Tool | Language | What it does | How PSI is called |
|---|---|---|---|
| `cwv-agent/src/tools/psi.js` | Node.js | Full CWV agent with caching, summarization, LLM integration | Google PSI npm package (`psi` 4.1.0) with API key |
| `scripts/helper_scripts/web_vitals.py` | Python | Playwright-based local CWV measurement (LCP, CLS, FID, INP, TTFB) | Does not use PSI; measures directly in browser |
| **`scripts/helper_scripts/psi_speed_insights.py`** | **Python** | **Standalone PSI audit via internal service** | **HTTP GET to `psi.experiencecloud.live`** |

This script fills the gap: a lightweight Python entry point to the same internal PSI infrastructure that the Node.js audit-worker uses, without requiring a Google API key or the `psi` npm package.
