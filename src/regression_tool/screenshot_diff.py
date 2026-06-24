"""Screenshot-only visual diff between two HTML pages.

Renders each HTML input with Playwright, then applies the pixel-level diff
algorithm from diff.py to detect changed regions. An annotated screenshot,
stitch of changed crops, and a self-contained HTML viewer are written to
output_dir.

This is a fast alternative to the full DOM-analysis pipeline for cases where
only the visual delta matters.

Progress lines written to stdout use the prefix ``[sdiff:<stage>]`` so
callers (e.g. the Flask webapp) can parse them for SSE events.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from diff import (
    annotate_image_with_bboxes,
    bboxes_from_grid,
    component_bbox_fraction,
    compute_diff_grid,
    pick_box_color,
    stitch_region_crops,
)

_MAX_PAGE_HEIGHT_PX = 15_000


def _render_to_png(path_or_url: str, viewport_width: int = 1280) -> bytes:
    """Render an HTML file path or URL to a full-page PNG via Playwright."""
    from playwright.sync_api import sync_playwright

    is_url = path_or_url.startswith("http://") or path_or_url.startswith("https://")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": viewport_width, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        if is_url:
            page.goto(path_or_url, wait_until="networkidle")
        else:
            html_path = Path(path_or_url)
            companion_suffixes = ("_files", " Files", "_bestanden", "_fichiers", "_archivos")
            has_companion = any(
                (html_path.parent / (html_path.stem + sfx)).is_dir()
                for sfx in companion_suffixes
            )
            if has_companion:
                page.goto(html_path.as_uri(), wait_until="domcontentloaded")
            else:
                page.set_content(html_path.read_text(encoding="utf-8"))
                page.wait_for_load_state("domcontentloaded")

        try:
            page.evaluate("() => document.fonts.ready", timeout=15_000)
        except Exception:
            pass

        dims = page.evaluate(
            "() => ({height: document.documentElement.scrollHeight})"
        )
        if dims["height"] > _MAX_PAGE_HEIGHT_PX:
            page.set_viewport_size({"width": viewport_width, "height": _MAX_PAGE_HEIGHT_PX})

        png_bytes = page.screenshot(full_page=True, timeout=120_000)
        browser.close()

    return png_bytes


def run_screenshot_diff(
    html_a: str,
    html_b: str,
    output_dir: str,
    viewport_width: int = 1280,
    progress_cb=None,
) -> dict:
    """Render both HTML inputs, compute the visual diff, and save results.

    progress_cb(stage: str) is called at each major step if provided.

    Writes to output_dir:
      orig.png              — raw full-page screenshot of html_a
      gen.png               — raw full-page screenshot of html_b
      annotated_orig.png    — original with diff bboxes overlaid
      diff_crops.png        — stitched crops of changed regions (if any diffs)
      diff_report.html      — self-contained HTML viewer

    Returns a dict with keys: bbox_count, bbox_fraction, output_dir,
    orig_png, gen_png, annotated_orig_png, diff_crops_png, diff_report_html.
    """
    def _cb(stage: str) -> None:
        if progress_cb:
            progress_cb(stage)

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    _cb("rendering_orig")
    orig_bytes = _render_to_png(html_a, viewport_width)
    orig_path = out / "orig.png"
    orig_path.write_bytes(orig_bytes)

    _cb("rendering_gen")
    gen_bytes = _render_to_png(html_b, viewport_width)
    gen_path = out / "gen.png"
    gen_path.write_bytes(gen_bytes)

    _cb("computing_diff")
    grid, w, h = compute_diff_grid(orig_path, gen_path)
    bboxes = bboxes_from_grid(grid, w, h)
    frac = component_bbox_fraction(bboxes, w, h) if bboxes else 0.0
    if frac > 0.6:
        grid, w, h = compute_diff_grid(orig_path, gen_path, blur_radius=1)
        bboxes = bboxes_from_grid(grid, w, h)
        frac = component_bbox_fraction(bboxes, w, h) if bboxes else 0.0

    orig_img = Image.open(orig_path).convert("RGB")
    gen_img = Image.open(gen_path).convert("RGB")
    color = pick_box_color(np.asarray(orig_img))

    annotated_orig_bytes = annotate_image_with_bboxes(orig_img, bboxes, color)
    ann_orig_path = out / "annotated_orig.png"
    ann_orig_path.write_bytes(annotated_orig_bytes)

    crops_path: Optional[Path] = None
    if bboxes:
        crops_bytes = stitch_region_crops(orig_img, bboxes)
        crops_path = out / "diff_crops.png"
        crops_path.write_bytes(crops_bytes)

    _cb("building_report")

    def _b64(p: Path) -> str:
        return base64.b64encode(p.read_bytes()).decode()

    report_html = _build_diff_report(
        orig_b64=_b64(ann_orig_path),
        gen_b64=base64.b64encode(gen_bytes).decode(),
        crops_b64=_b64(crops_path) if crops_path else None,
        bboxes=bboxes,
        bbox_fraction=frac,
        w=w,
        h=h,
    )
    report_path = out / "diff_report.html"
    report_path.write_text(report_html, encoding="utf-8")

    return {
        "bbox_count": len(bboxes),
        "bbox_fraction": round(frac, 4),
        "output_dir": str(out),
        "orig_png": str(orig_path),
        "gen_png": str(gen_path),
        "annotated_orig_png": str(ann_orig_path),
        "diff_crops_png": str(crops_path) if crops_path else None,
        "diff_report_html": str(report_path),
    }


def _build_diff_report(
    orig_b64: str,
    gen_b64: str,
    crops_b64: Optional[str],
    bboxes: list[dict],
    bbox_fraction: float,
    w: int,
    h: int,
) -> str:
    """Return a self-contained HTML string showing the diff results."""
    n = len(bboxes)
    pct = round(bbox_fraction * 100, 1)

    crops_section = ""
    if crops_b64:
        crops_section = f"""
  <div class="section">
    <h2>Changed Regions ({n})</h2>
    <img src="data:image/png;base64,{crops_b64}" style="max-width:100%;border-radius:8px;border:1px solid #2e3244;">
  </div>"""

    bbox_rows = "".join(
        f"<tr><td>{i + 1}</td><td>{b['x']}</td><td>{b['y']}</td>"
        f"<td>{b['w']}</td><td>{b['h']}</td>"
        f"<td>{b['x_pct']}%</td><td>{b['y_pct']}%</td>"
        f"<td>{b['w_pct']}%</td><td>{b['h_pct']}%</td></tr>"
        for i, b in enumerate(bboxes)
    )

    no_diff_msg = (
        '<p style="color:#8b8fa3;padding:14px 0;font-size:.88rem;">'
        "No visual differences detected.</p>"
    )
    bbox_table = (
        f"<table><thead><tr>"
        f"<th>#</th><th>x</th><th>y</th><th>w</th><th>h</th>"
        f"<th>x%</th><th>y%</th><th>w%</th><th>h%</th>"
        f"</tr></thead><tbody>{bbox_rows}</tbody></table>"
        if bboxes
        else no_diff_msg
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Screenshot Diff Report</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1117;color:#e2e4ea;padding:28px 20px}}
.hdr{{max-width:1400px;margin:0 auto 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
.hdr h1{{font-size:1.2rem;font-weight:700}}
.stats{{display:flex;gap:10px;flex-wrap:wrap}}
.stat{{background:#1a1d27;border:1px solid #2e3244;border-radius:8px;padding:10px 16px;text-align:center}}
.stat .v{{font-size:1.2rem;font-weight:700;color:#6c63ff}}
.stat .l{{font-size:.7rem;color:#8b8fa3;margin-top:2px}}
.content{{max-width:1400px;margin:0 auto}}
.section{{margin-bottom:28px}}
.section h2{{font-size:.82rem;font-weight:600;color:#8b8fa3;text-transform:uppercase;letter-spacing:.07em;margin-bottom:12px}}
.img-row{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media(max-width:700px){{.img-row{{grid-template-columns:1fr}}}}
.img-card{{background:#1a1d27;border:1px solid #2e3244;border-radius:10px;overflow:hidden}}
.img-card .label{{padding:10px 14px;font-size:.82rem;font-weight:600;color:#8b8fa3;border-bottom:1px solid #2e3244}}
.img-card img{{width:100%;display:block}}
table{{width:100%;border-collapse:collapse;font-size:.82rem;background:#1a1d27;border:1px solid #2e3244;border-radius:10px;overflow:hidden}}
th{{background:#242735;padding:9px 12px;text-align:left;font-weight:600;font-size:.76rem;color:#8b8fa3;border-bottom:1px solid #2e3244}}
td{{padding:7px 12px;border-bottom:1px solid #1e2130;font-family:ui-monospace,monospace;font-size:.8rem;color:#cbd5e1}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:rgba(108,99,255,.05)}}
</style>
</head>
<body>
<div class="hdr">
  <h1>Screenshot Diff Report</h1>
  <div class="stats">
    <div class="stat"><div class="v">{n}</div><div class="l">Changed Regions</div></div>
    <div class="stat"><div class="v">{pct}%</div><div class="l">Changed Area</div></div>
    <div class="stat"><div class="v">{w}&times;{h}</div><div class="l">Image Size (px)</div></div>
  </div>
</div>
<div class="content">
  <div class="section">
    <h2>Screenshots</h2>
    <div class="img-row">
      <div class="img-card">
        <div class="label">Original (annotated)</div>
        <img src="data:image/png;base64,{orig_b64}">
      </div>
      <div class="img-card">
        <div class="label">Generated</div>
        <img src="data:image/png;base64,{gen_b64}">
      </div>
    </div>
  </div>
{crops_section}
  <div class="section">
    <h2>Diff Bounding Boxes</h2>
    {bbox_table}
  </div>
</div>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute a screenshot-only visual diff between two HTML pages.",
    )
    parser.add_argument("--html-a", required=True, help="Original HTML file path or URL.")
    parser.add_argument("--html-b", required=True, help="Generated HTML file path or URL.")
    parser.add_argument("--output-dir", required=True, help="Directory to write diff results.")
    parser.add_argument(
        "--viewport-width", type=int, default=1280,
        help="Viewport width in px (default: 1280).",
    )
    args = parser.parse_args()

    def _progress(stage: str) -> None:
        print(f"[sdiff:{stage}]", flush=True)

    result = run_screenshot_diff(
        html_a=args.html_a,
        html_b=args.html_b,
        output_dir=args.output_dir,
        viewport_width=args.viewport_width,
        progress_cb=_progress,
    )
    print(
        f"[sdiff:done] {json.dumps({'bbox_count': result['bbox_count'], 'bbox_fraction': result['bbox_fraction']})}",
        flush=True,
    )
    print(
        f"Diff complete: {result['bbox_count']} changed regions "
        f"({result['bbox_fraction'] * 100:.1f}% of image area).",
        file=sys.stderr,
    )
    print(f"Report: {result['diff_report_html']}", file=sys.stderr)


if __name__ == "__main__":
    main()
