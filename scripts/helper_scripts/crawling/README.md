# Crawling + CWV benchmarking helpers

This folder contains small utilities to collect page URLs and run Core Web Vitals (CWV)
benchmarks for every page.

- `fetch_sitemap.py` — fetches a `sitemap.xml` and writes `urls.txt` (one URL per line).
- `crawl_and_benchmark.py` — reads a newline-delimited URLs file and measures CWV for each URL using the project's `measure_cwv_metrics` function.

Quick start:

1. Fetch a sitemap:

```bash
python scripts/helper_scripts/crawling/fetch_sitemap.py --sitemap https://example.com/sitemap.xml --out urls.txt
```

2. Run the benchmark (3 runs per page, 4 concurrent pages):

```bash
pip install -e .
pip install playwright
playwright install
python scripts/helper_scripts/crawling/crawl_and_benchmark.py --urls-file urls.txt --device mobile --num-runs 3 --concurrency 4 --out results.jsonl
```

Notes:
- These scripts reuse the project's CWV measurement code and do not invoke any LLM/agent.
- For large sites, reduce concurrency or run in batches to avoid resource exhaustion.
