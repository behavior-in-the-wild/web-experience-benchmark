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

---

## Screenshot Diff (Fast Pixel-Level Diff)

`screenshot_diff.py` is a lightweight alternative that skips DOM analysis entirely. It renders both HTML pages, computes a pixel-level diff using a tile-based alignment algorithm, and outputs annotated screenshots and a self-contained HTML report — typically in seconds rather than minutes.

### How It Works

1. **Renders** both HTML inputs to full-page PNGs via Playwright (supports local files and URLs).
2. **Computes a tile grid diff** — tries three alignment strategies (same-size, width-normalised, symmetric LANCZOS) and picks the one with the fewest flagged tiles. Automatically retries with Gaussian blur if more than 60% of the image is flagged (suppresses sub-pixel aliasing noise).
3. **Post-processes** the grid: dilates small components for context, filters 1-tile-wide noise stripes.
4. **Annotates** the original screenshot with coloured bounding boxes around changed regions, using a hue that is most absent from the image.
5. **Stitches** crops of all changed regions into a single image for quick review.
6. **Saves** a self-contained `diff_report.html` with the annotated screenshots, crop stitch, and a bounding-box table (pixel coords + percentages).

### CLI Usage

```bash
python screenshot_diff.py \
  --html-a samples/page_a.html \
  --html-b samples/page_b.html \
  --output-dir /path/to/output
```

Works with URLs too:

```bash
python screenshot_diff.py \
  --html-a "https://example.com/v1" \
  --html-b "https://example.com/v2" \
  --output-dir /tmp/diff_out \
  --viewport-width 1440
```

### Options

| Option | Default | Description |
|---|---|---|
| `--html-a` | *(required)* | Original HTML file path or URL |
| `--html-b` | *(required)* | Generated HTML file path or URL |
| `--output-dir` | *(required)* | Directory to write outputs |
| `--viewport-width` | `1280` | Browser viewport width in CSS px |

### Output

```
output-dir/
  orig.png              # raw full-page screenshot of html-a
  gen.png               # raw full-page screenshot of html-b
  annotated_orig.png    # original with coloured diff bounding boxes
  diff_crops.png        # stitched crops of each changed region
  diff_report.html      # self-contained HTML viewer (open in browser)
```

### Sample Run

```
$ python screenshot_diff.py \
    --html-a samples/page_a.html \
    --html-b samples/page_b.html \
    --output-dir samples/diff_output

[sdiff:rendering_orig]
[sdiff:rendering_gen]
[sdiff:computing_diff]
[sdiff:building_report]
[sdiff:done] {"bbox_count": 1, "bbox_fraction": 0.4111}
Diff complete: 1 changed regions (41.1% of image area).
Report: samples/diff_output/diff_report.html
```

### Webapp

The Flask webapp (`webapp/app.py`) exposes a **Screenshot Diff** button on the same input form. Upload two HTML files (or enter URLs) and click **Screenshot Diff** — the result opens as a diff report in a new tab without running the full DOM-analysis pipeline.

Relevant routes:
- `POST /compare_screenshot` — start a screenshot diff job
- `GET /screenshot_events/<job_id>` — SSE stream for live progress
- `GET /view_screenshot/<job_id>/report` — serve the completed report

### When to Use Each Mode

| | Full Report (`create_comparison_report.py`) | Screenshot Diff (`screenshot_diff.py`) |
|---|---|---|
| Speed | Minutes | Seconds |
| DOM-level matches | Yes (leaf + section IoU) | No |
| Pixel-level diff | No | Yes |
| Works on live URLs | Yes | Yes |
| Best for | Structural / layout validation | Quick visual regression check |
