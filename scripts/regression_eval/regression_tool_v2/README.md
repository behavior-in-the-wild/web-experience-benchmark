# Webpage Comparison Tool

A visual HTML comparison tool that takes two webpages (by local file path or public URL), renders them, and produces a self-contained HTML report showing matched sections and leaf elements side-by-side with IoU scores.

## How It Works

1. **Fetches/loads** both the original and generated HTML pages (via Playwright for URLs).
2. **Captures screenshots** and extracts element bounding boxes for all DOM nodes.
3. **Detects sections** (large structural blocks) and **leaf elements** (text, images) in both pages.
4. **Matches** sections and leaves across the two pages using one or more matching strategies (heuristic, embedding, or VLM).
5. **Generates a report** — a self-contained HTML file with three columns:
   - Column 1: Original page (live preview + editable source)
   - Column 2: Generated page (live preview + editable source)
   - Column 3: Matched diff list with IoU scores; hover to highlight elements

## Requirements

- Python 3.10+
- Playwright (`playwright install chromium`)
- Dependencies: `lxml`, `html5lib`, `nest_asyncio`, `numpy`, `easyocr`, `Pillow`

## Usage

### Compare two public URLs

```bash
python3 create_comparison_report.py \
  --output-dir /path/to/output \
  --original-url "https://example.com/page-v1" \
  --generated-url "https://example.com/page-v2" \
  --matching-types heuristic
```

### Compare two local HTML files

```bash
python3 create_comparison_report.py \
  --output-dir /path/to/output \
  --original-html-file-path /path/to/original.html \
  --generated-html-file-path /path/to/generated.html \
  --matching-types heuristic
```

### Re-run on a previously prepared output directory

If the output directory already contains `original_analysis/` and `output.html` from a prior run, you can skip re-fetching and just re-run matching:

```bash
python3 create_comparison_report.py \
  --output-dir /path/to/output \
  --matching-types heuristic
```

## Options

| Option | Default | Description |
|---|---|---|
| `--output-dir` | *(required)* | Directory for all outputs (screenshots, analysis, report) |
| `--original-url` | — | Public URL of the original page (fetched via Playwright) |
| `--generated-url` | — | Public URL of the generated page (fetched via Playwright) |
| `--original-html-file-path` | — | Path to a local original HTML file |
| `--generated-html-file-path` | — | Path to a local generated HTML file |
| `--matching-types` | `heuristic embedding vlm` | One or more of `heuristic`, `embedding`, `vlm` |
| `--thresh-height` | `2900` | Max pixel height for a single section (taller pages get split) |
| `--max-leaves` | `None` | Cap on number of leaves per section (for performance) |
| `--run-llm-diffs` | off | Run LLM-based diff generation (populates the LLM Diffs tab) |
| `--ai-provider` | `gpt41` | AI provider for LLM diffs |
| `--use-fragment-sectioning` | off | Use fragment-based section detection instead of the default |

URL inputs take precedence over file path inputs if both are provided.

## Output

All outputs are written to `--output-dir`:

```
output-dir/
  original_analysis/
    original.html          # fetched/copied original page
    full_page.png          # full-page screenshot
    <element>.json         # computed styles per element
    <element>.bbox.json    # bounding boxes per element
    ...
  generated_analysis/
    <same structure as original_analysis>
  report_heuristic.html    # self-contained comparison report
  report_embedding.html    # (if embedding matching was run)
  report_vlm.html          # (if VLM matching was run)
```

Open any `report_*.html` file in a browser to view the comparison.

## Example

```bash
python3 create_comparison_report.py \
  --output-dir /tmp/py_docs_comparison \
  --original-url "https://docs.python.org/3.11/" \
  --generated-url "https://docs.python.org/3.12/" \
  --matching-types heuristic
```

Then open `/tmp/py_docs_comparison/report_heuristic.html` in a browser.
