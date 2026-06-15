from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
Image.MAX_IMAGE_PIXELS = None  # disable decompression bomb limit for our own screenshots
from playwright.sync_api import sync_playwright

from browser_config import (
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
    launch_chromium,
    new_context,
    set_content_and_settle,
)


@dataclass(frozen=True)
class TileSpec:
    name: str
    y: int
    width: int
    height: int


def gpt_tiled_animation_compare(
    baseline_html_path: Path,
    patched_html_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    version = os.getenv("OPENAI_API_VERSION", "2024-02-15-preview")
    if not api_key or not endpoint:
        return {"regression": None, "error": "Azure OpenAI credentials not set"}
    try:
        from openai import AzureOpenAI
    except ImportError:
        return {"regression": None, "error": "openai package not installed"}

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamps = _timestamps()
    tiles, captures = capture_tile_series(
        baseline_html_path=baseline_html_path,
        patched_html_path=patched_html_path,
        output_dir=output_dir,
        timestamps_ms=timestamps,
    )
    sheets = build_tile_contact_sheets(captures, output_dir)
    client = AzureOpenAI(api_key=api_key, api_version=version, azure_endpoint=endpoint)

    tile_results = []
    for tile in tiles:
        sheet_path = sheets[tile.name]
        tile_results.append(_ask_vlm_for_tile(client, sheet_path, tile, timestamps))

    voting_tiles = [
        r for r in tile_results
        if r.get("regression") is True and r.get("persistent_issue") is True
    ]
    result = {
        "regression": len(voting_tiles) > 0,
        "mode": "tiled_animation_series",
        "tile_count": len(tiles),
        "timestamps_ms": timestamps,
        "tiles": [asdict(t) for t in tiles],
        "tile_results": tile_results,
        "voting_tiles": [r.get("tile") for r in voting_tiles],
        "artifacts_dir": str(output_dir),
        "error": None,
    }
    (output_dir / "tiled_vlm_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def capture_tile_series(
    *,
    baseline_html_path: Path,
    patched_html_path: Path,
    output_dir: Path,
    timestamps_ms: list[int],
) -> tuple[list[TileSpec], dict[str, dict[str, dict[int, Path]]]]:
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            baseline_page = _load_html_page(browser, baseline_html_path)
            patched_page = _load_html_page(browser, patched_html_path)
            page_height = max(_page_height(baseline_page), _page_height(patched_page), VIEWPORT_HEIGHT)
            tiles = _tile_specs(page_height)
            captures: dict[str, dict[str, dict[int, Path]]] = {
                tile.name: {"baseline": {}, "patched": {}}
                for tile in tiles
            }
            for tile in tiles:
                for side, page in (("baseline", baseline_page), ("patched", patched_page)):
                    side_dir = output_dir / tile.name / side
                    side_dir.mkdir(parents=True, exist_ok=True)
                    start = _monotonic_ms(page)
                    for ts in timestamps_ms:
                        wait_ms = ts - (_monotonic_ms(page) - start)
                        if wait_ms > 0:
                            page.wait_for_timeout(wait_ms)
                        page.evaluate("(y) => window.scrollTo(0, y)", tile.y)
                        page.wait_for_timeout(150)
                        path = side_dir / f"t{ts}.png"
                        full_path = side_dir / f"t{ts}_full.png"
                        try:
                            page.screenshot(path=str(full_path), full_page=True)
                        except Exception:
                            page.screenshot(path=str(full_path), full_page=False)
                        _crop_tile(full_path, path, tile)
                        full_path.unlink(missing_ok=True)
                        captures[tile.name][side][ts] = path
            return tiles, captures
        finally:
            browser.close()


def build_tile_contact_sheets(
    captures: dict[str, dict[str, dict[int, Path]]],
    output_dir: Path,
) -> dict[str, Path]:
    sheets = {}
    font = ImageFont.load_default()
    for tile_name, by_side in captures.items():
        timestamps = sorted(by_side["baseline"])
        images = []
        for side in ("baseline", "patched"):
            row = []
            for ts in timestamps:
                img = Image.open(by_side[side][ts]).convert("RGB")
                labeled = Image.new("RGB", (img.width, img.height + 24), "white")
                labeled.paste(img, (0, 24))
                draw = ImageDraw.Draw(labeled)
                draw.text((8, 6), f"{side} t={ts}ms", fill="black", font=font)
                row.append(labeled)
            images.append(row)
        width = sum(img.width for img in images[0])
        height = sum(row[0].height for row in images)
        sheet = Image.new("RGB", (width, height), "white")
        y = 0
        for row in images:
            x = 0
            for img in row:
                sheet.paste(img, (x, y))
                x += img.width
            y += row[0].height
        path = output_dir / f"{tile_name}_contact_sheet.jpg"
        sheet.save(path, quality=85)
        sheets[tile_name] = path
    return sheets


def _ask_vlm_for_tile(client, sheet_path: Path, tile: TileSpec, timestamps_ms: list[int]) -> dict[str, Any]:
    prompt = (
        "You are reviewing a visual regression tile series. The image is a contact sheet. "
        "Top row is baseline over time; bottom row is patched over the same timestamps. "
        f"Timestamps are {timestamps_ms} ms after page settle. "
        "Do not classify animation/carousel phase differences as regressions if both rows show valid changing content. "
        "Classify regression=true only when the patched row has a persistent issue across the time series: missing content, "
        "broken layout, illegible text, blank section, missing navigation, or severe style/image/widget failure. "
        "Return strict JSON with keys: tile, regression, persistent_issue, animation_noise, confidence, reason."
    )
    b64 = base64.b64encode(sheet_path.read_bytes()).decode()
    response = client.chat.completions.create(
        model=os.getenv("AZURE_DEPLOYMENT", "gpt-4.1"),
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": "high",
                }},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=600,
        temperature=0,
    )
    raw = response.choices[0].message.content
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Truncated response — extract regression bool directly from raw text
        m = re.search(r'"regression"\s*:\s*(true|false)', raw, re.IGNORECASE)
        reg = m.group(1).lower() == "true" if m else None
        m2 = re.search(r'"persistent_issue"\s*:\s*(true|false)', raw, re.IGNORECASE)
        pi = m2.group(1).lower() == "true" if m2 else None
        data = {"regression": reg, "persistent_issue": pi, "parse_error": "truncated"}
    data.setdefault("tile", tile.name)
    data["raw_response"] = raw
    return data


def _load_html_page(browser, html_path: Path):
    context = new_context(browser)
    page = context.new_page()
    html = html_path.read_text(encoding="utf-8")
    set_content_and_settle(page, html)
    return page


def _crop_tile(full_page_path: Path, output_path: Path, tile: TileSpec) -> None:
    image = Image.open(full_page_path).convert("RGB")
    canvas = Image.new("RGB", (tile.width, tile.height), "white")
    if image.width <= 0 or image.height <= 0:
        canvas.save(output_path)
        return
    y = min(max(0, tile.y), max(0, image.height - 1))
    width = min(tile.width, image.width)
    height = max(1, min(tile.height, image.height - y))
    crop = image.crop((0, y, width, y + height))
    canvas.paste(crop, (0, 0))
    canvas.save(output_path)


def _page_height(page) -> int:
    return int(page.evaluate("() => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)"))


def _tile_specs(page_height: int) -> list[TileSpec]:
    max_tiles = int(os.getenv("REGRESSION_VLM_MAX_TILES", "6"))
    tile_h = int(os.getenv("REGRESSION_VLM_TILE_HEIGHT", str(VIEWPORT_HEIGHT)))
    overlap = int(os.getenv("REGRESSION_VLM_TILE_OVERLAP", "100"))
    cutoff = int(os.getenv("REGRESSION_VLM_FIRST_PASS_CUTOFF", "3000"))
    effective_height = min(page_height, cutoff)
    ys = [0]
    step = max(1, tile_h - overlap)
    y = step
    while y < effective_height and len(ys) < max_tiles - 1:
        ys.append(y)
        y += step
    bottom = max(0, page_height - tile_h)
    if bottom not in ys and len(ys) < max_tiles:
        ys.append(bottom)
    names = ["top"] + [f"mid_{i}" for i in range(1, max(1, len(ys) - 1))] + (["bottom"] if len(ys) > 1 else [])
    if len(names) != len(ys):
        names = [f"tile_{i}" for i in range(len(ys))]
    return [
        TileSpec(name=name, y=int(y), width=VIEWPORT_WIDTH, height=min(tile_h, page_height - int(y) or tile_h))
        for name, y in zip(names, ys)
    ]


def _timestamps() -> list[int]:
    raw = os.getenv("REGRESSION_VLM_TIMESTAMPS_MS", "0,1000,3000")
    return sorted({int(part.strip()) for part in raw.split(",") if part.strip()})


def _monotonic_ms(page) -> int:
    return int(page.evaluate("() => performance.now()"))
